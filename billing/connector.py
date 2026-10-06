"""
Seam for a real billing/invoicing software integration (e.g. QuickBooks,
Zoho Books, Tally, a local accounting package). No such integration exists
yet, so this is a stub - confirmed orders are not pushed anywhere external
today.

When a billing vendor is identified, implement push_order_invoice() below to
call that vendor's API. Nothing else in the app needs to change - main.py's
_confirm_order() calls this as a best-effort, fire-and-forget step; if it
raises BillingNotConfiguredError (the default) the call is caught, logged,
and ignored - order confirmation to the customer is never blocked or delayed
by this.
"""


class BillingNotConfiguredError(Exception):
    pass


def push_order_invoice(order: dict, items: list) -> dict:
    """Should push a confirmed order to the external billing/invoicing
    system and return whatever identifier it gives back, e.g.
        {"external_invoice_id": str, "status": str}
    `order` and `items` are the same dict shapes storage.store.get_order()
    and storage.store.get_order_items() return.
    """
    raise BillingNotConfiguredError(
        "No billing connector configured yet. Set BILLING_API_BASE_URL and "
        "BILLING_API_KEY in .env once a vendor is chosen, and implement "
        "push_order_invoice() in billing/connector.py."
    )


def sync_payment_status(order_id: int) -> str:
    """Should pull the current payment/invoice status for this order from
    the external billing system and return one of: 'pending', 'paid',
    'failed'. Optional - only needed if the billing vendor is the source of
    truth for payment status rather than the delivery agent's PAID keyword
    (see main.py's _AGENT_PAYMENT_KEYWORDS) or a card-machine webhook."""
    raise BillingNotConfiguredError(
        "No billing connector configured yet - cannot sync payment status."
    )
