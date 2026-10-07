"""
Admin dashboard routes: login/logout, dashboard home, customers,
conversations, orders, analytics, catalog, offers, billing, settings,
printer, delivery agents. Every route except /admin/login depends on
get_current_admin, which raises 401 for missing/invalid sessions - handled
by the exception handler registered in main.py.
"""
import io
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from admin.auth import (
    create_session_cookie_value, get_current_admin, hash_password,
    try_get_current_admin, verify_password,
)
from admin.templating import render
from catalog import store as catalog_store
from config import config
from offers import store as offers_store
from storage import store

router = APIRouter(prefix="/admin")


def _default_date_range():
    end = date.today()
    start = end - timedelta(days=30)
    return start.isoformat(), end.isoformat()


def _month_bounds(year_month: str):
    """year_month is 'YYYY-MM' - returns (first day, last day) as ISO date strings."""
    year, month = int(year_month[:4]), int(year_month[5:7])
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


# ---- Auth ----

@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    """First-run admin creation - only reachable while the admins table is
    empty (e.g. right after a fresh deploy, or on a host like Render's free
    tier where the disk resets on every deploy/restart). Once any admin
    exists this always redirects to login, so it's safe to leave deployed
    permanently rather than needing a token or manual removal."""
    if store.any_admin_exists():
        return RedirectResponse("/admin/login", status_code=303)
    return render(request, "setup.html", error=None)


@router.post("/setup")
def setup_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if store.any_admin_exists():
        return RedirectResponse("/admin/login", status_code=303)
    if len(password) < 8:
        return render(request, "setup.html", error="Password must be at least 8 characters.")

    store.create_admin(username, hash_password(password))
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if try_get_current_admin(request):
        return RedirectResponse("/admin", status_code=303)
    if not store.any_admin_exists():
        return RedirectResponse("/admin/setup", status_code=303)
    return render(request, "login.html", error=None)


@router.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    admin = store.get_admin_by_username(username)
    if not admin or not verify_password(password, admin["password_hash"]):
        return render(request, "login.html", error="Invalid username or password.")

    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        config.ADMIN_SESSION_COOKIE,
        create_session_cookie_value(admin["id"]),
        max_age=config.ADMIN_SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(config.ADMIN_SESSION_COOKIE)
    return response


# ---- Dashboard home ----

@router.get("", response_class=HTMLResponse)
def dashboard_home(request: Request, admin=Depends(get_current_admin)):
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()

    stats_today = store.get_stats(today, today)
    stats_month = store.get_stats(month_start, today)
    daily = store.get_daily_counts(*_last_n_days(7))
    recent_orders = store.get_orders(*_default_date_range())[:6]

    return render(
        request, "dashboard_home.html", active_page="dashboard", admin=admin,
        stats_today=stats_today, stats_month=stats_month, daily=daily,
        recent_orders=recent_orders,
    )


def _last_n_days(n: int):
    end = date.today()
    start = end - timedelta(days=n - 1)
    return start.isoformat(), end.isoformat()


# ---- Customers ----

@router.get("/customers", response_class=HTMLResponse)
def customers_page(request: Request, admin=Depends(get_current_admin), start: str = "", end: str = ""):
    default_start, default_end = _default_date_range()
    start, end = start or default_start, end or default_end
    customers = store.get_customers_summary(start, end)
    return render(
        request, "customers.html", active_page="customers", admin=admin,
        customers=customers, start=start, end=end,
    )


# ---- Conversations ----

@router.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, admin=Depends(get_current_admin), start: str = "", end: str = "", phone: str = ""):
    default_start, default_end = _default_date_range()
    start, end = start or default_start, end or default_end
    customers = store.get_customers_summary(start, end)
    transcript = store.get_conversation(phone, start, end) if phone else None

    # Order history and purchase-behavior stats are shown across all time,
    # not just the transcript's date filter - the point is seeing a
    # customer's buying pattern over time, not just in the current window.
    order_history_by_month = None
    orders_by_month = {}
    favorite_items = None
    behavior_stats = None
    if phone:
        order_history_by_month = store.get_customer_orders_by_month(phone)
        for month in order_history_by_month:
            month_start, month_end = _month_bounds(month["year_month"])
            orders_by_month[month["year_month"]] = store.get_customer_orders(phone, month_start, month_end)

        favorite_items = store.get_customer_favorite_items(phone, limit=5)
        all_orders = store.get_customer_orders(phone)
        placed_orders = [o for o in all_orders if o["status"] in ("confirmed", "packed", "picked_up", "delivered")]
        if placed_orders:
            total_spent = sum(o["total"] for o in placed_orders)
            avg_order_value = total_spent / len(placed_orders)
            most_recent = max(o["created_at"] for o in placed_orders)
            days_since_last = (date.today() - datetime.fromisoformat(most_recent).date()).days
            behavior_stats = {
                "total_orders": len(placed_orders),
                "total_spent": total_spent,
                "avg_order_value": avg_order_value,
                "days_since_last": days_since_last,
            }

    return render(
        request, "conversations.html", active_page="conversations", admin=admin,
        customers=customers, start=start, end=end, phone=phone, transcript=transcript,
        order_history_by_month=order_history_by_month, orders_by_month=orders_by_month,
        favorite_items=favorite_items, behavior_stats=behavior_stats,
    )


# ---- Orders ----

@router.get("/orders", response_class=HTMLResponse)
def orders_page(request: Request, admin=Depends(get_current_admin), start: str = "", end: str = "", status: str = ""):
    default_start, default_end = _default_date_range()
    start, end = start or default_start, end or default_end
    orders = store.get_orders(start, end, status or None)
    orders_with_items = [(o, store.get_order_items(o["id"])) for o in orders]
    return render(
        request, "orders.html", active_page="orders", admin=admin,
        orders_with_items=orders_with_items, start=start, end=end, status=status,
    )


_STATUS_CUSTOMER_MESSAGES = {
    "picked_up": "Your order {ref} is on its way!",
    "delivered": "Your order {ref} has been delivered. Enjoy your meal! Thank you for ordering from {store}.",
    "cancelled": "Your order {ref} has been cancelled by the restaurant. Please message us if you have any questions.",
}


@router.post("/orders/{order_id}/status")
def order_update_status(
    order_id: int, admin=Depends(get_current_admin),
    new_status: str = Form(...), start: str = Form(""), end: str = Form(""), status: str = Form(""),
):
    """Manual status override from the Orders page - for staff to move an
    order along (or cancel it) without waiting on a delivery agent's
    WhatsApp keyword reply. Notifies the customer for the statuses they'd
    care about, same wording as the delivery-agent flow in main.py."""
    from urllib.parse import urlencode

    order = store.get_order(order_id)
    if order and new_status in store.ORDER_STATUSES and new_status != order["status"]:
        store.set_order_status(order_id, new_status)
        template = _STATUS_CUSTOMER_MESSAGES.get(new_status)
        if template:
            from whatsapp.client import WhatsAppError, send_text_message
            updated = store.get_order(order_id)
            msg = template.format(ref=store.order_ref(updated), store=config.STORE_NAME)
            try:
                send_text_message(order["phone"], msg)
                store.log_message(order["phone"], "out", msg)
            except WhatsAppError:
                pass  # status change still stands; notification is best-effort
    query = urlencode({k: v for k, v in {"start": start, "end": end, "status": status}.items() if v})
    return RedirectResponse("/admin/orders" + (f"?{query}" if query else ""), status_code=303)


@router.get("/orders/latest-id")
def orders_latest_id(admin=Depends(get_current_admin)):
    """Baseline for the dashboard's live new-order poll (see base.html) -
    called once on page load so the alert only fires for orders confirmed
    AFTER the admin opened the dashboard, not every pre-existing one.
    Despite the "latest_id" key name (kept for frontend compatibility),
    this is actually confirmed_at - see get_orders_since's docstring for
    why that's necessary instead of the order's own internal id."""
    return {"latest_id": store.get_latest_confirmed_at()}


@router.get("/orders/{order_id}/print", response_class=HTMLResponse)
def order_print_receipt(order_id: int, request: Request, admin=Depends(get_current_admin)):
    """Thermal-receipt-styled page for the browser's native print dialog -
    opened from the new-order alert banner's Print button (base.html) or
    directly from the Orders page. Works with any printer the staff's
    computer can already print to (the admin dashboard runs in the cloud,
    so it can't talk to a local thermal printer directly - this is the
    standard way a cloud app hands off to a local device)."""
    from config import vat_breakdown

    order = store.get_order(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    items = store.get_order_items(order_id)
    customer = store.get_customer(order["phone"])
    excl_vat, vat_amount = vat_breakdown(order["total"])
    return render(
        request, "order_print.html", active_page="orders", admin=admin,
        order=order, items=items, customer=customer,
        order_type_label=store.order_type_label(order),
        amount_excl_vat=excl_vat, vat_amount=vat_amount,
    )


@router.get("/orders/new")
def orders_new_since(since: str = "", admin=Depends(get_current_admin)):
    """Polled every few seconds by the dashboard's live alert JS (base.html)
    - returns any orders confirmed after `since` (a confirmed_at timestamp,
    despite the generic param name kept for frontend compatibility) so the
    popup+beep can show order #, type (delivery/pickup/dine-in), and total
    for each. See get_orders_since's docstring for why this is keyed on
    confirmed_at rather than the order's own internal id."""
    new_orders = store.get_orders_since(since)
    latest = max([o["confirmed_at"] for o in new_orders], default=since or store.get_latest_confirmed_at())
    return {
        "latest_id": latest,
        "orders": [
            {
                "id": o["id"],
                "ref": store.order_ref(o),
                "order_type": store.order_type_label(o),
                "total": o["total"],
                "phone": o["phone"],
                "notes": o.get("notes"),
            }
            for o in new_orders
        ],
    }


# ---- Catalog (menu) ----

@router.get("/catalog", response_class=HTMLResponse)
def catalog_page(request: Request, admin=Depends(get_current_admin), q: str = "", added: str = ""):
    items = catalog_store.search_items(q, limit=1000) if q.strip() else catalog_store.list_items()
    last_synced = catalog_store.last_synced_at()
    return render(
        request, "catalog.html", active_page="catalog", admin=admin,
        items=items, last_synced=last_synced, q=q,
        add_message="Item added to the menu." if added else None,
    )


@router.post("/catalog/add")
def catalog_add_item(
    request: Request, admin=Depends(get_current_admin),
    name: str = Form(...), category: str = Form(""), unit: str = Form(""),
    price: str = Form(...), stock_qty: str = Form("1"), q: str = Form(""),
):
    items = catalog_store.search_items(q, limit=1000) if q.strip() else catalog_store.list_items()
    last_synced = catalog_store.last_synced_at()

    if not name.strip():
        return render(
            request, "catalog.html", active_page="catalog", admin=admin,
            items=items, last_synced=last_synced, q=q, add_error="Item name is required.",
        )
    try:
        price_val = float(price)
        stock_qty_val = float(stock_qty) if stock_qty.strip() else 1.0
    except ValueError:
        return render(
            request, "catalog.html", active_page="catalog", admin=admin,
            items=items, last_synced=last_synced, q=q, add_error="Price and stock quantity must be numbers.",
        )
    if price_val < 0 or stock_qty_val < 0:
        return render(
            request, "catalog.html", active_page="catalog", admin=admin,
            items=items, last_synced=last_synced, q=q, add_error="Price and stock quantity can't be negative.",
        )

    catalog_store.upsert_item(
        name=name.strip(), price=price_val, stock_qty=stock_qty_val,
        category=category.strip() or None, unit=unit.strip() or None, source="manual",
    )
    return RedirectResponse(f"/admin/catalog?q={q}&added=1", status_code=303)


@router.post("/catalog/import")
async def catalog_import(request: Request, admin=Depends(get_current_admin), file: UploadFile = File(...)):
    import os
    import tempfile

    from catalog.excel_import import import_file

    suffix = os.path.splitext(file.filename or "upload.xlsx")[1] or ".xlsx"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    error = None
    count = 0
    try:
        count = import_file(tmp_path, source="excel")
    except Exception as e:
        error = str(e)
    finally:
        os.remove(tmp_path)

    items = catalog_store.list_items()
    last_synced = catalog_store.last_synced_at()
    return render(
        request, "catalog.html", active_page="catalog", admin=admin,
        items=items, last_synced=last_synced, q="",
        import_message=f"Imported/updated {count} item(s)." if not error else None,
        import_error=error,
    )


@router.post("/catalog/{item_id}/toggle-stock")
def catalog_toggle_stock(item_id: int, admin=Depends(get_current_admin), q: str = Form("")):
    item = catalog_store.get_item(item_id)
    if item:
        catalog_store.set_stock(item_id, 0 if item["in_stock"] else 1)
    return RedirectResponse(f"/admin/catalog?q={q}", status_code=303)


# ---- Analytics ----

@router.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request, admin=Depends(get_current_admin), start: str = "", end: str = "", preset: str = ""):
    start, end = _resolve_range(start, end, preset)
    stats = store.get_stats(start, end)
    daily = store.get_daily_counts(start, end)
    orders = store.get_orders(start, end)
    return render(
        request, "analytics.html", active_page="analytics", admin=admin,
        stats=stats, daily=daily, orders=orders, start=start, end=end, preset=preset,
    )


def _resolve_range(start: str, end: str, preset: str):
    today = date.today()
    if preset == "today":
        return today.isoformat(), today.isoformat()
    if preset == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if preset == "7d":
        return (today - timedelta(days=6)).isoformat(), today.isoformat()
    default_start, default_end = _default_date_range()
    return start or default_start, end or default_end


@router.get("/analytics/export")
def analytics_export(admin=Depends(get_current_admin), start: str = "", end: str = ""):
    default_start, default_end = _default_date_range()
    start, end = start or default_start, end or default_end

    from openpyxl import Workbook

    wb = Workbook()

    ws = wb.active
    ws.title = "Messages"
    ws.append(["Timestamp (UTC)", "Phone", "Name", "Direction", "Message"])
    for row in store.get_all_messages(start, end):
        ws.append([row["created_at"], row["phone"], row["name"], "Customer" if row["direction"] == "in" else "Bot", row["message"]])
    for col_letter, width in zip("ABCDE", [26, 16, 20, 10, 60]):
        ws.column_dimensions[col_letter].width = width

    ws2 = wb.create_sheet("Customers")
    ws2.append(["Phone", "Name", "Message Count", "Last Message (UTC)"])
    for row in store.get_customers_summary(start, end):
        ws2.append([row["phone"], row["name"], row["message_count"], row["last_message_at"]])
    for col_letter, width in zip("ABCD", [16, 20, 14, 26]):
        ws2.column_dimensions[col_letter].width = width

    ws3 = wb.create_sheet("Daily Summary")
    ws3.append(["Date", "Messages Received", "Replies Sent"])
    for row in store.get_daily_counts(start, end):
        ws3.append([row["day"], row["received"], row["sent"]])
    for col_letter, width in zip("ABC", [14, 18, 14]):
        ws3.column_dimensions[col_letter].width = width

    ws4 = wb.create_sheet("Orders")
    ws4.append(["Order ID", "Phone", "Status", "Order Type", "Delivery Agent", "Subtotal", "Delivery Fee", "Discount Offer ID", "Total", "Created (UTC)", "Confirmed (UTC)"])
    for o in store.get_orders(start, end):
        ws4.append([o["id"], o["phone"], o["status"], store.order_type_label(o), o["delivery_agent_phone"], o["subtotal"], o["delivery_fee"], o["discount_applied"], o["total"], o["created_at"], o["confirmed_at"]])
    for col_letter, width in zip("ABCDEFGHIJK", [10, 16, 14, 8, 16, 12, 12, 14, 12, 26, 26]):
        ws4.column_dimensions[col_letter].width = width

    ws5 = wb.create_sheet("Menu Snapshot")
    ws5.append(["SKU", "Name", "Category", "Unit", "Price", "In Stock", "Source", "Updated (UTC)"])
    for item in catalog_store.list_items():
        ws5.append([item["sku"], item["name"], item["category"], item["unit"], item["price"], "Yes" if item["in_stock"] else "No", item["source"], item["updated_at"]])
    for col_letter, width in zip("ABCDEFGH", [14, 32, 22, 12, 10, 10, 10, 26]):
        ws5.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"finedine-report_{start}_to_{end}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- Reports ----
# Distinct from Analytics: pre-built, calendar-period summaries (monthly /
# yearly rollups, top-selling items) rather than an arbitrary custom range.

@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, admin=Depends(get_current_admin), year: str = "", month: str = ""):
    today = date.today()
    year = year or str(today.year)
    month = month or f"{today.month:02d}"
    month_start, month_end = _month_bounds(f"{year}-{month}")

    monthly_stats = store.get_stats(month_start, month_end)
    top_items = store.get_top_selling_items(month_start, month_end, limit=10)
    yearly_rollup = store.get_orders_by_month(f"{year}-01-01", f"{year}-12-31")

    available_years = sorted({row["year_month"][:4] for row in store.get_orders_by_month()}, reverse=True)
    if not available_years:
        available_years = [str(today.year)]

    return render(
        request, "reports.html", active_page="reports", admin=admin,
        year=year, month=month, monthly_stats=monthly_stats, top_items=top_items,
        yearly_rollup=yearly_rollup, available_years=available_years,
    )


@router.get("/reports/export")
def reports_export(admin=Depends(get_current_admin), year: str = "", month: str = ""):
    today = date.today()
    year = year or str(today.year)
    month = month or f"{today.month:02d}"
    month_start, month_end = _month_bounds(f"{year}-{month}")

    from openpyxl import Workbook

    wb = Workbook()

    ws = wb.active
    ws.title = "Monthly Summary"
    stats = store.get_stats(month_start, month_end)
    ws.append(["Metric", "Value"])
    ws.append(["Period", f"{year}-{month}"])
    ws.append(["Messages received", stats["messages_received"]])
    ws.append(["Replies sent", stats["replies_sent"]])
    ws.append(["Unique customers", stats["unique_customers"]])
    ws.append(["Orders confirmed", stats["orders_confirmed"]])
    ws.append(["Revenue", stats["revenue"]])
    for col_letter, width in zip("AB", [24, 20]):
        ws.column_dimensions[col_letter].width = width

    ws2 = wb.create_sheet("Top Items")
    ws2.append(["Item", "Qty Sold", "Revenue"])
    for row in store.get_top_selling_items(month_start, month_end, limit=50):
        ws2.append([row["name"], row["total_qty"], row["total_revenue"]])
    for col_letter, width in zip("ABC", [30, 12, 14]):
        ws2.column_dimensions[col_letter].width = width

    ws3 = wb.create_sheet("Yearly Rollup")
    ws3.append(["Month", "Orders", "Revenue", "Unique Customers"])
    for row in store.get_orders_by_month(f"{year}-01-01", f"{year}-12-31"):
        ws3.append([row["year_month"], row["order_count"], row["total_revenue"], row["unique_customers"]])
    for col_letter, width in zip("ABCD", [12, 12, 14, 16]):
        ws3.column_dimensions[col_letter].width = width

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"finedine-report_{year}-{month}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---- Offers ----

@router.get("/offers", response_class=HTMLResponse)
def offers_page(request: Request, admin=Depends(get_current_admin)):
    offers = offers_store.list_offers()
    return render(request, "offers.html", active_page="offers", admin=admin, offers=offers, draft=None)


@router.post("/offers/draft")
def offers_draft(request: Request, admin=Depends(get_current_admin), prompt: str = Form(...)):
    from ai.offer_assistant import draft_offer

    offers = offers_store.list_offers()
    error = None
    draft = None
    try:
        draft = draft_offer(prompt)
    except Exception as e:
        error = str(e)

    return render(
        request, "offers.html", active_page="offers", admin=admin,
        offers=offers, draft=draft, draft_error=error, last_prompt=prompt,
    )


@router.post("/offers/create")
def offers_create(
    request: Request, admin=Depends(get_current_admin),
    title: str = Form(...), description: str = Form(""), discount_type: str = Form(...),
    discount_value: float = Form(...), starts_at: str = Form(""), ends_at: str = Form(""),
):
    offers_store.create_offer(
        title=title, description=description, discount_type=discount_type,
        discount_value=discount_value, starts_at=starts_at or None, ends_at=ends_at or None,
        status="draft", created_by_admin_id=admin["id"],
    )
    return RedirectResponse("/admin/offers", status_code=303)


@router.post("/offers/{offer_id}/activate")
def offers_activate(offer_id: int, admin=Depends(get_current_admin)):
    offers_store.set_offer_status(offer_id, "active")
    return RedirectResponse("/admin/offers", status_code=303)


@router.post("/offers/{offer_id}/deactivate")
def offers_deactivate(offer_id: int, admin=Depends(get_current_admin)):
    offers_store.set_offer_status(offer_id, "expired")
    return RedirectResponse("/admin/offers", status_code=303)


@router.post("/offers/{offer_id}/delete")
def offers_delete(offer_id: int, admin=Depends(get_current_admin)):
    offers_store.delete_offer(offer_id)
    return RedirectResponse("/admin/offers", status_code=303)


# ---- Delivery agents ----

@router.get("/delivery-agents", response_class=HTMLResponse)
def delivery_agents_page(request: Request, admin=Depends(get_current_admin)):
    agents = store.list_delivery_agents()
    return render(request, "delivery_agents.html", active_page="delivery_agents", admin=admin, agents=agents, error=None)


@router.post("/delivery-agents")
def delivery_agents_add(request: Request, admin=Depends(get_current_admin), phone: str = Form(...), name: str = Form(...)):
    store.add_delivery_agent(phone.strip(), name.strip())
    return RedirectResponse("/admin/delivery-agents", status_code=303)


@router.post("/delivery-agents/{agent_id}/activate")
def delivery_agents_activate(agent_id: int, admin=Depends(get_current_admin)):
    store.set_delivery_agent_active(agent_id, True)
    return RedirectResponse("/admin/delivery-agents", status_code=303)


@router.post("/delivery-agents/{agent_id}/deactivate")
def delivery_agents_deactivate(agent_id: int, admin=Depends(get_current_admin)):
    store.set_delivery_agent_active(agent_id, False)
    return RedirectResponse("/admin/delivery-agents", status_code=303)


# ---- Billing ----

@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request, admin=Depends(get_current_admin)):
    today = date.today()
    stats_month = store.get_stats(today.replace(day=1).isoformat(), today.isoformat())
    billing_settings = store.get_billing_settings()
    return render(
        request, "billing.html", active_page="billing", admin=admin,
        stats_month=stats_month, billing_settings=billing_settings, message=None, error=None,
    )


@router.post("/billing/connector")
def billing_connector_save(
    request: Request, admin=Depends(get_current_admin),
    enabled: str = Form(""), api_base_url: str = Form(""), api_key: str = Form(""),
):
    store.set_billing_settings(
        enabled=bool(enabled),
        api_base_url=api_base_url.strip() or None,
        api_key=api_key.strip() or None,
    )
    today = date.today()
    return render(
        request, "billing.html", active_page="billing", admin=admin,
        stats_month=store.get_stats(today.replace(day=1).isoformat(), today.isoformat()),
        billing_settings=store.get_billing_settings(), message="Billing connector settings saved.", error=None,
    )


# ---- Printer ----

@router.get("/printer", response_class=HTMLResponse)
def printer_page(request: Request, admin=Depends(get_current_admin)):
    settings = store.get_printer_settings()
    return render(request, "printer.html", active_page="printer", admin=admin, settings=settings, message=None, error=None)


@router.post("/printer")
def printer_save(
    request: Request, admin=Depends(get_current_admin),
    connection_type: str = Form(...), printer_ip: str = Form(""), printer_port: str = Form(""),
):
    if connection_type not in ("network", "bluetooth", "usb"):
        return render(
            request, "printer.html", active_page="printer", admin=admin,
            settings=store.get_printer_settings(), message=None, error="Invalid connection type.",
        )
    if connection_type == "network" and not printer_ip.strip():
        return render(
            request, "printer.html", active_page="printer", admin=admin,
            settings=store.get_printer_settings(), message=None, error="Printer IP address is required for a network printer.",
        )
    if connection_type in ("bluetooth", "usb") and not printer_port.strip():
        return render(
            request, "printer.html", active_page="printer", admin=admin,
            settings=store.get_printer_settings(), message=None,
            error="COM port / device path is required for a Bluetooth or USB printer.",
        )

    store.set_printer_settings(
        connection_type=connection_type,
        printer_ip=printer_ip.strip() or None,
        printer_port=printer_port.strip() or None,
    )
    return render(
        request, "printer.html", active_page="printer", admin=admin,
        settings=store.get_printer_settings(), message="Printer settings saved.", error=None,
    )


# ---- Settings ----

@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, admin=Depends(get_current_admin)):
    return render(request, "settings.html", active_page="settings", admin=admin, message=None, error=None)


@router.post("/settings/password")
def settings_change_password(
    request: Request, admin=Depends(get_current_admin),
    current_password: str = Form(...), new_password: str = Form(...),
):
    if not verify_password(current_password, admin["password_hash"]):
        return render(request, "settings.html", active_page="settings", admin=admin, message=None, error="Current password is incorrect.")
    if len(new_password) < 8:
        return render(request, "settings.html", active_page="settings", admin=admin, message=None, error="New password must be at least 8 characters.")

    store.set_admin_password(admin["id"], hash_password(new_password))
    return render(request, "settings.html", active_page="settings", admin=admin, message="Password updated.", error=None)


@router.post("/settings/store")
def settings_save_store(
    request: Request, admin=Depends(get_current_admin),
    store_name: str = Form(...), currency: str = Form(...),
    delivery_fee: str = Form(...), free_delivery_threshold: str = Form(...),
    store_phone: str = Form(""),
):
    from config import apply_store_settings_override

    if not store_name.strip():
        return render(request, "settings.html", active_page="settings", admin=admin, message=None, error="Store name is required.")
    if not currency.strip():
        return render(request, "settings.html", active_page="settings", admin=admin, message=None, error="Currency is required.")
    try:
        delivery_fee_val = float(delivery_fee)
        free_delivery_threshold_val = float(free_delivery_threshold)
    except ValueError:
        return render(request, "settings.html", active_page="settings", admin=admin, message=None, error="Delivery fee and free delivery threshold must be numbers.")
    if delivery_fee_val < 0 or free_delivery_threshold_val < 0:
        return render(request, "settings.html", active_page="settings", admin=admin, message=None, error="Delivery fee and free delivery threshold can't be negative.")

    store.set_store_settings(
        store_name=store_name.strip(), currency=currency.strip(),
        delivery_fee=delivery_fee_val, free_delivery_threshold=free_delivery_threshold_val,
        store_phone=store_phone.strip() or None,
    )
    apply_store_settings_override()  # take effect immediately, no restart needed
    return render(request, "settings.html", active_page="settings", admin=admin, message="Store settings saved.", error=None)


# ---- WhatsApp connection (Embedded Signup / Coexistence) ----

@router.get("/whatsapp", response_class=HTMLResponse)
def whatsapp_connect_page(request: Request, admin=Depends(get_current_admin)):
    connection = store.get_whatsapp_connection()
    return render(
        request, "whatsapp_connect.html", active_page="whatsapp", admin=admin,
        connection=connection, message=None, error=None,
        meta_app_id=config.META_APP_ID, meta_config_id=config.META_CONFIG_ID,
    )


@router.post("/whatsapp/connect")
def whatsapp_connect_callback(
    request: Request, admin=Depends(get_current_admin),
    code: str = Form(...), phone_number_id: str = Form(...),
    waba_id: str = Form(""), is_coexistence: str = Form(""),
):
    from config import apply_whatsapp_connection_override
    from whatsapp.coexistence import CoexistenceError, exchange_code_for_token, register_phone_number

    try:
        access_token = exchange_code_for_token(code)
        register_phone_number(phone_number_id, access_token)
    except CoexistenceError as e:
        connection = store.get_whatsapp_connection()
        return render(
            request, "whatsapp_connect.html", active_page="whatsapp", admin=admin,
            connection=connection, message=None, error=str(e),
            meta_app_id=config.META_APP_ID, meta_config_id=config.META_CONFIG_ID,
        )

    store.set_whatsapp_connection(
        access_token=access_token, phone_number_id=phone_number_id,
        waba_id=waba_id or None, is_coexistence=bool(is_coexistence),
    )
    apply_whatsapp_connection_override()  # take effect immediately, no restart needed

    connection = store.get_whatsapp_connection()
    msg = (
        "WhatsApp connected! Your existing WhatsApp Business app will keep working side "
        "by side - chat history sync (up to 6 months) may take a few minutes to appear."
        if connection and connection["is_coexistence"] else
        "WhatsApp number connected."
    )
    return render(
        request, "whatsapp_connect.html", active_page="whatsapp", admin=admin,
        connection=connection, message=msg, error=None,
        meta_app_id=config.META_APP_ID, meta_config_id=config.META_CONFIG_ID,
    )


@router.post("/whatsapp/disconnect")
def whatsapp_disconnect(admin=Depends(get_current_admin)):
    store.clear_whatsapp_connection()
    return RedirectResponse("/admin/whatsapp", status_code=303)


# ---- WhatsApp message templates ----
# Pre-approved templates needed to message a customer outside the 24h
# session window (e.g. a proactive "your order is ready" sent well after
# their last message) - distinct from the free-form replies the bot sends
# inside an active conversation. See whatsapp/templates.py.

def _whatsapp_api_configured() -> bool:
    return bool(config.WHATSAPP_ACCESS_TOKEN and config.WHATSAPP_BUSINESS_ACCOUNT_ID)


@router.get("/whatsapp/templates", response_class=HTMLResponse)
def whatsapp_templates_page(request: Request, admin=Depends(get_current_admin)):
    from whatsapp.templates import WhatsAppTemplateError, list_templates

    templates_list, error = [], None
    if _whatsapp_api_configured():
        try:
            templates_list = list_templates()
        except WhatsAppTemplateError as e:
            error = str(e)
    return render(
        request, "whatsapp_templates.html", active_page="whatsapp_templates", admin=admin,
        templates_list=templates_list, message=None, error=error,
        api_configured=_whatsapp_api_configured(),
    )


@router.post("/whatsapp/templates/create")
def whatsapp_templates_create(
    request: Request, admin=Depends(get_current_admin),
    name: str = Form(...), category: str = Form(...), language: str = Form("en_US"),
    body_text: str = Form(...), footer_text: str = Form(""),
):
    from whatsapp.templates import WhatsAppTemplateError, create_template, list_templates

    import re as _re
    clean_name = _re.sub(r"[^a-z0-9_]", "", name.strip().lower().replace(" ", "_"))

    error = None
    message = None
    if not clean_name:
        error = "Template name is required (letters, numbers, underscores only)."
    elif category not in ("MARKETING", "UTILITY", "AUTHENTICATION"):
        error = "Invalid category."
    elif not body_text.strip():
        error = "Template body text is required."
    else:
        try:
            create_template(
                name=clean_name, category=category, body_text=body_text.strip(),
                language=language.strip() or "en_US", footer_text=footer_text.strip() or None,
            )
            message = f"Template '{clean_name}' submitted - it will show as PENDING until Meta reviews and approves it."
        except WhatsAppTemplateError as e:
            error = str(e)

    templates_list = []
    if _whatsapp_api_configured():
        try:
            templates_list = list_templates()
        except Exception:
            pass
    return render(
        request, "whatsapp_templates.html", active_page="whatsapp_templates", admin=admin,
        templates_list=templates_list, message=message, error=error,
        api_configured=_whatsapp_api_configured(),
    )


@router.post("/whatsapp/templates/{name}/delete")
def whatsapp_templates_delete(name: str, admin=Depends(get_current_admin)):
    from whatsapp.templates import WhatsAppTemplateError, delete_template
    try:
        delete_template(name)
    except WhatsAppTemplateError:
        pass
    return RedirectResponse("/admin/whatsapp/templates", status_code=303)
