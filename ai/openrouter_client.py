"""
Thin client for OpenRouter's chat completions endpoint (OpenAI-compatible).
Docs: https://openrouter.ai/docs
"""
import logging

import requests

from config import config

logger = logging.getLogger("finedine-agent")


class OpenRouterError(Exception):
    pass


# Failures worth retrying on the next model in the fallback chain: rate
# limits, upstream provider/server errors, a request that timed out or came
# back empty, and "model not found/unavailable" - OpenRouter's free-model
# lineup changes often (slugs get retired/promoted to paid with little
# notice), so a 404 here usually means "this specific model slug is gone",
# not "this request is malformed" (which would fail identically everywhere
# and isn't itself a model slug problem).
_RETRYABLE_STATUS_CODES = {404, 429, 500, 502, 503, 504}


def _call_model(messages, model: str, temperature: float, max_tokens: int) -> str:
    """Raises OpenRouterError on any failure - callers decide whether that's
    retryable (see _RETRYABLE_STATUS_CODES) or should propagate immediately."""
    resp = requests.post(
        f"{config.OPENROUTER_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": f"{config.STORE_NAME} WhatsApp Agent",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=30,
    )

    if resp.status_code != 200:
        raise OpenRouterError(f"OpenRouter error {resp.status_code}: {resp.text}")

    if not resp.text.strip():
        raise OpenRouterError("OpenRouter returned an empty response body (model may be overloaded/unavailable)")

    try:
        data = resp.json()
    except ValueError as e:
        raise OpenRouterError(f"OpenRouter returned non-JSON response: {resp.text[:500]}") from e

    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise OpenRouterError(f"Unexpected OpenRouter response: {data}") from e


def chat_completion(messages, temperature: float = 0.3, max_tokens: int = 600, model: str = None) -> str:
    """Tries `model` (or config.OPENROUTER_MODEL) first, then falls through
    config.OPENROUTER_FALLBACK_MODELS in order on a retryable failure (rate
    limit, provider outage, timeout, empty/malformed response) - so one
    free-tier model being temporarily rate-limited upstream doesn't take the
    whole bot down. A non-retryable failure (e.g. bad request, auth error)
    raises immediately without burning through the fallback chain, since
    every model would fail the same way."""
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set in .env")

    primary = model or config.OPENROUTER_MODEL
    # Only chain fallbacks for the default text model - an explicit model=
    # override (e.g. the vision model) is a deliberate choice by the caller,
    # not something to silently swap out for a text-only fallback.
    candidates = [primary] if model else [primary] + [m for m in config.OPENROUTER_FALLBACK_MODELS if m != primary]

    last_error = None
    for i, candidate in enumerate(candidates):
        try:
            return _call_model(messages, candidate, temperature, max_tokens)
        except OpenRouterError as e:
            last_error = e
            is_last = i == len(candidates) - 1
            status = _extract_status_code(str(e))
            retryable = status is None or status in _RETRYABLE_STATUS_CODES
            if not retryable or is_last:
                raise
            logger.warning("OpenRouter model '%s' failed (%s) - falling back to next model", candidate, e)

    raise last_error


def _extract_status_code(error_message: str):
    """Pulls the leading HTTP status code back out of an OpenRouterError's
    message (see _call_model's "OpenRouter error {status}: ..." format) -
    returns None for errors with no status code (empty body, bad JSON,
    unexpected shape), which are treated as retryable by default."""
    prefix = "OpenRouter error "
    if not error_message.startswith(prefix):
        return None
    try:
        return int(error_message[len(prefix):].split(":", 1)[0].strip())
    except ValueError:
        return None
