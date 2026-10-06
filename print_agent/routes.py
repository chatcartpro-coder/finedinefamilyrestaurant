"""
JSON API for the store's local print agent (print_agent/agent.py) to poll
for newly confirmed orders and print them on a network thermal printer.

Deliberately mounted outside /admin so it's never touched by main.py's
_admin_auth_redirect exception handler (which redirects browser requests to
/admin/login on a 401 - wrong behavior for a headless script client, which
wants a plain JSON 401 it can check and retry/log).
"""
from fastapi import APIRouter, Depends

from print_agent.auth import require_print_agent_token
from storage import store

router = APIRouter(prefix="/print-agent")


@router.get("/orders/pending")
def pending_orders(_=Depends(require_print_agent_token)):
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
        "phone": order["phone"],
        "customer_name": customer.get("name") if customer else None,
        "confirmed_at": order["confirmed_at"],
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
            }
            for i in items
        ],
    }
