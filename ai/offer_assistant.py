"""
Takes an admin's free-text offer request ("20% off biryani on weekends")
and asks OpenRouter to return a structured offer draft. The admin always
reviews the draft before it's saved (as a 'draft' status offer) and must
separately activate it - this module never activates anything itself.
"""
import json
from datetime import date

from ai.openrouter_client import chat_completion
from storage import store

SYSTEM_PROMPT = """You convert a restaurant admin's plain-language promotion request into a structured offer.
Return ONLY a JSON object (no markdown fences, no extra text) with exactly these fields:
{
  "title": "short offer name",
  "description": "one sentence customer-facing description",
  "discount_type": "percent" or "fixed",
  "discount_value": number,
  "scope_type": "all" or "category" or "items",
  "scope_value": ["list of menu category names or dish names mentioned, empty list if scope_type is 'all'"],
  "starts_at": "YYYY-MM-DD or null if not specified",
  "ends_at": "YYYY-MM-DD or null if not specified"
}

Rules:
- If the admin doesn't specify a discount type, assume "percent".
- If dates are relative (e.g. "this weekend", "next week"), resolve them to actual dates using today's date given below.
- Never invent a discount value the admin didn't imply - if genuinely unclear, use a conservative default like 10.
- scope_value should list category/dish names as plain strings, not IDs.

Today's date: __TODAY__
"""


class OfferDraftError(Exception):
    pass


def draft_offer(admin_prompt: str) -> dict:
    system_prompt = SYSTEM_PROMPT.replace("__TODAY__", date.today().isoformat())
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": admin_prompt},
    ]

    result = chat_completion(messages, temperature=0.2, max_tokens=400)
    store.log_ai_usage(
        purpose="offer_draft", model=result.model, prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens, total_tokens=result.total_tokens,
    )
    raw = result.text

    # Models sometimes wrap JSON in markdown fences despite instructions - strip if present.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        draft = json.loads(cleaned)
    except ValueError as e:
        raise OfferDraftError(f"AI did not return valid JSON: {raw[:300]}") from e

    required = {"title", "description", "discount_type", "discount_value", "scope_type", "scope_value"}
    missing = required - set(draft.keys())
    if missing:
        raise OfferDraftError(f"AI response missing field(s): {', '.join(missing)}")

    if draft["discount_type"] not in ("percent", "fixed"):
        draft["discount_type"] = "percent"

    draft.setdefault("starts_at", None)
    draft.setdefault("ends_at", None)
    return draft
