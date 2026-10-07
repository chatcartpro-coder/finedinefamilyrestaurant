"""
Entry point. Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000

Expose this publicly (e.g. via a reverse proxy or a tunnel like ngrok during
development) and set the resulting URL as your webhook in Meta's App Dashboard
under WhatsApp > Configuration, together with WHATSAPP_VERIFY_TOKEN from .env.
"""
import logging
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from admin.routes import router as admin_router
from admin.temp_reset import router as temp_reset_router  # TEMP: remove after use, see admin/temp_reset.py
from ai.agent import (
    compute_delivery_fee, detect_confirmation_intent, detect_delivery_preference,
    detect_probable_address, detect_reuse_saved_address, generate_image_reply, generate_reply,
    is_accepting_orders, operating_hours_label,
)
from ai.voice import TranscriptionError, transcribe
from catalog import store as catalog_store
from config import config
from print_agent.routes import router as print_agent_router
from privacy_policy import PRIVACY_POLICY_HTML
from storage import store
from whatsapp.client import WhatsAppError, download_media, mark_as_read, send_text_message

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("finedine-agent")

app = FastAPI(title=f"{config.STORE_NAME} WhatsApp AI Agent")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.include_router(admin_router)
app.include_router(temp_reset_router)  # TEMP: remove after use, see admin/temp_reset.py
app.include_router(print_agent_router)


@app.exception_handler(HTTPException)
async def _admin_auth_redirect(request: Request, exc: HTTPException):
    # Any /admin/* route depends on get_current_admin, which raises 401 for a
    # missing/invalid session - send the browser to the login page instead of
    # showing a bare JSON error.
    if exc.status_code == 401 and request.url.path.startswith("/admin"):
        return RedirectResponse("/admin/login", status_code=303)
    # /print-agent/* is a headless JSON API client (the restaurant's print
    # script), not a browser - always return JSON, never the HTML fallback below.
    if request.url.path.startswith("/print-agent"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    return HTMLResponse(str(exc.detail), status_code=exc.status_code)


@app.get("/")
def health():
    return {"status": "ok", "service": "finedine-whatsapp-agent"}


@app.get("/privacy-policy", response_class=HTMLResponse)
def privacy_policy():
    return PRIVACY_POLICY_HTML


@app.on_event("startup")
def _log_openrouter_model_config():
    logger.info(
        "OpenRouter config: primary=%s fallbacks=%s",
        config.OPENROUTER_MODEL, config.OPENROUTER_FALLBACK_MODELS,
    )


@app.on_event("startup")
def _apply_store_settings_override():
    from config import apply_store_settings_override
    apply_store_settings_override()


@app.on_event("startup")
def _apply_whatsapp_connection_override():
    from config import apply_whatsapp_connection_override
    apply_whatsapp_connection_override()


@app.on_event("startup")
def _auto_import_catalog_if_empty():
    # Safety net for a brand-new/empty DB (e.g. Render's free web-service
    # plan, which has no persistent disk at all - every deploy/restart wipes
    # the filesystem). On a plan with a persistent disk (see render.yaml's
    # `disk` + DB_DATA_DIR), this only fires once, on the very first boot.
    import os
    from catalog.excel_import import import_file

    if catalog_store.is_empty() and os.path.exists(config.CATALOG_SEED_PATH):
        try:
            count = import_file(config.CATALOG_SEED_PATH, source="excel")
            logger.info("Auto-imported %d menu item(s) from seed file on startup", count)
        except Exception:
            logger.exception("Failed to auto-import menu seed on startup")


# ---- Meta webhook verification (GET) ----
@app.get("/webhook")
def verify_webhook(request: Request):
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")

    if mode == "subscribe" and token == config.WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="Verification failed", status_code=403)


# ---- Inbound messages (POST) ----
# Responds to Meta immediately and does the actual work (OpenRouter call,
# menu lookups, sending the reply) in a background task. Meta expects a
# fast response and will retry delivery of the same message otherwise -
# retries show up as duplicate message_ids, so dedupe before doing any work.
@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    logger.info("Inbound payload: %s", payload)

    try:
        entry = payload["entry"][0]
        change = entry["changes"][0]
        value = change["value"]

        # Coexistence-only events - all arrive under different top-level keys
        # than a normal customer message, so they must be checked before the
        # "messages" branch below or they'd silently fall through to
        # "ignored":
        #   - "history": the one-time chat-history backfill (up to 6 months)
        #     sent within 24h of a restaurant connecting via Embedded Signup
        #     with history-sync consent - threads[].messages[].
        #   - "smb_app_state_sync": the restaurant's WhatsApp Business app
        #     contact list (add/edit/delete), kept in sync as customers rows.
        #   - "smb_message_echoes": a live copy of a message the restaurant
        #     owner sent manually from the WhatsApp Business app (or a linked
        #     device), so the admin Conversations page shows the full
        #     back-and-forth regardless of which app replied.
        if "history" in value:
            background_tasks.add_task(_process_coexistence_history, value["history"])
            return {"status": "accepted"}

        if "smb_app_state_sync" in value:
            background_tasks.add_task(_process_coexistence_contact_sync, value["smb_app_state_sync"])
            return {"status": "accepted"}

        if "smb_message_echoes" in value:
            for echo in value["smb_message_echoes"].get("messages", []):
                background_tasks.add_task(_process_message_echo, echo)
            return {"status": "accepted"}

        if "messages" not in value:
            return {"status": "ignored"}

        message = value["messages"][0]
        from_number = message["from"]
        message_id = message["id"]
        msg_type = message.get("type", "text")

    except (KeyError, IndexError) as e:
        logger.warning("Unrecognized payload shape: %s", e)
        return {"status": "ignored"}

    if store.already_processed(message_id):
        logger.info("Skipping already-processed message_id=%s (Meta retry)", message_id)
        return {"status": "duplicate_ignored"}
    store.mark_processed(message_id)

    # Meta sends the customer's own WhatsApp profile name alongside every
    # message (value.contacts[0].profile.name) - capture it for free so the
    # AI can greet returning customers by name without ever having to ask,
    # same as the saved-address flow. upsert_customer's COALESCE means this
    # never overwrites an existing name with a missing one.
    profile_name = None
    try:
        profile_name = value["contacts"][0]["profile"]["name"]
    except (KeyError, IndexError, TypeError):
        pass
    if profile_name:
        store.upsert_customer(from_number, name=profile_name)

    if msg_type == "text":
        text = message.get("text", {}).get("body", "").strip()
        if not text:
            return {"status": "ignored_non_text"}
        background_tasks.add_task(_process_text_message, from_number, text, message_id)

    elif msg_type == "location":
        location = message.get("location", {})
        background_tasks.add_task(
            _process_location_message, from_number, location.get("latitude"),
            location.get("longitude"), location.get("name") or location.get("address"), message_id,
        )

    elif msg_type == "image":
        image = message.get("image", {})
        background_tasks.add_task(
            _process_image_message, from_number, image.get("id"), image.get("mime_type", "image/jpeg"),
            image.get("caption", ""), message_id,
        )

    elif msg_type == "audio":
        audio = message.get("audio", {})
        background_tasks.add_task(
            _process_audio_message, from_number, audio.get("id"), audio.get("mime_type", "audio/ogg"), message_id,
        )

    else:
        logger.info("Ignoring unsupported message type '%s' from %s", msg_type, from_number)
        return {"status": "ignored_unsupported_type"}

    return {"status": "accepted"}


def _process_text_message(phone: str, text: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    try:
        # Route delivery agents to their own flow before anything else - a
        # registered agent's messages should never be treated as a customer
        # placing a food order.
        if store.get_delivery_agent_by_phone(phone):
            handle_delivery_agent_message(phone, text)
            return
        handle_customer_message(phone, text)
    except Exception:
        logger.exception("Failed to handle message from %s", phone)
        _send(phone, f"Sorry, I'm having trouble responding right now. Please try again in a moment, or call the restaurant at {config.STORE_PHONE or 'our number'}.")


def _process_location_message(phone: str, lat, lng, label: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    if lat is None or lng is None:
        _send(phone, "Sorry, I couldn't read that location. Could you try sharing it again?")
        return

    store.log_message(phone, "in", f"[location] {lat},{lng}" + (f" {label}" if label else ""))
    store.set_customer_location(phone, lat, lng, label)

    order = store.get_active_order(phone)
    if not order or not store.get_order_items(order["id"]):
        _send(phone, "Got your location! Let me know what you'd like to order and I'll get started.")
        return

    _apply_delivery_location(phone, order, lat, lng, label)


def _apply_delivery_location(phone: str, order: dict, lat: float, lng: float, label: str):
    """A shared location pin only places a rider at a building, not a
    specific unit - always require a door/apartment/villa number as text
    before moving to confirmation (never let a pin alone satisfy delivery
    info), so the printed/WhatsApp receipt and the delivery agent's
    notification always have a readable address, not just a map link."""
    delivery_fee = compute_delivery_fee(order["subtotal"])
    # Pass address_text=None (not the WhatsApp pin's own label, e.g. "Home")
    # so delivery_address_text stays empty and needs_door_number in
    # handle_customer_message correctly still asks for the door number.
    store.set_order_delivery(order["id"], lat, lng, delivery_fee, None)
    order = store.get_order(order["id"])
    _send(
        phone,
        "Got your location! One more thing - could you share your door/apartment/villa number and any landmark, "
        "so the rider can find you exactly? (e.g. \"Villa 12, near the mosque\" or \"Flat 304, Marina Tower\")",
    )


def _apply_delivery_address_label(phone: str, order: dict, address_text: str):
    """Follow-up to _apply_delivery_location: attaches the door/unit number
    text the customer sends after sharing a pin, WITHOUT touching the
    coordinates already saved (see storage.store.set_order_delivery_address_label).
    This is what actually unblocks needs_delivery_address and moves the
    order to awaiting_confirmation - a pin by itself never does."""
    store.set_order_delivery_address_label(order["id"], address_text)
    store.set_customer_address_text(phone, address_text)
    order = store.get_order(order["id"])
    store.set_order_status(order["id"], "awaiting_confirmation")

    customer = store.get_customer(phone)
    order_items = store.get_order_items(order["id"])
    reply, actions, note = generate_reply(
        f"My door/unit number is: {address_text}", order, order_items,
        customer=customer, history=store.get_recent_history(phone),
    )
    order = _apply_cart_actions(phone, order, actions)
    order = _apply_order_note(phone, order, note)
    escalation_note = ""
    if "confirm" not in reply.lower():
        escalation_note = (
            f"\n\nTotal: {config.CURRENCY} {order['total']:.2f} (includes {config.CURRENCY} {order['delivery_fee']:.2f} delivery). "
            "Reply CONFIRM to place this order or CANCEL to change it."
        )
    _send(phone, reply + escalation_note)


def _apply_delivery_text_address(phone: str, order: dict, address_text: str):
    """Sibling to _apply_delivery_location for a customer who typed their
    address instead of sharing a WhatsApp location pin - same flow, no
    coordinates involved. Also updates the customer's saved-address label and
    last_address_text so a future order can offer to reuse it, the same way a
    shared-location address would be (existing saved lat/lng, if any, are
    left untouched - upsert_customer's COALESCE means this only overwrites
    the label, so a customer who later shares real coordinates against a
    different address won't have this stale text label silently attached to
    them)."""
    store.upsert_customer(phone, label=address_text)
    store.set_customer_address_text(phone, address_text)
    delivery_fee = compute_delivery_fee(order["subtotal"])
    store.set_order_delivery_text(order["id"], address_text, delivery_fee)
    order = store.get_order(order["id"])
    store.set_order_status(order["id"], "awaiting_confirmation")

    customer = store.get_customer(phone)
    order_items = store.get_order_items(order["id"])
    reply, actions, note = generate_reply(
        f"I'll deliver to this address: {address_text}", order, order_items,
        customer=customer, history=store.get_recent_history(phone),
    )
    order = _apply_cart_actions(phone, order, actions)
    order = _apply_order_note(phone, order, note)
    escalation_note = ""
    if "confirm" not in reply.lower():
        escalation_note = (
            f"\n\nTotal: {config.CURRENCY} {order['total']:.2f} (includes {config.CURRENCY} {order['delivery_fee']:.2f} delivery). "
            "Reply CONFIRM to place this order or CANCEL to change it."
        )
    _send(phone, reply + escalation_note)


def _process_audio_message(phone: str, media_id: str, mime_type: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    if not media_id:
        _send(phone, "Sorry, I couldn't receive that voice note. Could you try sending it again?")
        return

    try:
        audio_bytes = download_media(media_id)
        transcript = transcribe(audio_bytes, mime_type)
    except (WhatsAppError, TranscriptionError):
        logger.exception("Voice transcription failed for %s", phone)
        store.log_message(phone, "in", "[voice note - transcription failed]")
        _send(phone, "Sorry, I couldn't understand that voice note. Could you please type your order instead?")
        return

    if not transcript.strip():
        store.log_message(phone, "in", "[voice note - empty transcript]")
        _send(phone, "Sorry, I couldn't catch that. Could you please type your order instead?")
        return

    store.log_message(phone, "in", f"[voice] {transcript}")
    try:
        if store.get_delivery_agent_by_phone(phone):
            handle_delivery_agent_message(phone, transcript, already_logged=True)
            return
        handle_customer_message(phone, transcript, already_logged=True)
    except Exception:
        logger.exception("Failed to handle transcribed voice message from %s", phone)
        _send(phone, f"Sorry, I'm having trouble responding right now. Please try again in a moment, or call the restaurant at {config.STORE_PHONE or 'our number'}.")


def _process_image_message(phone: str, media_id: str, mime_type: str, caption: str, message_id: str):
    try:
        mark_as_read(message_id)
    except WhatsAppError as e:
        logger.warning("mark_as_read failed: %s", e)

    store.log_message(phone, "in", f"[image]{(' ' + caption) if caption else ''}")

    if not media_id:
        _send(phone, "Sorry, I couldn't receive that image. Could you try sending it again?")
        return

    try:
        image_bytes = download_media(media_id)
        reply = generate_image_reply(image_bytes, mime_type, caption=caption)
        _send(phone, reply)
    except Exception:
        logger.exception("Failed to handle image from %s", phone)
        _send(phone, "Sorry, I'm having trouble looking at that image right now. Could you describe the dish in words instead?")


# ---- Coexistence sync handlers ----
# Only ever invoked for a restaurant connected via Embedded Signup with
# Coexistence (see whatsapp/coexistence.py, admin/routes.py's /admin/whatsapp
# routes) - a .env-configured, non-Coexistence number never triggers these,
# since Meta only sends these webhook fields for Coexistence connections.

def _process_coexistence_history(history: dict):
    """One-time backfill of up to 6 months of chat history, synced from the
    restaurant's existing WhatsApp Business app after they connect via
    Embedded Signup. Written straight into customers/conversations (not
    staged) so it shows up immediately in the admin Conversations page,
    same as live chat history."""
    threads = history.get("threads", [])
    imported = 0
    for thread in threads:
        # Each thread is the full history with one customer - its id is that
        # customer's phone number, regardless of which side sent any given
        # message within it.
        customer_phone = thread.get("id")
        if not customer_phone:
            continue

        for msg in thread.get("messages", []):
            message_id = msg.get("id")
            if message_id and store.already_processed(message_id):
                continue
            if message_id:
                store.mark_processed(message_id)

            # A message "from" the customer's own number is inbound; a
            # message "from" anything else (the restaurant's business
            # number) is outbound - i.e. one the restaurant sent, whether
            # via this bot historically or by typing it in the WhatsApp
            # Business app.
            direction = "in" if msg.get("from") == customer_phone else "out"
            text = msg.get("text", {}).get("body") if isinstance(msg.get("text"), dict) else None
            text = text or f"[{msg.get('type', 'message')}]"
            timestamp = msg.get("timestamp")
            created_at = None
            if timestamp:
                try:
                    created_at = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
                except (ValueError, TypeError):
                    created_at = None

            store.upsert_customer(customer_phone)
            store.log_message(customer_phone, direction, text, created_at=created_at)
            imported += 1

    store.set_whatsapp_history_sync_status("complete")
    logger.info("Coexistence history sync: imported %d message(s) across %d thread(s)", imported, len(threads))


def _process_coexistence_contact_sync(sync: dict):
    """The restaurant owner's WhatsApp Business app contact list (added/
    edited/removed), kept as customers rows so saved names show up even for
    a customer who hasn't messaged the bot yet."""
    for entry in sync.get("state_sync", []):
        contact = entry.get("contact") or {}
        phone = contact.get("phone_number") or contact.get("wa_id")
        if not phone:
            continue
        action = entry.get("action", "add")
        if action == "remove":
            continue  # customers/conversations history is kept even if unfriended in the app
        name = contact.get("full_name") or contact.get("first_name")
        store.upsert_customer(phone, name=name)


def _process_message_echo(echo: dict):
    """A message the restaurant owner sent manually from the WhatsApp
    Business app (or a linked device) after connecting via Coexistence -
    mirrored here purely for the admin Conversations page's record; the bot
    takes no further action on it (it's an outbound message, not something
    needing an AI reply)."""
    message_id = echo.get("id")
    if message_id:
        if store.already_processed(message_id):
            return
        store.mark_processed(message_id)

    customer_phone = echo.get("to")
    if not customer_phone:
        return
    text = echo.get("text", {}).get("body") if isinstance(echo.get("text"), dict) else None
    text = text or f"[{echo.get('type', 'message')} sent from WhatsApp Business app]"
    store.upsert_customer(customer_phone)
    store.log_message(customer_phone, "out", text)


# ---- Customer-facing order flow ----

def handle_customer_message(phone: str, text: str, already_logged: bool = False):
    if not already_logged:
        store.log_message(phone, "in", text)

    order = store.get_active_order(phone)

    # If we're waiting on an explicit confirm/cancel, check that first so a
    # stray "yes" never gets routed into general order-building logic.
    if order and order["status"] == "awaiting_confirmation":
        intent = detect_confirmation_intent(text)
        if intent == "confirm":
            if not is_accepting_orders():
                _send(
                    phone,
                    f"Sorry, {config.STORE_NAME} isn't accepting orders right now - we're open "
                    f"{operating_hours_label()}. Your order is saved; just reply CONFIRM once we're open "
                    "and I'll place it for you!",
                )
                return
            # Hard guard, not just a prompt instruction: a delivery order
            # must have a door/unit number (delivery_address_text) before it
            # can actually be confirmed - confirmed live that an order could
            # otherwise reach "awaiting_confirmation" via the AI-driven reply
            # path (ai/agent.py's conversational prompt-only instruction)
            # with delivery_lat/delivery_address_text still both empty,
            # producing a receipt/printout with no address at all. This
            # can't rely on the prompt alone since the model doesn't always
            # follow it under multi-turn/multi-item pressure.
            is_delivery_order = order.get("order_type") not in ("pickup", "dine_in") and not order.get("is_pickup")
            if is_delivery_order and order.get("delivery_lat") is None and not order.get("delivery_address_text"):
                _send(
                    phone,
                    "Before I place this order - where should we deliver it? Share your location (paperclip -> "
                    "Location) or just type your delivery address, including door/apartment/villa number.",
                )
                return
            if is_delivery_order and order.get("delivery_lat") is not None and not order.get("delivery_address_text"):
                # A pin was shared but the door/unit number follow-up never
                # landed (e.g. the customer typed "confirm" instead of a
                # door number) - still can't confirm without it.
                _send(
                    phone,
                    "Almost there - could you share your door/apartment/villa number and any landmark so the rider "
                    "can find you exactly?",
                )
                return
            _confirm_order(phone, order)
            return
        if intent == "cancel":
            store.set_order_status(order["id"], "draft")
            _send(phone, "No problem, order not placed yet. What would you like to change?")
            return
        # Anything else while awaiting confirmation: let the AI handle it
        # (e.g. "can you add a drink too") but keep status as-is; it stays
        # awaiting_confirmation until an explicit confirm/cancel arrives.

    # Once there's an order with items and no delivery/pickup/dine-in
    # decision yet (or delivery was chosen but no address - by location OR
    # text - has landed): a "pickup" or "dine in" reply short-circuits
    # straight to final confirmation (no location needed either way); a
    # "yes, same address" reply reuses the customer's saved location instead
    # of waiting for a fresh share; a message that reads like a typed
    # address is accepted directly as the delivery address text, no
    # coordinates required. Anything else (including "delivery") falls
    # through to the normal AI reply below, whose system prompt already
    # knows to ask for/confirm a delivery address (by location or text).
    needs_delivery_address = (
        order and store.get_order_items(order["id"]) and not order.get("is_pickup")
        and order.get("delivery_lat") is None and not order.get("delivery_address_text")
    )
    # A pin was shared (or reused) but the door/unit number follow-up hasn't
    # landed yet - the order is still not ready for confirmation, so route
    # the customer's very next message as that door number rather than
    # letting it fall through to the generic AI reply below.
    needs_door_number = (
        order and store.get_order_items(order["id"]) and not order.get("is_pickup")
        and order.get("delivery_lat") is not None and not order.get("delivery_address_text")
    )
    if needs_door_number:
        _apply_delivery_address_label(phone, order, text)
        return
    if needs_delivery_address:
        preference = detect_delivery_preference(text)
        if preference == "pickup":
            store.set_order_pickup(order["id"])
            order = store.get_order(order["id"])
            _prompt_final_confirmation(phone, order)
            return
        if preference == "dine_in":
            store.set_order_dine_in(order["id"])
            order = store.get_order(order["id"])
            _prompt_final_confirmation(phone, order)
            return

        customer = store.get_customer(phone)
        has_saved_address = customer and (customer.get("last_lat") is not None or customer.get("last_address_text"))
        if has_saved_address and detect_reuse_saved_address(text):
            if customer.get("last_lat") is not None:
                # Reusing a saved pin still needs a door number confirmed -
                # if we also have a saved text label from a past order, skip
                # re-asking and attach it directly; otherwise ask again.
                _apply_delivery_location(phone, order, customer["last_lat"], customer["last_lng"], customer.get("last_location_label"))
                if customer.get("last_address_text"):
                    order = store.get_order(order["id"])
                    _apply_delivery_address_label(phone, order, customer["last_address_text"])
            else:
                _apply_delivery_text_address(phone, order, customer["last_address_text"])
            return

        if detect_probable_address(text):
            _apply_delivery_text_address(phone, order, text)
            return

    order_items = store.get_order_items(order["id"]) if order else []
    customer = store.get_customer(phone)
    history = store.get_recent_history(phone, limit=10)
    reply, actions, note = generate_reply(text, order, order_items, customer=customer, history=history)

    order = _apply_cart_actions(phone, order, actions)
    _apply_order_note(phone, order, note)

    _send(phone, reply)


def _prompt_final_confirmation(phone: str, order: dict):
    """Used for both pickup and dine-in - same no-delivery-fee flow, just a
    different synthetic "customer message" fed to the AI and a different
    escalation-note label, driven by order_type (set by set_order_pickup/
    set_order_dine_in right before this is called)."""
    items = store.get_order_items(order["id"])
    store.set_order_status(order["id"], "awaiting_confirmation")
    is_dine_in = order.get("order_type") == "dine_in"
    synthetic_message = "I'll dine in at the restaurant." if is_dine_in else "I'll pick it up myself."
    label = "dine-in - no delivery fee" if is_dine_in else "pickup - no delivery fee"
    reply, actions, note = generate_reply(synthetic_message, order, items, customer=store.get_customer(phone), history=store.get_recent_history(phone))
    order = _apply_cart_actions(phone, order, actions) or order
    order = _apply_order_note(phone, order, note) or order
    # _apply_cart_actions reverts an awaiting_confirmation order back to
    # draft if it changes the cart (see its docstring) - this function's
    # whole point is presenting the final total for confirmation, so put it
    # back to awaiting_confirmation regardless (synthetic messages here
    # shouldn't normally trigger cart actions, but stay correct if one does).
    if order["status"] != "awaiting_confirmation":
        store.set_order_status(order["id"], "awaiting_confirmation")
        order = store.get_order(order["id"])
    escalation_note = ""
    if "confirm" not in reply.lower():
        escalation_note = (
            f"\n\nTotal: {config.CURRENCY} {order['total']:.2f} ({label}). "
            "Reply CONFIRM to place this order or CANCEL to change it."
        )
    _send(phone, reply + escalation_note)


def _confirm_order(phone: str, order: dict):
    store.set_order_status(order["id"], "confirmed")
    order = store.get_order(order["id"])
    items = store.get_order_items(order["id"])
    _send(phone, _format_whatsapp_receipt(order, items))

    _push_invoice_best_effort(order, items)

    if order.get("is_pickup"):
        return  # nothing to hand off to a delivery agent

    agent = store.get_next_available_delivery_agent()
    if not agent:
        logger.warning("Order #%s confirmed for delivery but no active delivery agent is registered", order["id"])
        return

    store.assign_delivery_agent(order["id"], agent["phone"])
    _notify_delivery_agent(agent["phone"], order, items)


def _push_invoice_best_effort(order: dict, items: list):
    """Fire-and-forget call into billing/connector.py - never blocks or
    fails the customer-facing confirmation flow. No-ops (just logs) until a
    real billing vendor is wired in."""
    if not config.BILLING_API_BASE_URL:
        return  # not configured - skip silently, no log noise on every order
    from billing.connector import BillingNotConfiguredError, push_order_invoice
    try:
        push_order_invoice(order, items)
    except BillingNotConfiguredError:
        logger.info("Billing connector not configured; skipping invoice push for order #%s", order["id"])
    except Exception:
        logger.exception("Billing invoice push failed for order #%s (non-fatal)", order["id"])




def _apply_cart_actions(phone: str, order: dict | None, actions: list) -> dict | None:
    """Applies the ADD/REMOVE actions the AI returned alongside its reply
    (see ai/agent.py's generate_reply + CART UPDATES prompt section).
    Replaces the old regex-based item guesser entirely: the AI already
    decided exactly what to add/remove, grounded in the precise menu
    context it was shown and validated server-side against those same ids
    (ai/agent.py's _parse_cart_actions) - this function's only job is to
    apply those decisions to the database, with no guessing of its own.
    Returns the (possibly newly-created) order, or the original order if no
    actions were given.

    A REMOVE for an item not actually in the cart, or an ADD that's
    rejected (out of stock), is a genuine no-op and must NOT revert an
    awaiting_confirmation order back to draft - confirmed live: the AI can
    emit an action referencing an item already fully accounted for (e.g.
    restating the settled order back in a reply that also answers an
    unrelated question), and reverting status for a no-op meant the
    customer's subsequent "CONFIRM" no longer matched any
    awaiting_confirmation order, so it silently fell through to a generic
    AI reply instead of actually placing the order - the order stayed
    'draft' forever despite the customer confirming."""
    if not actions:
        return order

    for change in actions:
        item = change["item"]
        qty = change["qty"]

        if change["action"] == "ADD" and not item.get("in_stock", True):
            continue  # rejected outright - never touches order status

        if order and order["status"] == "awaiting_confirmation":
            if change["action"] == "REMOVE":
                # No matching line to remove -> genuine no-op, skip without
                # touching status (see docstring).
                has_match = any(i["catalog_item_id"] == item["id"] for i in store.get_order_items(order["id"]))
                if not has_match:
                    continue
            else:  # ADD
                # Once an order is awaiting_confirmation, a duplicate ADD for
                # an item already at the same-or-higher quantity is always a
                # restatement, not a real addition - the customer would have
                # to explicitly ask for more for the prompt to emit a higher
                # qty than what's already there. Suppressing this is what
                # stops a no-op ADD from reopening an order the customer is
                # about to confirm (see docstring) - a genuine "add 2 more"
                # still goes through since its qty exceeds the existing line.
                existing_qty = sum(
                    i["qty"] for i in store.get_order_items(order["id"])
                    if i["catalog_item_id"] == item["id"]
                )
                if existing_qty >= qty:
                    continue

        if not order or order["status"] not in ("draft", "awaiting_confirmation"):
            order_id = store.create_order(phone)
            order = store.get_order(order_id)
        elif order["status"] == "awaiting_confirmation":
            # A real change is about to be applied - revert to draft so the
            # total gets recalculated before we ask for confirmation again.
            store.set_order_status(order["id"], "draft")
            order = store.get_order(order["id"])

        if change["action"] == "ADD":
            store.add_order_item(order["id"], item["id"], item["name"], item["price"], qty)
        else:  # REMOVE
            existing = [i for i in store.get_order_items(order["id"]) if i["catalog_item_id"] == item["id"]]
            remaining = qty
            for row in existing:
                if remaining <= 0:
                    break
                if row["qty"] <= remaining:
                    store.remove_order_item(row["id"])
                    remaining -= row["qty"]
                else:
                    store.set_order_item_qty(row["id"], row["qty"] - remaining)
                    remaining = 0

    return store.get_order(order["id"]) if order else None


def _apply_order_note(phone: str, order: dict | None, note: str | None) -> dict | None:
    """Attaches a special-request note the AI picked up this turn (see
    ai/agent.py's generate_reply + CART UPDATES prompt section) to the
    active order, creating a draft order first if there isn't one yet (a
    customer can give a prep instruction before naming any items, e.g.
    "make sure it's not too spicy" as their very first message). Unlike
    _apply_cart_actions, attaching a note never reverts an
    awaiting_confirmation order back to draft - it's metadata for the
    kitchen/rider, not a change to what's being charged, so it shouldn't
    reopen a total the customer is about to confirm."""
    if not note:
        return order
    if not order or order["status"] not in ("draft", "awaiting_confirmation"):
        order_id = store.create_order(phone)
        order = store.get_order(order_id)
    store.add_order_note(order["id"], note)
    return store.get_order(order["id"])


def _format_whatsapp_receipt(order: dict, items: list) -> str:
    """Itemized order confirmation, sent to the customer over WhatsApp the
    moment their order is confirmed. Prices are VAT-inclusive (see
    config.vat_breakdown) - the total never changes, this just breaks the
    VAT amount back out for the customer's records."""
    from config import vat_breakdown

    lines = [f"{config.STORE_NAME}", f"Order {store.order_ref(order)} - confirmed", f"Order type: {store.order_type_label(order)}", ""]

    for item in items:
        qty = item["qty"]
        qty_str = f"{qty:g}" if isinstance(qty, float) else str(qty)
        lines.append(f"{qty_str} x {item['item_name_snapshot']}")
        lines.append(f"  {config.CURRENCY} {item['unit_price_snapshot']:.2f} = {config.CURRENCY} {item['line_total']:.2f}")

    lines.append("")
    lines.append(f"Subtotal: {config.CURRENCY} {order['subtotal']:.2f}")
    if not order.get("is_pickup"):
        lines.append(f"Delivery: {config.CURRENCY} {order['delivery_fee']:.2f}")
    if order.get("discount_applied"):
        lines.append("Discount applied")
    lines.append(f"Total: {config.CURRENCY} {order['total']:.2f}")
    excl_vat, vat_amount = vat_breakdown(order["total"])
    lines.append(f"(incl. VAT {config.CURRENCY} {vat_amount:.2f} - amount excl. VAT: {config.CURRENCY} {excl_vat:.2f})")

    if order.get("order_type") == "dine_in":
        lines.append("")
        lines.append("This is a dine-in order - please head to the restaurant, your order will be prepared for you.")
    elif order.get("is_pickup"):
        lines.append("")
        lines.append("This is a pickup order - please collect it from the restaurant.")
    elif order.get("delivery_address_text") or order.get("delivery_lat") is not None:
        # A customer can share their address two ways - a typed text address
        # (delivery_address_text) or a WhatsApp location pin (delivery_lat/
        # lng, with delivery_address_text only set if they also gave it a
        # label). Previously this only checked delivery_address_text, so a
        # pin-shared address with no label silently never appeared on the
        # receipt at all - confirmed live (receipt showed no address despite
        # a real location pin being shared and confirmed in the chat).
        lines.append("")
        if order.get("delivery_address_text"):
            lines.append(f"Deliver to: {order['delivery_address_text']}")
        if order.get("delivery_lat") is not None:
            lines.append(f"Map: https://maps.google.com/?q={order['delivery_lat']},{order['delivery_lng']}")

    if order.get("notes"):
        lines.append("")
        lines.append(f"Notes: {order['notes']}")

    lines.append("")
    lines.append(f"Thank you for ordering from {config.STORE_NAME}!")
    return "\n".join(lines)


# ---- Delivery agent flow ----
# A registered delivery agent (storage.store.delivery_agents) drives their
# assigned order through packed -> picked_up -> delivered by replying with
# these exact keywords. This is a separate conversational flow from the
# customer-facing one - main.py::_process_text_message routes here first if
# the sender is a known agent.

_AGENT_STATUS_KEYWORDS = {
    "packed": "packed",
    "picked": "picked_up",
    "picked up": "picked_up",
    "delivered": "delivered",
}

# Optional enrichment, not part of the status lifecycle above - a delivery
# agent can send this any time after an order is packed (card-machine
# payment taken on drop-off), for card payments only. Never required: a
# cash-on-delivery order simply never gets this and payment_status stays
# NULL forever - nothing downstream gates on it, so PACKED -> PICKED ->
# DELIVERED works identically whether or not PAID was ever sent.
# Future: a real card-machine webhook/SDK callback (or billing/connector.py's
# sync_payment_status()) could set payment_status programmatically instead
# of relying on this WhatsApp keyword - a strictly additive change, since
# payment_status is already treated as an independent, optional field.
_AGENT_PAYMENT_KEYWORDS = {
    "paid": "paid",
}


def handle_delivery_agent_message(phone: str, text: str, already_logged: bool = False):
    if not already_logged:
        store.log_message(phone, "in", text)

    lowered = text.strip().lower()

    order = store.get_order_assigned_to_agent(phone)
    if not order:
        _send(phone, "You don't have an active delivery order right now.")
        return

    payment_status = _AGENT_PAYMENT_KEYWORDS.get(lowered)
    if payment_status:
        if order["status"] not in ("packed", "picked_up", "delivered"):
            _send(phone, f"Order {store.order_ref(order)} hasn't been packed yet - mark PACKED first.")
            return
        store.set_order_payment_status(order["id"], payment_status)
        _send(phone, f"Order {store.order_ref(order)} marked as paid. Thanks!")
        return

    new_status = _AGENT_STATUS_KEYWORDS.get(lowered)
    if not new_status:
        _send(phone, "Reply PACKED once the kitchen has the order ready, PICKED once you've collected it, PAID once payment is taken (card machine), or DELIVERED once it's dropped off.")
        return

    # Enforce the lifecycle order so a mistyped reply can't skip a stage.
    valid_next = {"confirmed": "packed", "packed": "picked_up", "picked_up": "delivered"}
    if valid_next.get(order["status"]) != new_status:
        _send(phone, f"Order {store.order_ref(order)} is currently '{order['status']}' - that update doesn't apply yet.")
        return

    store.set_order_status(order["id"], new_status)
    _send(phone, f"Order {store.order_ref(order)} marked as {new_status.replace('_', ' ')}. Thanks!")

    if new_status == "picked_up":
        _send(order["phone"], "Your order is on its way!")
    elif new_status == "delivered":
        _send(order["phone"], f"Your order has been delivered. Enjoy your meal! Thank you for ordering from {config.STORE_NAME}.")


def _notify_delivery_agent(agent_phone: str, order: dict, items: list):
    lines = [f"New delivery order - {store.order_ref(order)}", ""]
    for item in items:
        qty = item["qty"]
        qty_str = f"{qty:g}" if isinstance(qty, float) else str(qty)
        lines.append(f"{qty_str} x {item['item_name_snapshot']}")
    lines.append("")
    lines.append(f"Total: {config.CURRENCY} {order['total']:.2f}")
    lines.append(f"Customer: {order['phone']}")
    if order.get("delivery_address_text"):
        lines.append(f"Deliver to: {order['delivery_address_text']}")
    if order.get("delivery_lat") is not None:
        lines.append(f"Map: https://maps.google.com/?q={order['delivery_lat']},{order['delivery_lng']}")
    lines.append("")
    lines.append("Reply PACKED once ready, PICKED once collected, PAID if you take a card payment, DELIVERED once dropped off.")
    _send(agent_phone, "\n".join(lines))


def _send(phone: str, message: str):
    try:
        send_text_message(phone, message)
        store.log_message(phone, "out", message)
    except WhatsAppError as e:
        logger.error("Failed to send WhatsApp message to %s: %s", phone, e)
