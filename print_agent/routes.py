"""
JSON API for the store's local print agent (print_agent/agent.py) to poll
for newly confirmed orders and print them on a network thermal printer.

Deliberately mounted outside /admin so it's never touched by main.py's
_admin_auth_redirect exception handler (which redirects browser requests to
/admin/login on a 401 - wrong behavior for a headless script client, which
wants a plain JSON 401 it can check and retry/log).
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from config import config
from print_agent.auth import require_print_agent_token
from storage import store

router = APIRouter(prefix="/print-agent")


def _to_local_iso(value: str) -> str:
    """Converts a stored UTC ISO timestamp to the restaurant's local time
    (STORE_TIMEZONE), still as an ISO string - same conversion as
    admin/templating.py's local_time Jinja filter, duplicated here (rather
    than imported) since print_agent/agent.py is a standalone script with
    no Jinja/web-app dependencies and just displays whatever it's given.
    Confirmed live: the printed thermal receipt showed raw UTC
    (e.g. "08:14") while the dashboard correctly showed local time
    ("12:14", UAE is UTC+4) - the standalone script had no timezone
    conversion at all, unlike every web-rendered page. Falls back to the
    raw value if it can't be parsed, same as the Jinja filter does."""
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(config.STORE_TIMEZONE)).isoformat()
    except (ValueError, TypeError):
        return value


@router.get("/orders/pending")
def pending_orders(_=Depends(require_print_agent_token)):
    from ai.agent import is_restaurant_open
    if not is_restaurant_open():
        return {"orders": []}  # hold pre-orders until the kitchen opens
    orders = store.get_unprinted_confirmed_orders()
    return {"orders": [_serialize_order(o) for o in orders]}


@router.post("/orders/{order_id}/ack")
def acknowledge_order(order_id: int, _=Depends(require_print_agent_token)):
    store.mark_order_printed(order_id)
    return {"status": "ok"}


@router.get("/printer-config")
def printer_config(_=Depends(require_print_agent_token)):
    settings = store.get_printer_settings()
    if not settings:
        return {"configured": False}
    return {"configured": True, **settings}


def _serialize_order(order: dict) -> dict:
    from config import vat_breakdown

    items = store.get_order_items(order["id"])
    customer = store.get_customer(order["phone"])
    excl_vat, vat_amount = vat_breakdown(order["total"])
    return {
        "id": order["id"],
        "order_code": store.order_ref(order),
        "phone": order["phone"],
        "customer_name": customer.get("name") if customer else None,
        "confirmed_at": _to_local_iso(order["confirmed_at"]),
        "order_type": order.get("order_type"),
        "order_type_label": store.order_type_label(order),
        "subtotal": order["subtotal"],
        "delivery_fee": order["delivery_fee"],
        "discount_applied": order["discount_applied"],
        "total": order["total"],
        "amount_excl_vat": excl_vat,
        "vat_amount": vat_amount,
        "delivery_address_text": order["delivery_address_text"],
        "delivery_lat": order["delivery_lat"],
        "delivery_lng": order["delivery_lng"],
        "notes": order["notes"],
        "items": [
            {
                "name": i["item_name_snapshot"],
                "qty": i["qty"],
                "unit_price": i["unit_price_snapshot"],
                "line_total": i["line_total"],
                # None for an off-catalog item (ai/agent.py's ADDITEM:
                # trailer) - print_agent/agent.py uses this to show "Price
                # TBD" instead of a misleading "AED 0.00".
                "catalog_item_id": i["catalog_item_id"],
            }
            for i in items
        ],
    }
