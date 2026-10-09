"""
Thin client for OpenRouter's chat completions endpoint (OpenAI-compatible).
Docs: https://openrouter.ai/docs
"""
import logging
from dataclasses import dataclass

import requests

from config import config

logger = logging.getLogger("finedine-agent")


class OpenRouterError(Exception):
    pass


@dataclass
class ChatCompletionResult:
    """Return type of chat_completion() - carries the reply text plus which
    model actually answered (after any fallback chain) and its real token
    usage, so callers can log cost (see storage.store.log_ai_usage) without
    a second round-trip. token fields are None if a provider's response
    omitted usage data (not all do) - never guessed."""
    text: str
    model: str
    prompt_tokens: int = None
    completion_tokens: int = None
    total_tokens: int = None


# Failures worth retrying on the next model in the fallback chain: rate
# limits, upstream provider/server errors, a request that timed out or came
# back empty, and "model not found/unavailable" - OpenRouter's free-model
# lineup changes often (slugs get retired/renamed/promoted to paid with
# little notice, confirmed in production: both 404 "model unavailable" and
# 400 "not a valid model ID" have been observed for a stale/wrong slug), so
# these usually mean "this specific model slug is wrong/gone", not "this
# request is malformed" (which would fail identically everywhere).
_RETRYABLE_STATUS_CODES = {404, 429, 500, 502, 503, 504}

# A 400 is ambiguous (could be a bad model slug OR a genuinely malformed
# request) - only treat it as retryable when OpenRouter's own error message
# says so, so a real malformed-request 400 still fails fast as intended.
_RETRYABLE_400_MARKERS = ("not a valid model id", "model not found")


def _call_model(messages, model: str, temperature: float, max_tokens: int) -> tuple:
    """Returns (content, usage) - usage is OpenRouter's raw "usage" dict
    (prompt_tokens/completion_tokens/total_tokens) when the response
    includes one, else {} (not all providers return it)."""
    """Raises OpenRouterError on any failure - callers decide whether that's
    retryable (see _RETRYABLE_STATUS_CODES) or should propagate immediately."""
    try:
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
                # Some free models (e.g. nemotron reasoning variants) emit
                # their internal "thinking" text as part of the visible
                # reply by default - this is the actual, structural fix for
                # the raw chain-of-thought ("Wait! The assistant previous
                # turn hallucinated...") that was confirmed live leaking
                # straight to a customer, found by comparing against a
                # sibling project (wurth-whatsapp-agent) that never hit this
                # problem specifically because it sets this flag. Stops the
                # reasoning text from ever being generated as part of
                # content in the first place, instead of post-hoc filtering
                # it out after the fact.
                "reasoning": {"exclude": True},
            },
            # A model that's about to fail (429/empty content) responds almost
            # instantly - this timeout mainly matters for a model that hangs
            # instead of failing fast. Lowered from 30s: confirmed live that a
            # customer waited through 2+ models failing in sequence before a
            # working one was reached, and the full fallback chain (5 models)
            # could take minutes at 30s each in the worst case. 15s still gives
            # a genuinely slow-but-working model room to respond, while capping
            # how long a hung model can delay the whole chain.
            timeout=15,
        )
    except requests.exceptions.RequestException as e:
        # A network-level failure (timeout, connection error, DNS issue)
        # raises its own exception type, never reaching the status-code
        # check below - confirmed live: a ReadTimeout crashed straight
        # through chat_completion's "except OpenRouterError" (which never
        # catches it) and fell all the way out to main.py's generic error
        # handler, completely skipping the fallback chain even though
        # other models were available and untried. Wrapping it as an
        # OpenRouterError (with no status code, so it's treated as
        # retryable by default - see _extract_status_code) lets the normal
        # fallback logic handle it like any other failure.
        raise OpenRouterError(f"OpenRouter request failed for model '{model}': {e}") from e

    if resp.status_code != 200:
        raise OpenRouterError(f"OpenRouter error {resp.status_code}: {resp.text}")

    if not resp.text.strip():
        raise OpenRouterError("OpenRouter returned an empty response body (model may be overloaded/unavailable)")

    try:
        data = resp.json()
    except ValueError as e:
        raise OpenRouterError(f"OpenRouter returned non-JSON response: {resp.text[:500]}") from e

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise OpenRouterError(f"Unexpected OpenRouter response: {data}") from e

    # Some free-tier models return HTTP 200 with content: null/"" under load
    # or moderation - confirmed live (AttributeError crashing the whole
    # request instead of falling back). Treat this the same as any other
    # retryable failure so chat_completion's fallback chain moves on to the
    # next model instead of taking the bot down.
    if not content or not content.strip():
        raise OpenRouterError(f"OpenRouter model '{model}' returned empty content (choices[0].message.content was null/blank)")

    return content.strip(), (data.get("usage") or {})


def chat_completion(messages, temperature: float = 0.3, max_tokens: int = 1000, model: str = None,
                     fallback_models: list = None) -> ChatCompletionResult:
    """Tries `model` (or config.OPENROUTER_MODEL) first, then falls through
    `fallback_models` (or config.OPENROUTER_FALLBACK_MODELS when no explicit
    model= override is given) in order on a retryable failure (rate limit,
    provider outage, timeout, empty/malformed response) - so one free-tier
    model being temporarily rate-limited upstream doesn't take the whole
    feature down. A non-retryable failure (e.g. bad request, auth error)
    raises immediately without burning through the fallback chain, since
    every model would fail the same way.

    An explicit model= override used to skip the fallback chain entirely
    (on the reasoning that it's a deliberate caller choice, e.g. picking a
    vision-capable model) - confirmed live that this let a single free
    vision model's temporary 429 upstream rate limit fail the ENTIRE image
    feature outright, every time, with no fallback attempted at all. Now a
    caller can pass its own fallback_models (e.g. ai/agent.py passing
    config.OPENROUTER_VISION_FALLBACK_MODELS) to get the same resilience
    for a non-default model; omitting it preserves the old no-fallback
    behavior for callers that genuinely want only one exact model tried.

    Returns a ChatCompletionResult (text + which model answered + real
    token usage from OpenRouter, for cost tracking - see storage.store.
    log_ai_usage) rather than a plain string."""
    if not config.OPENROUTER_API_KEY:
        raise OpenRouterError("OPENROUTER_API_KEY is not set in .env")

    primary = model or config.OPENROUTER_MODEL
    if fallback_models is not None:
        chain = fallback_models
    elif model:
        chain = []
    else:
        chain = config.OPENROUTER_FALLBACK_MODELS
    candidates = [primary] + [m for m in chain if m != primary]

    last_error = None
    for i, candidate in enumerate(candidates):
        try:
            content, usage = _call_model(messages, candidate, temperature, max_tokens)
            return ChatCompletionResult(
                text=content,
                model=candidate,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
                total_tokens=usage.get("total_tokens"),
            )
        except OpenRouterError as e:
            last_error = e
            is_last = i == len(candidates) - 1
            status = _extract_status_code(str(e))
            retryable = (
                status is None
                or status in _RETRYABLE_STATUS_CODES
                or (status == 400 and any(marker in str(e).lower() for marker in _RETRYABLE_400_MARKERS))
            )
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
