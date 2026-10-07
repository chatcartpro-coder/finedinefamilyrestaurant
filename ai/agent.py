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
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from config import config
from ai.openrouter_client import chat_completion
from ai.ordering_knowledge import get_clarification_hints
from catalog import store as catalog_store
from offers import store as offers_store

logger = logging.getLogger("finedine-agent")

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
    # UAE-specific plot/unit/community shorthand and common area names - a
    # real address like "Al Wasl P562" or "JVC S11 R12" has no generic
    # English address word at all, so the original keyword list alone
    # missed it entirely (confirmed live: a customer's typed address never
    # got captured, the order stayed stuck in draft with no way to confirm).
    "plot", "unit", "suite", "no.", "room", "community", "cluster",
    "warehouse", "al wasl", "al quoz", "al barsha", "jvc", "jlt", "jbr",
    "marina", "downtown", "deira", "bur dubai", "karama", "satwa", "discovery gardens",
    "silicon oasis", "motor city", "sports city", "business bay", "dip", "dic",
}

# A plot/villa/unit number shorthand like "P562", "V12", "S11", "R12" -
# a single letter directly followed by digits, common in UAE addresses but
# not caught by any word in ADDRESS_KEYWORDS (confirmed live: "Al wasl
# p562" fell through undetected). Checked as a regex, not a plain keyword,
# since a bare single letter would false-positive on ordinary words.
_PLOT_NUMBER_RE = re.compile(r"\b[a-z]\d{2,}\b", re.IGNORECASE)

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
- Your ONLY job is to help customers with items on this restaurant's menu and to place their order quickly. If a \
customer asks about anything unrelated (general knowledge, other businesses, coding, news, personal advice, etc.), \
politely say you can only help with the menu and orders, and steer back to what they'd like to order - in one short \
sentence, never engage with the off-topic request itself. For anything the menu context can't answer (e.g. a \
complaint, a refund, an allergy question, a large catering request), ask them to call the restaurant directly.
- Always be warm, polite, and respectful, even if the customer is short, impatient, or frustrated.
- If the customer's name is known (see "Customer name" below), greet/address them by it naturally once near the \
start of the conversation (e.g. "Welcome back, {{name}}!") - never ask for their name, WhatsApp already provides it. \
If it's not known, don't ask for it either; just proceed without using a name.
- Always reply in the same language the customer is writing in (e.g. Arabic, Hindi, Malayalam, Tamil, English) - if \
they switch languages mid-conversation, switch with them. Write the ENTIRE reply in that one language and script \
consistently - never mix scripts or languages within a single reply (e.g. don't blend Tamil and Bengali characters, \
or switch back to English mid-sentence). Default to English whenever the customer's message is in English, even if \
it contains a typo, informal spelling, or an unfamiliar word (e.g. "Chicken blriyani" is still English, just \
misspelled - reply in English, never switch to a different language based on a typo). Only switch away from English \
when the customer's message is clearly and substantially written in another language/script, not based on a single \
ambiguous word. If you're not fully confident in a non-English language, it's better to reply in clear, simple \
English than to risk a garbled, wrong-language, or mixed-script reply. Menu item names and prices stay as listed \
(Latin script/English) regardless of the reply language, since that's how they're listed in the system.
- Only offer/confirm items that appear in the menu context below, using their exact listed price and unit.
- If the menu context below lists categories instead of specific dishes (this happens when the customer asked a \
general "what's on the menu" question rather than naming a dish), briefly list those categories and ask which one \
interests them, or what dish they're in the mood for - don't claim the menu is unavailable, and don't invent dish \
names or prices before the customer narrows it down.
- If the customer asks for a whole group/category by name instead of one specific dish (e.g. "meals", "breakfast", \
"show me the starters") - the menu context below will then list every real item in that group with its price - \
list them out clearly (name + price, one per line or a short bulleted list) and ask which one(s) they'd like. Don't \
pick one for them, and don't add anything to the order yet (no ITEMS: line this turn) until they specify which \
item(s) from the list they want.
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
- If a customer asks for something not in the menu context at all (e.g. a snack, ice cream flavor, or dish that \
isn't listed anywhere, not even a close variant), don't refuse it outright - tell them you'll note it down and the \
restaurant will confirm if they can prepare it, then capture exactly what they asked for via a NOTE: line (see CART \
UPDATES below) so staff see it on the order and can follow up. Never invent a price for it or add it as a priced \
ITEMS line (there's no real catalog price to use) - it's recorded as a note only, and the order total only reflects \
the real catalog items actually added. If the menu context shows a close real variant (e.g. they ask for "Pista ice \
cream" but only "Mixed Ice Cream" is listed), mention that real option too so they can choose it instead if they'd \
rather not wait for confirmation - but still accept and note their original request if they want it anyway.
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
  - IMPORTANT: a shared location pin only tells us which building the customer is in, not which door - the app \
always asks a required follow-up for the door/apartment/villa number and landmark after a pin is shared, before the \
final total is shown. This happens automatically outside of your reply, so once you see a location pin or a message \
like "My door/unit number is: ..." in the conversation, treat the address as settled and move straight to the \
itemized total - never ask for the door number yourself, and never say the order is confirmed until the customer \
replies CONFIRM.
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
- CRITICAL: never tell the customer you've "added" something to their order unless you are ALSO writing a real \
ITEMS: ADD line for it this same turn - the two must always match. If you're asking a clarifying question (e.g. \
which biryani, which size) and haven't added anything yet, say so plainly and don't claim otherwise. Saying \
something was added when it wasn't is a serious error - it's better to ask again than to falsely confirm.
- Keep replies under 100 words unless summarizing a full order requires more.

CART UPDATES - read carefully, this is how items actually get added to the order:
Quantity: use EXACTLY the number the customer stated this turn, nothing else - "1 chicken biryani" means qty:1, \
"chicken biryani" with no number stated means qty:1 (never default to 2 or any other number), "2 chicken biryani" \
means qty:2. Re-read the customer's exact words before writing the qty value - do not round up, double, or guess a \
"typical" order size. If a number is genuinely ambiguous (e.g. unclear if it's a quantity or part of a dish name), \
ask a brief clarifying question instead of guessing.

After your reply to the customer, on a new line, add a line starting with exactly "ITEMS:" followed by one entry \
per item to ADD or REMOVE this turn, separated by semicolons. Format each entry as "ADD id:N qty:Q" or \
"REMOVE id:N qty:Q" (qty for REMOVE is how many to take off, not the new total), using the exact [id:N] shown \
either in the menu context below or the customer's current order below - NEVER invent an id, and NEVER use an id \
that isn't shown in one of those two places this turn. If the customer named something not in the menu context, do \
NOT emit an ITEMS line for it - just say it's unavailable in your reply (per the rule above). If nothing should be \
added or removed this turn (e.g. you're just answering a question, confirming/restating the order back to the \
customer without them asking for a change, or still waiting on a clarifying answer), write "ITEMS: none" - \
critically, NEVER re-emit ADD for an item that's already in "Customer's current order" below unless the customer is \
explicitly asking for MORE of it this turn, since re-adding it duplicates that line instead of just restating it, \
and can also incorrectly reopen an order that was already finalized for the customer to confirm. Only include an \
item once the customer has clearly confirmed exactly what they want (size/variant already resolved per the \
clarification-hints rule above) - don't add an item while a clarifying question about it is still open. This ITEMS \
line is never shown to the customer and must be the very last line of your response, nothing after it.

Customers can customize a dish by adding/removing/reducing an ingredient even when the menu doesn't list that as a \
separate option - e.g. "palak paneer without palak" (minus spinach), "chicken biryani without chicken" (treat as a \
request for a vegetable/plain version of that biryani, don't refuse it as contradictory), "tea without sugar", "less \
spicy", "no nuts" (allergy - always take these seriously and note them exactly). Never tell a customer an item isn't \
available just because they asked to modify an ingredient in it - add the item normally with ITEMS: as usual, and \
capture the modification with a NOTE: line so the kitchen sees it before preparing. Only ask a clarifying question if \
the request is genuinely ambiguous (e.g. unclear which of two items in the same message it applies to); otherwise \
just confirm warmly and move on, don't interrogate the customer about an ingredient swap.

If the customer gives a special preparation/handling request this turn that doesn't change WHAT they're ordering \
(e.g. "make it extra crispy", "no onions", "no sambar, extra red chutney", "less sugar", "ring the doorbell twice", \
or an ingredient customization as described above), OR asks for an item not in the menu context at all (see the \
rule above - e.g. "snacks" or an ice cream flavor that isn't listed) - something the kitchen/staff need to know, not \
something with a real catalog price - add a SECOND trailer line right \
after the ITEMS line, starting with exactly "NOTE:" followed by a short, clear instruction (your own words, not a \
quote). This gets attached to the order for the restaurant staff to see on the receipt/dashboard - acknowledge the \
request warmly in your reply same as you would anyway, but don't skip writing the NOTE line just because you \
already said you'd do it in the reply text, since that line is what actually saves it. Omit the NOTE line entirely \
(don't write "NOTE: none") if there's no new special request this turn. Example responses:
Sure! I've added 2 Chicken Biryani (Full) to your order, extra spicy as requested. Would you like a drink with that?
ITEMS: ADD id:482 qty:2
NOTE: Extra spicy

Noted - I'll pass along your request for vanilla ice cream, and the restaurant will confirm if they can prepare it. \
Anything else?
ITEMS: none
NOTE: Customer also requested: vanilla ice cream (not on menu - restaurant to confirm availability)

If the customer's message this turn contains delivery address information - either a FULL street address (e.g. \
"Al Wasl P562", "Villa 12 Jumeirah 3", "JVC S11 R12" - UAE addresses are often just an area/street name plus a plot \
or unit number, with no English address word like "street" or "villa" in them at all) when none has been given yet, \
OR a door/apartment/villa number and/or building name/landmark (they're answering "could you share your door/\
apartment/villa number" or similar) when a location pin was already shared - add a THIRD trailer line starting with \
exactly "ADDRESS:" followed by ONLY the clean address/location details extracted from their message - area/street \
name, plot/building name, room/flat/villa number, floor, landmark - nothing else. Strip out anything that isn't \
actually part of the address: if they also mention order items, say thanks, explain they already placed the order, \
or add other commentary in the same message, leave all of that out of the ADDRESS line entirely (that part of their \
message, e.g. an item change, is still handled normally via the ITEMS/NOTE lines - ADDRESS is only the pure location \
text). Examples: customer writes "I would like only one Ghee Masala Dosa. I have already placed the order. My \
building is Middle East Building, Room No. 305, 3rd Floor. Please deliver it to my room thank you" -> ADDRESS: \
Middle East Building, Room 305, 3rd Floor (not the whole message). Customer writes "Al wasl p562" as their delivery \
address -> ADDRESS: Al Wasl P562. Omit the ADDRESS line entirely if this turn's message has no address/door \
information in it.
- CRITICAL: if you just asked the customer to confirm a delivery address ("Deliver to X? Reply YES or share a new \
location") and they reply "No" (or similar - "nope", "wrong", "not that one"), that means the ADDRESS IS WRONG, not \
that they want to switch to pickup or dine-in - ask them to share the correct delivery address (by location or \
text), and do NOT change the order type or move toward a final total/confirmation until a correct address is given. \
Never interpret a plain "No" in response to an address question as a request to skip delivery.

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
            # [id:N] is required so generate_reply() can parse exact,
            # unambiguous ITEMS: action lines back out of the AI's reply -
            # never match by name text, which is how a wrong/hallucinated
            # item got added to a real customer's cart before this change.
            parts.append(f"- [id:{it['id']}] {it['name']}: {config.CURRENCY} {it['price']:.2f} - {availability}")
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
    lines = [
        f"- [id:{i['catalog_item_id']}] {i['qty']:g} x {i['item_name_snapshot']} @ "
        f"{config.CURRENCY} {i['unit_price_snapshot']:.2f} = {config.CURRENCY} {i['line_total']:.2f}"
        for i in items
    ]
    lines.append(f"Subtotal: {config.CURRENCY} {order['subtotal']:.2f}")
    return "\n".join(lines)


def _format_delivery_context(order: dict | None) -> str:
    if not order:
        return "(No active order.)"
    if order.get("order_type") == "dine_in":
        return "Dine-in order - no delivery fee, no location needed."
    if order.get("is_pickup"):
        return "Pickup order - no delivery fee, no location needed."
    # Checked delivery_address_text alongside delivery_lat - a customer who
    # TYPES their address (no location pin) has delivery_fee/total already
    # computed and stored by set_order_delivery_text, but this previously
    # only recognized a pin, so a typed-address order always fell through
    # to the generic "not yet decided" message below even after the
    # customer clearly chose delivery and gave a real address. Confirmed
    # live: the model then concluded (reasonably, given what it was shown)
    # that no delivery fee existed yet and told the customer to call the
    # restaurant to confirm it before checkout - completely blocking a
    # normal, correctly-computed order from ever being confirmed.
    if order.get("delivery_lat") is not None or order.get("delivery_address_text"):
        return (
            f"Delivery address received. Delivery fee: {config.CURRENCY} {order['delivery_fee']:.2f}, "
            f"Total: {config.CURRENCY} {order['total']:.2f} - this is the real, final delivery fee, already "
            f"computed and stored, not a placeholder - never say it's unknown or tell the customer to call and "
            f"confirm it."
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


# Generic group-browse words that aren't themselves real category names
# but clearly mean "show me that kind of item" - mapped to a substring
# matched against item NAMES (not search_items' name-or-category LIKE,
# which also pulls in unrelated items from categories that happen to
# contain the word elsewhere). "meals" -> "Meal" catches Snack/Jumbo/
# Family/Party/Boneless/Kids Meal (the Broasted category) plus the plain
# "Meals" item, without pulling in unrelated dishes.
_GROUP_BROWSE_NAME_SUBSTRINGS = {
    "meals": "meal",
    "meal": "meal",
}
_LEADING_QTY_RE_FOR_BROWSE = re.compile(r"^\d{1,2}\s+")


def _match_category_browse(message: str) -> list:
    """If the customer's message is a short, generic request for a whole
    category/group ("meals", "breakfast", "show me the tandoor items")
    rather than one specific dish, returns every real matching item
    cleanly (store.list_items_by_category or a name-substring match) -
    otherwise returns [] so the normal keyword search runs instead.
    Deliberately requires the message to be SHORT (<=3 words after
    stripping a leading quantity) so this doesn't misfire on a longer,
    more specific order that merely mentions a category word in passing."""
    lowered = message.strip().lower()
    lowered = _LEADING_QTY_RE_FOR_BROWSE.sub("", lowered).strip()
    words = lowered.split()
    if not words or len(words) > 3:
        return []

    for category in catalog_store.list_categories():
        if lowered == category.lower() or lowered in (f"{category.lower()}s", f"{category.lower()} items"):
            return catalog_store.list_items_by_category(category)

    for word, name_substring in _GROUP_BROWSE_NAME_SUBSTRINGS.items():
        if word in words:
            return [
                it for it in catalog_store.search_items(name_substring, limit=50)
                if name_substring in it["name"].lower()
            ]

    return []


def search_catalog_for_message(message: str, top_k: int = 16) -> list:
    """Very simple keyword-based menu matching: try the whole message, then
    each line (for multi-line orders like "Vanilla 2\nMango 1\nPista 1"),
    then individual significant words. Good enough for a single-restaurant
    menu of a few hundred items. top_k raised from 8 to 16 by default so a
    multi-line order doesn't get starved of context for later lines -
    generate_reply() is what actually grounds the AI's item choices, so
    under-including real candidates here risks the AI being unable to find
    a legitimate match even though the item exists. Always merges the
    whole-message attempt with per-line/per-word fallback (not just when
    the whole-message attempt finds nothing) - a message naming several
    distinct dishes (e.g. "2 set idli and 2 set dosa") can get a PARTIAL
    whole-message match (e.g. fuzzy-matching "dosa" but missing "idli"
    entirely, confirmed live), and stopping there would silently drop the
    other dish from the menu context the AI sees."""
    seen_ids = set()
    combined = []

    def _add(candidates):
        for item in candidates:
            if item["id"] not in seen_ids:
                seen_ids.add(item["id"])
                combined.append(item)

    _add(catalog_store.search_items(message, limit=top_k))

    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
    if len(lines) > 1:
        for line in lines:
            _add(catalog_store.search_items(line, limit=4))

    words = [w for w in re.findall(r"[a-zA-Z]{3,}", message) if w.lower() not in {"the", "and", "for", "with"}]
    for w in words:
        _add(catalog_store.search_items(w, limit=4))

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
    has_plot_number = bool(_PLOT_NUMBER_RE.search(lowered))
    return has_plot_number or (has_digit and has_address_word)


_ITEMS_LINE_RE = re.compile(r"^ITEMS:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ITEMS_ACTION_RE = re.compile(r"(ADD|REMOVE)\s+id:(\d+)\s+qty:(\d+(?:\.\d+)?)", re.IGNORECASE)
_NOTE_LINE_RE = re.compile(r"^NOTE:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_ADDRESS_LINE_RE = re.compile(r"^ADDRESS:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
# Catches a reply claiming something was added to the order/cart
# ("I've added 2 Chicken Dum Biryani to your order", "added to your cart",
# "have added X") - used only when there's NO ITEMS: line at all, to
# detect a hallucinated confirmation with nothing real behind it (see
# _parse_cart_actions). Deliberately requires "to your/the order/cart" or
# a number right after "added" to avoid false-triggering on an unrelated
# use of the word (e.g. "we added extra spice to our menu").
_CLAIMS_ADDED_RE = re.compile(
    r"\b(i'?ve|i have|have) added\b.{0,40}?\b(to (your |the )?(order|cart))\b"
    r"|\badded \d+\b",
    re.IGNORECASE | re.DOTALL,
)


def _parse_cart_actions(raw_reply: str, allowed_items: dict) -> tuple[str, list, str | None, str | None]:
    """Splits the AI's raw response into (customer_facing_text, actions,
    note, address). The ITEMS:/NOTE:/ADDRESS: trailer lines (see
    SYSTEM_PROMPT_TEMPLATE's "CART UPDATES" section) are stripped out
    entirely before anything is sent to the customer - they're
    machine-readable instructions to this code, never customer-visible.
    `allowed_items` is {id: item_dict} for exactly the catalog items shown
    to the model THIS turn (see generate_reply) - an action referencing any
    other id is dropped, so the model can never cause an item the customer
    didn't actually see offered to be added to their order, even if it
    hallucinates an id. `note` is the special-request text (e.g. "extra
    crispy", "no sambar, extra red chutney") to attach to the order via
    storage.store.add_order_note, or None if the model didn't include a
    NOTE: line this turn. `address` is the clean door/unit-number text the
    model extracted from the customer's message (see main.py's
    _apply_delivery_address_label), discarding any order-change/commentary
    text mixed into the same message - confirmed live that saving the raw
    customer message verbatim as the address produced an unreadable
    paragraph on the printed receipt instead of just "Room 305, 3rd
    Floor". None if the model didn't include an ADDRESS: line this turn."""
    match = _ITEMS_LINE_RE.search(raw_reply)
    if not match:
        text = raw_reply.strip()
        if _CLAIMS_ADDED_RE.search(text):
            # Hard guard, not just a prompt instruction: confirmed live that
            # the model can write a confident "I've added 2 Chicken Dum
            # Biryani to your order" sentence while completely omitting the
            # ITEMS: trailer line - no action was ever applied (verified
            # directly against the database: the order stayed empty), but
            # the customer was told a false confirmation and had no way to
            # know their item was never actually added. Since there's no
            # ITEMS: line to recover a real action from, replace the
            # hallucinated claim with an honest re-ask rather than pass it
            # through - telling the customer something was added when it
            # wasn't is worse than asking again.
            text = "Sorry, could you confirm exactly what you'd like to order? I want to make sure I get it right."
        return _sanitize_reply_text(text), [], None, None

    text = raw_reply[:match.start()].rstrip()
    actions = []
    for verb, id_str, qty_str in _ITEMS_ACTION_RE.findall(match.group(1)):
        item_id = int(id_str)
        qty = float(qty_str)
        if item_id not in allowed_items or qty <= 0:
            continue
        actions.append({"action": verb.upper(), "item": allowed_items[item_id], "qty": qty})

    note = None
    note_match = _NOTE_LINE_RE.search(raw_reply, match.end())
    if note_match:
        note_text = note_match.group(1).strip()
        if note_text and note_text.lower() != "none":
            note = note_text

    address = None
    address_match = _ADDRESS_LINE_RE.search(raw_reply, match.end())
    if address_match:
        address_text = address_match.group(1).strip()
        if address_text and address_text.lower() != "none":
            address = address_text

    return _sanitize_reply_text(text), actions, note, address


# Phrases that mean the model is narrating its own confusion/meta-state
# instead of writing a customer reply - confirmed live TWICE, from two
# different models (one paid, one a free reasoning-capable model despite
# the API's reasoning:exclude flag): "Wait! The assistant previous turn
# hallucinated/glitched..." and a multi-paragraph self-debugging essay
# ("I need to check what items are in the order... This is a problem - I
# claimed to add items but didn't actually add them... I think I need to
# proceed with the order summary...") that ran past WhatsApp's 4096-char
# message limit and got the send rejected outright - the customer got
# NOTHING, not even a fallback, since that failure happens one level
# below this filter (see main.py's _send hard length cap, the actual
# last line of defense). This can't be fixed by removing one model from
# the chain - it needs a content-level safety net that applies regardless
# of which model answers.
_META_COMMENTARY_RE = re.compile(
    r"\b(the assistant|previous turn|hallucinat\w*|glitch\w*|weird cut|as an ai|i am an ai language model"
    r"|let me check|i need to (check|add|verify)|this is a problem|i claimed to|i think i need to"
    r"|looking (back|more closely) at|menu context (says|shows)|according to the rules|the rules say)\b",
    re.IGNORECASE,
)
_SAFE_FALLBACK_REPLY = "Sorry, could you repeat that? I want to make sure I get your order right."
# Confirmed live: _format_delivery_context previously only recognized a
# shared location pin, not a typed-text address, as "delivery info
# received" - so a typed-address order always looked like the delivery
# fee was still unknown, and the model told the customer to call the
# restaurant to confirm it before checkout, completely blocking a
# normal, already-computed order from ever being confirmed. The prompt
# context is fixed (see _format_delivery_context), but this is also
# caught here as a hard guard, since compute_delivery_fee() is a
# deterministic function - the AI should never need to say this.
_FALSE_DELIVERY_FEE_CLAIM_RE = re.compile(
    r"\b(call the restaurant|confirm the delivery fee|don'?t have the delivery fee|delivery fee isn'?t available"
    r"|without the delivery fee)\b",
    re.IGNORECASE,
)
# A real customer reply is explicitly instructed to stay under ~100 words
# (SYSTEM_PROMPT_TEMPLATE's "Keep replies under 100 words" rule) - a reply
# many times that length is itself a strong signal of leaked internal
# reasoning even when it doesn't match a specific _META_COMMENTARY_RE
# phrase, since nothing in this domain legitimately needs a 1500+
# character reply.
_MAX_PLAUSIBLE_REPLY_LENGTH = 1500


def _sanitize_reply_text(text: str) -> str:
    """Last line of defense before a reply reaches the customer: catches
    meta-commentary leaking out of the model (narrating its own confusion).
    Confirmed live (from the PAID primary model, so this can't be fixed by
    removing a fallback model) that "the assistant previous turn
    hallucinated/glitched or the prompt had a weird cut" was sent straight
    to a real customer - that's the main thing this guards against.

    An earlier version also rejected any short reply whose last line
    didn't end in sentence punctuation, meant to catch cutoffs like a bare
    "Here" or a dangling bullet - but that heuristic was confirmed live to
    ALSO reject completely normal, correct replies ("Total: AED 24.00",
    "Perfect, confirmed for delivery to Al wasl p562 202", any reply
    ending on a price or address with no period), which broke the order
    flow worse than the truncation bug it was meant to fix (trapped the
    customer in a confirmation loop). Removed that check entirely - a
    missed truncation is a much smaller problem than blocking normal
    replies. Only an extremely short reply is still caught, since that's
    unambiguous regardless of ending punctuation."""
    if not text:
        return _SAFE_FALLBACK_REPLY
    if _META_COMMENTARY_RE.search(text):
        return _SAFE_FALLBACK_REPLY
    if _FALSE_DELIVERY_FEE_CLAIM_RE.search(text):
        return (
            "Your delivery fee is already calculated and included in your total - no need to call. "
            "Reply CONFIRM to place your order."
        )
    stripped = text.strip()
    if len(stripped) <= 6:
        # "Here", "Ok", "-" etc. - too short to be a real, complete reply
        # to anything in this domain.
        return _SAFE_FALLBACK_REPLY
    if len(stripped) > _MAX_PLAUSIBLE_REPLY_LENGTH:
        # Confirmed live: a multi-paragraph self-debugging essay (well over
        # 1500 chars) passed through here without matching any specific
        # _META_COMMENTARY_RE phrase, then exceeded WhatsApp's 4096-char
        # limit downstream and got the send rejected outright - the
        # customer got nothing. Catching the length here, upstream of that
        # failure, means a proper short apology goes out instead of either
        # a huge wall of text or total silence.
        return _SAFE_FALLBACK_REPLY
    return text


def generate_reply(customer_message: str, order: dict | None, order_items: list, customer: dict | None = None, history: list = None) -> tuple[str, list, str | None, str | None]:
    """Returns (reply_text, cart_actions, note, address) - cart_actions is a
    list of {"action": "ADD"|"REMOVE", "item": <catalog item dict>, "qty":
    float}, already validated against the catalog items shown to the model
    this turn; note is a special-request string to attach to the order (or
    None); address is a cleaned door/unit-number string extracted from the
    customer's message (or None) - see _parse_cart_actions. Replaces the old
    regex-based _apply_cart_updates in main.py entirely: the AI itself
    decides what to add/remove, grounded in the exact menu context it was
    shown, instead of a second independent guesser risking a different
    (possibly wrong) item from what the AI told the customer was added."""
    category_items = _match_category_browse(customer_message)
    if category_items:
        # The customer is asking for a whole category/group ("meals",
        # "breakfast") rather than naming one specific dish - a plain
        # keyword search mixes in unrelated items that happen to
        # fuzzy-match (confirmed live: "2 meals" / "2 veg meals" returned
        # noisy results including unrelated fish dishes, which the AI
        # couldn't cleanly resolve and silently failed to add anything).
        # Show exactly the real items in that group instead.
        catalog_items = category_items
    else:
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

    # Uses chat_completion's default max_tokens (1000, raised from 600) -
    # confirmed live that replies were truncating mid-sentence/mid-word
    # ("Here", a bare bullet "-", "If delivery, we can send it to") even on
    # the paid primary model, likely because some models spend part of
    # their token budget on an internal step before visible output starts.
    raw_reply = chat_completion(messages)
    # Also allow removing an item already in the cart even if this turn's
    # catalog search didn't happen to re-surface it (e.g. "remove the
    # appam" after the conversation moved on to other dishes).
    allowed_items = {it["id"]: it for it in catalog_items}
    for oi in order_items or []:
        cid = oi.get("catalog_item_id")
        if cid is not None and cid not in allowed_items:
            allowed_items[cid] = {
                "id": cid, "name": oi["item_name_snapshot"],
                "price": oi["unit_price_snapshot"], "in_stock": True,
            }
    text, actions, note, address = _parse_cart_actions(raw_reply, allowed_items)
    before = [dict(a) for a in actions]
    actions = _correct_single_add_quantity(customer_message, actions)
    logger.info(
        "generate_reply: customer_message=%r catalog_items_shown=%r raw_items_line=%r actions_before=%r actions_after=%r reply_text=%r",
        customer_message, [it["name"] for it in catalog_items],
        _ITEMS_LINE_RE.search(raw_reply).group(0) if _ITEMS_LINE_RE.search(raw_reply) else None,
        before, actions, text,
    )
    return text, actions, note, address


_LEADING_QTY_RE = re.compile(r"^(\d{1,2})\b")
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "single": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _extract_stated_quantity(message: str) -> int | None:
    """Pulls an unambiguous quantity straight from the customer's own words
    - a leading digit ("1 chicken biryani", "2 cokes") or number word ("a
    biryani", "two cokes"). Returns None if nothing clear is found, so the
    caller leaves the AI's own qty alone rather than guessing wrong."""
    lowered = message.strip().lower()
    digit_match = _LEADING_QTY_RE.match(lowered)
    if digit_match:
        return int(digit_match.group(1))
    first_word = lowered.split()[0] if lowered.split() else ""
    return _NUMBER_WORDS.get(first_word)


def _correct_single_add_quantity(customer_message: str, actions: list) -> list:
    """Hard override, not just a prompt instruction: a free-tier model has
    been confirmed live to ignore an explicit prompt rule and add the wrong
    quantity (customer said "1 chicken dum biriyani", model added qty 2,
    then repeated the same wrong qty even after the customer corrected it
    to "1 biriyani") - including by splitting one item into TWO separate
    ADD actions for the same id (e.g. "ADD id:482 qty:1; ADD id:482
    qty:1"), which _apply_cart_actions applies as two additions, still
    ending up at qty 2 even though each individual action said qty 1 -
    confirmed live as the actual mechanism behind a recurrence after the
    first single-action fix. First collapses multiple ADD actions for the
    same item into one (summing their qty), then applies the same
    stated-quantity override. Only applies when, after collapsing, there's
    exactly ONE ADD action and no other action this turn, and the
    customer's message clearly states a quantity - multi-item messages \
    ("2 biryani and 3 cokes") are left alone since a single leading number \
    can't be safely attributed to a specific item among several genuinely \
    different ones."""
    add_actions = [a for a in actions if a["action"] == "ADD"]
    other_actions = [a for a in actions if a["action"] != "ADD"]
    add_item_ids = {a["item"]["id"] for a in add_actions}
    if other_actions or len(add_item_ids) != 1:
        return actions

    collapsed_qty = sum(a["qty"] for a in add_actions)
    collapsed = dict(add_actions[0])
    collapsed["qty"] = collapsed_qty

    stated_qty = _extract_stated_quantity(customer_message)
    if stated_qty is not None and stated_qty != collapsed_qty:
        collapsed["qty"] = float(stated_qty)

    return [collapsed]


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
