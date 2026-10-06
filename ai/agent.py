"""
Orchestrates a single customer turn for restaurant food ordering:
  1. search the menu catalog for items relevant to what the customer said
  2. build a system prompt grounded in the menu + the customer's current order
  3. call OpenRouter for a reply
  4. lightweight structured extraction to keep the order (order_items) in sync
  5. explicit confirm/cancel handling once a total has been presented

Order lifecycle (see storage/store.py): draft -> awaiting_confirmation -> confirmed
-> (delivery only) packed -> picked_up -> delivered. Only a message that
unambiguously affirms the exact confirmation prompt moves an order to
'confirmed' - that check is a plain keyword match here, not left to the AI's
free-form judgement, so a customer can never accidentally place an order.
"""
import base64
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config import config
from ai.openrouter_client import chat_completion
from ai.ordering_knowledge import get_clarification_hints
from catalog import store as catalog_store
from offers import store as offers_store

CONFIRM_WORDS = {"confirm", "yes", "yep", "yeah", "ok", "okay", "place order", "place the order", "sure", "go ahead"}
CANCEL_WORDS = {"cancel", "no", "nope", "stop", "wait", "change"}
DELIVERY_WORDS = {"delivery", "deliver"}
PICKUP_WORDS = {"pickup", "pick up", "pick-up", "collect", "self pickup", "self-pickup", "takeaway", "take away"}
DINE_IN_WORDS = {"dine in", "dine-in", "dinein", "eating in", "eat in", "at the restaurant", "table"}

# Signals a message is probably a delivery address rather than a food item -
# not a full address parser, just enough to distinguish "12 Al Wasl Road,
# near the mosque" from "2 chicken biryani". Used only while an order is
# awaiting a delivery decision/address (see main.py's gating).
ADDRESS_KEYWORDS = {
    "street", "st.", "road", "rd.", "villa", "building", "bldg", "apartment", "apt", "flat",
    "near", "opposite", "behind", "next to", "area", "block", "floor", "tower", "avenue",
    "district", "sector", "house", "gate", "landmark",
}

# A generic "what do you have?" question names no specific dish, so the
# keyword search in search_catalog_for_message legitimately finds nothing -
# these phrases signal the customer wants an overview instead, which falls
# back to listing categories rather than claiming the menu is unavailable.
MENU_BROWSE_PHRASES = {
    "menu", "what do you have", "whats on the menu", "what's on the menu",
    "what can i order", "what do you serve", "show me the menu", "categories",
}

SYSTEM_PROMPT_TEMPLATE = """You are the WhatsApp ordering assistant for {store_name}, a restaurant. You help \
customers order food using ONLY the menu items listed below - never invent a dish, price, or availability that \
isn't in this list. Be concise (WhatsApp-length replies, a few short sentences). Be warm and appetizing, but don't \
ramble.

Rules:
- Always be warm, polite, and respectful, even if the customer is short, impatient, or frustrated.
- If the customer's name is known (see "Customer name" below), greet/address them by it naturally once near the \
start of the conversation (e.g. "Welcome back, {{name}}!") - never ask for their name, WhatsApp already provides it. \
If it's not known, don't ask for it either; just proceed without using a name.
- Always reply in the same language the customer is writing in (e.g. Arabic, Hindi, Malayalam, Tamil, English) - if \
they switch languages mid-conversation, switch with them. Write the ENTIRE reply in that one language and script \
consistently - never mix scripts or languages within a single reply (e.g. don't blend Tamil and Bengali characters, \
or switch back to English mid-sentence). If you're not fully confident in a non-English language, it's better to \
reply in clear, simple English than to risk a garbled or mixed-script reply. Menu item names and prices stay as \
listed (Latin script/English) regardless of the reply language, since that's how they're listed in the system.
- Only offer/confirm items that appear in the menu context below, using their exact listed price and unit.
- If the menu context below lists categories instead of specific dishes (this happens when the customer asked a \
general "what's on the menu" question rather than naming a dish), briefly list those categories and ask which one \
interests them, or what dish they're in the mood for - don't claim the menu is unavailable, and don't invent dish \
names or prices before the customer narrows it down.
- Many dishes come in Half and Full sizes, listed as separate menu entries (e.g. "Butter Chicken (Half)" / "Butter \
Chicken (Full)"). If a customer orders a dish that has both sizes in the menu context, ask which size they want \
before adding it - never guess. If a dish only has one size listed, don't offer a choice that doesn't exist.
- Before finalizing a line item, check the clarification hints below for that specific item - if a hint applies \
(e.g. spice level, which variety, sweetness), ask exactly that ONE question and nothing else in that message, then \
wait for the answer before moving on. Never combine a clarifying question with the delivery/pickup or address ask \
in the same message, even if the customer says they're done ordering - finish clarifying the item first, send that \
as its own message, and only ask about delivery in a later message once the item is fully settled. Don't re-ask \
something the customer already told you. If a customer's answer to a clarifying question is a short or unclear \
reply (e.g. a typo or abbreviation you're not confident about), don't guess - briefly confirm what you understood \
before proceeding (e.g. "Just to confirm - extra sweet, or something else?").
- If an item is marked NOT AVAILABLE or isn't in the menu, say so plainly and suggest a similar available dish.
- If a customer asks the price of a dish, state it clearly from the menu context, and add one brief, genuine \
reason to order it (e.g. "it's one of our most popular biryanis") - never invent a claim not reasonably inferable \
from the menu, and never be pushy about it.
- Keep a running mental order as the customer adds items; when you list it, list each item with qty, unit price, \
and line total.
- Check "Operating hours" below before moving toward checkout. If it says CLOSED, you can still discuss the menu \
and build up their order, but tell them plainly (once, don't repeat every message) that we're currently closed and \
state the hours - never present a final total or ask for CONFIRM while closed. If they're browsing while closed, \
let them know their order will be saved and they can confirm once we reopen.
- Once the customer seems finished ordering (e.g. "that's it", "checkout", "done"), ask whether this is for \
DELIVERY, PICKUP (takeaway), or DINE-IN if not already stated.
  - For pickup or dine-in: no location needed, no delivery fee - go straight to the final itemized total. For \
dine-in, let them know their order will be prepared and ready for them at the restaurant.
  - For delivery: if the customer has a saved address (see "Saved address" below), ask them to confirm it's still \
correct ("Deliver to {{saved address}} again? Reply YES or share a new location") instead of asking them to share \
location from scratch. If they have no saved address, ask them to either share their location using WhatsApp's \
location attachment (paperclip -> Location) OR simply type their delivery address as text - both are fine, they \
don't need to use the location feature if it's easier to type it.
- If the customer's delivery address came from a voice note (visible in the conversation as a message you said or \
that appears after a spoken message), read the address back to them explicitly and ask them to confirm it's \
correct before finalizing the order, since speech-to-text can mishear house/building numbers and street names - \
don't treat a transcribed address with the same confidence as a typed one or a shared location pin.
- Once you have everything needed (items, size/clarification choices, delivery/pickup/dine-in choice, and a \
confirmed address if delivery), present the final itemized order: items, subtotal, delivery fee (0 for pickup/\
dine-in), and total in {currency}. All menu prices already include VAT - never add VAT on top or mention it unless \
the customer specifically asks, in which case note that prices are VAT-inclusive. End that message by asking the \
customer to reply CONFIRM to place the order or CANCEL to change it. Always phrase it this way so the system can \
detect the reply. Accept every order the customer wants to place, regardless of size - never refuse or discourage an \
order. Move toward this final confirmation as quickly as possible once the cart, delivery/pickup/dine-in choice, and \
address (if needed) are all settled - don't ask further clarifying questions once nothing is actually ambiguous, \
don't repeat information already confirmed, and don't linger once you have what you need. Speed to checkout matters: \
every extra message is friction for the customer.
- Once an order's items are settled (customer seems done adding more), proactively suggest one or two popular \
extras that pair well (e.g. a drink, a side, or a dessert) from the menu context if something relevant is shown - \
but only once, and only from what's actually in the menu context, never invented. Don't push this into every reply.
- If the customer seems ready to check out with a very small order (e.g. a single low-priced item), you may gently \
mention that adding a side or drink makes for a fuller meal - but this is a soft, one-time suggestion only, never a \
requirement, and never refuse or delay checkout if they decline. Accept whatever they order, however small.
- If any active offers are listed below, mention the relevant one naturally when it applies to what the customer \
is ordering (e.g. a percent-off deal that applies to their cart) - but only once per conversation, and only if \
it's genuinely relevant, never forced into every reply.
- Use the time-of-day context below to steer what you proactively suggest: lean toward breakfast items in the \
morning, lunch-friendly options midday, and dinner/heavier dishes in the evening - but never refuse an order for an \
item just because of the time of day; only use it to shape unprompted suggestions, not to gatekeep what a customer \
can order.
- Never say an order is placed/confirmed yourself - only the system marks an order confirmed after the customer \
replies to that exact prompt. If asked "is my order confirmed?", check the order status context below and answer \
truthfully.
- Keep replies under 100 words unless summarizing a full order requires more.

Menu context (items relevant to this conversation):
{catalog_context}

Clarification hints for items shown above:
{clarification_hints}

Customer's current order:
{cart_context}

Order status: {order_status}
Delivery info: {delivery_context}
Customer name: {customer_name_context}
Saved address: {saved_address_context}
Active offers: {active_offers_context}
Time of day: {time_of_day_context}
Operating hours: {operating_hours_context}
"""


def _format_catalog_context(items: list, categories: list = None) -> str:
    if items:
        parts = []
        for it in items:
            availability = "available" if it["in_stock"] else "NOT AVAILABLE"
            parts.append(f"- {it['name']}: {config.CURRENCY} {it['price']:.2f} - {availability}")
        return "\n".join(parts)
    if categories:
        return (
            "No specific dish matched this message, but here are the menu's categories - ask the customer "
            "which one interests them, or what dish they'd like, rather than listing every item:\n"
            + "\n".join(f"- {c}" for c in categories)
        )
    return "(No matching menu items found for this query.)"


def _format_cart_context(order: dict | None, items: list) -> str:
    if not order or not items:
        return "(Empty - no items added yet.)"
    lines = [f"- {i['qty']:g} x {i['item_name_snapshot']} @ {config.CURRENCY} {i['unit_price_snapshot']:.2f} = {config.CURRENCY} {i['line_total']:.2f}" for i in items]
    lines.append(f"Subtotal: {config.CURRENCY} {order['subtotal']:.2f}")
    return "\n".join(lines)


def _format_delivery_context(order: dict | None) -> str:
    if not order:
        return "(No active order.)"
    if order.get("order_type") == "dine_in":
        return "Dine-in order - no delivery fee, no location needed."
    if order.get("is_pickup"):
        return "Pickup order - no delivery fee, no location needed."
    if order.get("delivery_lat") is not None:
        return (
            f"Delivery location received. Delivery fee: {config.CURRENCY} {order['delivery_fee']:.2f}, "
            f"Total: {config.CURRENCY} {order['total']:.2f}"
        )
    return "(Delivery-vs-pickup-vs-dine-in not yet decided, or delivery location not yet shared.)"


def _format_customer_name_context(customer: dict | None) -> str:
    if customer and customer.get("name"):
        return customer["name"]
    return "(Not known yet - WhatsApp didn't provide a profile name for this customer.)"


def _format_saved_address_context(customer: dict | None) -> str:
    if not customer:
        return "(No saved address for this customer yet.)"
    if customer.get("last_lat") is not None:
        label = customer.get("last_location_label") or f"{customer['last_lat']:.5f}, {customer['last_lng']:.5f}"
        return f"{label} (from a previous order)"
    if customer.get("last_address_text"):
        return f"{customer['last_address_text']} (from a previous order)"
    return "(No saved address for this customer yet.)"


def _format_offers_context(offers: list) -> str:
    if not offers:
        return "(No active offers right now.)"
    parts = []
    for o in offers:
        if o["discount_type"] == "percent":
            discount = f"{o['discount_value']:g}% off"
        else:
            discount = f"{config.CURRENCY} {o['discount_value']:.2f} off"
        line = f"- {o['title']}: {discount}"
        if o.get("description"):
            line += f" - {o['description']}"
        parts.append(line)
    return "\n".join(parts)


def _now_local() -> datetime:
    try:
        return datetime.now(ZoneInfo(config.STORE_TIMEZONE))
    except Exception:
        return datetime.now()


def _current_daypart() -> str:
    hour = _now_local().hour
    if 5 <= hour < 11:
        return "breakfast (morning)"
    if 11 <= hour < 15:
        return "lunch (midday)"
    if 15 <= hour < 18:
        return "afternoon"
    if 18 <= hour < 23:
        return "dinner (evening)"
    return "late-night"


def is_accepting_orders() -> bool:
    """True if the restaurant is currently open AND before the order cutoff
    (STORE_ORDER_CUTOFF_MINUTES before STORE_CLOSE_HOUR), for all order
    types. STORE_CLOSE_HOUR < STORE_OPEN_HOUR means closing time is past
    midnight (e.g. open 8, close 2 -> open 08:00 through 01:30 the next
    "business day" with a 30-min cutoff)."""
    now = _now_local()
    open_minutes = config.STORE_OPEN_HOUR * 60
    close_minutes = config.STORE_CLOSE_HOUR * 60
    if close_minutes <= open_minutes:
        close_minutes += 24 * 60  # closing time is past midnight
    cutoff_minutes = close_minutes - config.STORE_ORDER_CUTOFF_MINUTES

    now_minutes = now.hour * 60 + now.minute
    # If we're in the "past midnight" portion of the business day (e.g. it's
    # 01:00 and close is scheduled at 26:00/2 AM next-day-equivalent), shift
    # "now" the same way so it compares on the same number line.
    if now_minutes < open_minutes and close_minutes > 24 * 60:
        now_minutes += 24 * 60

    return open_minutes <= now_minutes < cutoff_minutes


def operating_hours_label() -> str:
    """Human-readable hours string for customer-facing messages, e.g.
    "8:00 AM - 2:00 AM (last orders 1:30 AM)"."""
    def fmt(total_minutes: int) -> str:
        h, m = divmod(total_minutes % (24 * 60), 60)
        period = "AM" if h < 12 else "PM"
        display_h = h % 12 or 12
        return f"{display_h}:{m:02d} {period}"

    open_minutes = config.STORE_OPEN_HOUR * 60
    close_minutes = config.STORE_CLOSE_HOUR * 60
    if close_minutes <= open_minutes:
        close_minutes += 24 * 60
    cutoff_minutes = close_minutes - config.STORE_ORDER_CUTOFF_MINUTES
    return f"{fmt(open_minutes)} - {fmt(close_minutes)} (last orders {fmt(cutoff_minutes)})"


def _format_time_of_day_context() -> str:
    return f"It is currently {_current_daypart()} for the restaurant's local time."


def _format_operating_hours_context() -> str:
    if is_accepting_orders():
        return f"Open now, accepting orders. Hours: {operating_hours_label()}."
    return (
        f"CLOSED right now (outside operating hours). Hours: {operating_hours_label()}. "
        "Do not accept or confirm any order right now - tell the customer we're closed and when we reopen, "
        "but you can still answer menu questions."
    )


def search_catalog_for_message(message: str, top_k: int = 8) -> list:
    """Very simple keyword-based menu matching: try the whole message, and
    fall back to individual significant words. Good enough for a
    single-restaurant menu of a few hundred items."""
    results = catalog_store.search_items(message, limit=top_k)
    if results:
        return results

    words = [w for w in re.findall(r"[a-zA-Z]{3,}", message) if w.lower() not in {"the", "and", "for", "with"}]
    seen_ids = set()
    combined = []
    for w in words:
        for item in catalog_store.search_items(w, limit=4):
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                combined.append(item)
    return combined[:top_k]


def detect_confirmation_intent(message: str) -> str | None:
    """Returns 'confirm', 'cancel', or None. Only meaningful while an order
    is awaiting_confirmation - callers gate on that state."""
    lowered = message.strip().lower()
    if any(word == lowered or lowered.startswith(word) for word in CONFIRM_WORDS):
        return "confirm"
    if any(word == lowered or lowered.startswith(word) for word in CANCEL_WORDS):
        return "cancel"
    return None


def detect_delivery_preference(message: str) -> str | None:
    """Returns 'delivery', 'pickup', 'dine_in', or None - used to detect the
    customer's answer to the delivery/pickup/dine-in question. Checked in
    this order since "dine in" and "pickup" phrasing don't overlap but both
    should be checked before the broader "delivery" match."""
    lowered = message.strip().lower()
    if any(word in lowered for word in DINE_IN_WORDS):
        return "dine_in"
    if any(word in lowered for word in PICKUP_WORDS):
        return "pickup"
    if any(word in lowered for word in DELIVERY_WORDS):
        return "delivery"
    return None


def detect_reuse_saved_address(message: str) -> bool:
    """Lightweight yes-match for 'deliver to the same address again?' -
    deliberately narrower than detect_confirmation_intent's CONFIRM_WORDS
    since this is a different question in the flow (confirm the *address*,
    not the *order*), even though the affirmative wording overlaps."""
    lowered = message.strip().lower()
    return lowered in {"yes", "yep", "yeah", "y", "same", "same address", "confirm"} or lowered.startswith("yes")


def detect_probable_address(message: str) -> bool:
    """Lightweight heuristic for 'this looks like a typed delivery address,
    not a food item or a confirm/cancel/delivery-preference reply' - not a
    real address parser, just enough signal (digits + common address words)
    to route the message correctly. Callers gate this on the order already
    being at the delivery-address-needed step (see main.py), so it's never
    checked against arbitrary conversation turns."""
    lowered = message.strip().lower()
    if not lowered or len(lowered) < 6:
        return False
    has_digit = any(ch.isdigit() for ch in lowered)
    has_address_word = any(word in lowered for word in ADDRESS_KEYWORDS)
    return has_digit and has_address_word


def generate_reply(customer_message: str, order: dict | None, order_items: list, customer: dict | None = None, history: list = None) -> str:
    catalog_items = search_catalog_for_message(customer_message)
    # A generic "what's on the menu?" question names no specific dish, so
    # the keyword search above legitimately finds nothing - fall back to
    # listing categories instead of telling the customer the menu is
    # unavailable (see _format_catalog_context).
    categories = None
    if not catalog_items:
        lowered = customer_message.strip().lower()
        if any(phrase in lowered for phrase in MENU_BROWSE_PHRASES):
            categories = catalog_store.list_categories()

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        store_name=config.STORE_NAME,
        currency=config.CURRENCY,
        catalog_context=_format_catalog_context(catalog_items, categories),
        clarification_hints=get_clarification_hints(catalog_items),
        cart_context=_format_cart_context(order, order_items),
        order_status=order["status"] if order else "no active order",
        delivery_context=_format_delivery_context(order),
        customer_name_context=_format_customer_name_context(customer),
        saved_address_context=_format_saved_address_context(customer),
        active_offers_context=_format_offers_context(offers_store.get_active_offers()),
        time_of_day_context=_format_time_of_day_context(),
        operating_hours_context=_format_operating_hours_context(),
    )

    messages = [{"role": "system", "content": system_prompt}]
    for direction, text in (history or []):
        role = "user" if direction == "in" else "assistant"
        messages.append({"role": role, "content": text})
    messages.append({"role": "user", "content": customer_message})

    return chat_completion(messages)


def _guess_dish_name_from_image(image_bytes: bytes, mime_type: str, caption: str = "") -> str:
    """Asks the vision model for just a short dish name/description - no
    menu context involved, this is purely 'what do you see' so it can be
    used as a search query against the menu afterwards."""
    b64_image = base64.b64encode(image_bytes).decode("ascii")
    messages = [
        {"role": "system", "content": (
            "Look at the photo and reply with ONLY the short dish/food name you see "
            "(e.g. 'chicken biryani', 'butter chicken', 'mango juice'), nothing else. If you can't tell, reply 'unknown'."
        )},
        {"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_image}"}},
            {"type": "text", "text": caption.strip() or "What dish is this?"},
        ]},
    ]
    return chat_completion(messages, model=config.OPENROUTER_VISION_MODEL, temperature=0.1, max_tokens=20)


def generate_image_reply(image_bytes: bytes, mime_type: str, caption: str = "") -> str:
    """Analyzes a customer-sent photo (e.g. of a dish they saw elsewhere or
    want to identify) and tries to match it to a specific menu item, so the
    reply can state a real price/availability instead of a vague
    description. Descriptive only - never adds the item to the order
    itself; the customer confirms verbally on their next message, which
    flows through the normal text path."""
    guess = _guess_dish_name_from_image(image_bytes, mime_type, caption)
    query = guess.strip() if guess.strip().lower() != "unknown" else (caption.strip() or "food photo sent by customer")

    catalog_items = search_catalog_for_message(query)
    best_match = catalog_items[0] if catalog_items else None

    if best_match:
        system_prompt = (
            f"You are {config.STORE_NAME}'s WhatsApp ordering assistant. A customer sent a photo of a dish. "
            f"Vision analysis identified it as likely being '{guess}', which best matches this menu item:\n"
            f"{_format_catalog_context([best_match])}\n\n"
            f"Confirm this specific match to the customer with its exact price and availability from above, and ask "
            f"if they'd like to add it to their order (they can just say so in their next message). Never invent a "
            f"price or availability not shown above. If the vision match seems unlikely to be right, say so honestly "
            f"instead of asserting it. Keep the reply concise."
        )
    else:
        system_prompt = (
            f"You are {config.STORE_NAME}'s WhatsApp ordering assistant. A customer sent a photo, likely of a dish "
            f"they want to order or ask about. Vision analysis guessed it might be '{guess}', but nothing on our "
            f"menu matched. Describe what it appears to be, say we don't have an exact match, and suggest the "
            f"customer describe it in words or browse similar items below if any seem relevant. Never invent a "
            f"price or availability not in the menu context.\n\nMenu context:\n{_format_catalog_context(catalog_items)}"
        )

    b64_image = base64.b64encode(image_bytes).decode("ascii")
    user_content = [
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64_image}"}},
        {"type": "text", "text": caption.strip() or "What is this and can I order it?"},
    ]

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    return chat_completion(messages, model=config.OPENROUTER_VISION_MODEL)


def compute_delivery_fee(subtotal: float) -> float:
    if subtotal >= config.FREE_DELIVERY_THRESHOLD:
        return 0.0
    return config.DELIVERY_FEE
