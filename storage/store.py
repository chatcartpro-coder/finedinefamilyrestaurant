"""
Lightweight SQLite storage - no external DB needed to get started.
Tracks customers, conversation logs, in-progress/confirmed orders (incl. the
delivery-agent status lifecycle), and message dedup. Swap for Postgres later
by replacing this module if you outgrow SQLite (schema below avoids
SQLite-only syntax where practical).
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

from config import config

_local = threading.local()


def _get_conn():
    if not hasattr(_local, "conn"):
        os.makedirs(config.DB_DATA_DIR, exist_ok=True)
        _local.conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _local.conn.execute("PRAGMA foreign_keys = ON")
        _init_schema(_local.conn)
    return _local.conn


def _init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS customers (
        phone TEXT PRIMARY KEY,
        name TEXT,
        last_lat REAL,
        last_lng REAL,
        last_location_label TEXT,
        last_address_text TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        direction TEXT,        -- 'in' or 'out'
        message TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS processed_messages (
        message_id TEXT PRIMARY KEY,
        processed_at TEXT
    );

    -- status lifecycle: draft -> awaiting_confirmation -> confirmed
    --                    -> packed -> picked_up -> delivered
    --                 (or -> cancelled at any point before packed)
    -- packed/picked_up/delivered only apply to delivery orders, driven by
    -- the assigned delivery_agents member replying on WhatsApp; pickup
    -- orders go confirmed -> delivered directly (customer collects in person).
    -- payment_status is independent of the status lifecycle above, not part
    -- of it: NULL means cash/unknown, 'paid' means a delivery agent's card
    -- machine took payment (see main.py's _AGENT_PAYMENT_KEYWORDS) - nothing
    -- gates on it, a cash-on-delivery order simply never sets it.
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT,
        status TEXT DEFAULT 'draft',
        subtotal REAL DEFAULT 0,
        delivery_fee REAL DEFAULT 0,
        total REAL DEFAULT 0,
        delivery_lat REAL,
        delivery_lng REAL,
        delivery_address_text TEXT,
        is_pickup INTEGER DEFAULT 0,
        delivery_agent_phone TEXT,
        notes TEXT,
        created_at TEXT,
        confirmed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS order_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id INTEGER REFERENCES orders(id),
        catalog_item_id INTEGER,
        item_name_snapshot TEXT,
        unit_price_snapshot REAL,
        qty REAL,
        line_total REAL
    );

    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS offers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        discount_type TEXT NOT NULL,   -- 'percent' | 'fixed'
        discount_value REAL NOT NULL,
        scope_type TEXT NOT NULL DEFAULT 'all',  -- 'all' | 'category' | 'items'
        scope_value TEXT,              -- JSON list of category names or catalog_item_ids
        starts_at TEXT,
        ends_at TEXT,
        status TEXT NOT NULL DEFAULT 'draft',   -- draft | active | expired
        created_by_admin_id INTEGER,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS printer_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        connection_type TEXT NOT NULL DEFAULT 'network',  -- 'network' | 'bluetooth' | 'usb'
        printer_ip TEXT,
        printer_port TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS delivery_agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT UNIQUE NOT NULL,
        name TEXT,
        active INTEGER DEFAULT 1,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS billing_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        enabled INTEGER NOT NULL DEFAULT 0,
        api_base_url TEXT,
        api_key TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS store_settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        store_name TEXT,
        currency TEXT,
        delivery_fee REAL,
        free_delivery_threshold REAL,
        store_phone TEXT,
        updated_at TEXT
    );

    -- Holds the WhatsApp Business Account connected via Embedded Signup
    -- (Coexistence or standalone Cloud API number), overriding the
    -- .env-configured WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID once set.
    -- See whatsapp/coexistence.py.
    CREATE TABLE IF NOT EXISTS whatsapp_connection (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        access_token TEXT,
        phone_number_id TEXT,
        waba_id TEXT,
        is_coexistence INTEGER NOT NULL DEFAULT 0,
        history_sync_status TEXT,       -- NULL | 'pending' | 'complete'
        connected_at TEXT,
        updated_at TEXT
    );
    """)
    conn.commit()

    # Additive columns on an existing table - guard against re-running on a DB
    # that already has them (SQLite has no "ADD COLUMN IF NOT EXISTS").
    cols = [row[1] for row in conn.execute("PRAGMA table_info(orders)").fetchall()]
    if "discount_applied" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN discount_applied INTEGER")
        conn.commit()
    if "printed_at" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN printed_at TEXT")
        conn.commit()
    if "payment_status" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN payment_status TEXT")
        conn.commit()
    if "order_type" not in cols:
        # 'delivery' | 'pickup' | 'dine_in' - is_pickup is kept in sync for
        # backward compat (both pickup and dine_in set is_pickup=1, since
        # both skip delivery fee/address/agent assignment the same way) but
        # order_type is now the source of truth for what gets printed/shown.
        # Backfill existing rows from is_pickup since order_type didn't exist
        # before this column was added.
        conn.execute("ALTER TABLE orders ADD COLUMN order_type TEXT")
        conn.execute("UPDATE orders SET order_type = CASE WHEN is_pickup = 1 THEN 'pickup' ELSE 'delivery' END")
        conn.commit()
    if "order_code" not in cols:
        conn.execute("ALTER TABLE orders ADD COLUMN order_code TEXT")
        conn.commit()
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_order_code ON orders(order_code)")
    conn.commit()

    customer_cols = [row[1] for row in conn.execute("PRAGMA table_info(customers)").fetchall()]
    if "last_address_text" not in customer_cols:
        conn.execute("ALTER TABLE customers ADD COLUMN last_address_text TEXT")
        conn.commit()


# ---- Message dedup (Meta webhook retry protection) ----

def already_processed(message_id: str) -> bool:
    conn = _get_conn()
    cur = conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,))
    return cur.fetchone() is not None


def mark_processed(message_id: str):
    conn = _get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages (message_id, processed_at) VALUES (?, ?)",
        (message_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


# ---- Customers ----

def get_customer(phone: str):
    conn = _get_conn()
    cur = conn.execute(
        "SELECT phone, name, last_lat, last_lng, last_location_label, last_address_text "
        "FROM customers WHERE phone = ?",
        (phone,),
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["phone", "name", "last_lat", "last_lng", "last_location_label", "last_address_text"]
    return dict(zip(keys, row))


def upsert_customer(phone: str, name: str = None, lat: float = None, lng: float = None, label: str = None):
    existing = get_customer(phone)
    conn = _get_conn()
    conn.execute("""
        INSERT INTO customers (phone, name, last_lat, last_lng, last_location_label, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            name=COALESCE(excluded.name, customers.name),
            last_lat=COALESCE(excluded.last_lat, customers.last_lat),
            last_lng=COALESCE(excluded.last_lng, customers.last_lng),
            last_location_label=COALESCE(excluded.last_location_label, customers.last_location_label),
            updated_at=excluded.updated_at
    """, (phone, name, lat, lng, label, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    return existing


def set_customer_location(phone: str, lat: float, lng: float, label: str = None):
    upsert_customer(phone, lat=lat, lng=lng, label=label)


def set_customer_address_text(phone: str, address_text: str):
    """Persists a typed (non-pin) delivery address so it can be offered for
    reuse next time, same as a shared location pin is via set_customer_location.
    Kept in a separate column (last_address_text) rather than last_location_label,
    since that label is reserved for a human-friendly tag on a lat/lng pin."""
    conn = _get_conn()
    conn.execute("""
        INSERT INTO customers (phone, last_address_text, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            last_address_text=excluded.last_address_text,
            updated_at=excluded.updated_at
    """, (phone, address_text, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ---- Conversations ----

def log_message(phone: str, direction: str, message: str, created_at: str = None):
    """created_at: optional ISO timestamp override, used only when backfilling
    synced Coexistence chat history (whatsapp_connection's history webhook)
    so imported messages keep their real send time instead of "now"."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (phone, direction, message, created_at) VALUES (?, ?, ?, ?)",
        (phone, direction, message, created_at or datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def get_recent_history(phone: str, limit: int = 10):
    """Returns recent messages oldest-first, for conversation context."""
    conn = _get_conn()
    cur = conn.execute(
        "SELECT direction, message FROM conversations WHERE phone = ? ORDER BY id DESC LIMIT ?",
        (phone, limit),
    )
    rows = cur.fetchall()
    return list(reversed(rows))


# ---- Orders ----

_ORDER_COLUMNS = (
    "id, phone, status, subtotal, delivery_fee, total, delivery_lat, delivery_lng, "
    "delivery_address_text, is_pickup, delivery_agent_phone, notes, created_at, confirmed_at, "
    "discount_applied, printed_at, payment_status, order_type, order_code"
)

# Human-readable order_type labels, shared by main.py's WhatsApp receipt and
# admin/routes.py's new-order alert payload. Falls back to is_pickup for any
# pre-order_type-column row that somehow still has a NULL order_type.
ORDER_TYPE_LABELS = {"delivery": "Delivery", "pickup": "Takeaway / Pickup", "dine_in": "Dine-in"}


def order_ref(order: dict) -> str:
    """The reference shown to customers/staff/agents - the unique order_code
    once confirmed, falling back to the internal #id for drafts."""
    return order.get("order_code") or f"#{order['id']}"


def order_type_label(order: dict) -> str:
    return ORDER_TYPE_LABELS.get(order.get("order_type")) or ("Takeaway / Pickup" if order.get("is_pickup") else "Delivery")


def get_active_order(phone: str):
    """Returns the customer's current draft/awaiting_confirmation order, if any."""
    conn = _get_conn()
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders
        WHERE phone = ? AND status IN ('draft', 'awaiting_confirmation')
        ORDER BY id DESC LIMIT 1
    """, (phone,))
    row = cur.fetchone()
    if not row:
        return None
    return _order_row_to_dict(row)


def create_order(phone: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO orders (phone, status, created_at) VALUES (?, 'draft', ?)",
        (phone, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_order(order_id: int):
    conn = _get_conn()
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders WHERE id = ?
    """, (order_id,))
    row = cur.fetchone()
    return _order_row_to_dict(row) if row else None


def get_order_items(order_id: int):
    conn = _get_conn()
    cur = conn.execute("""
        SELECT id, catalog_item_id, item_name_snapshot, unit_price_snapshot, qty, line_total
        FROM order_items WHERE order_id = ?
    """, (order_id,))
    keys = ["id", "catalog_item_id", "item_name_snapshot", "unit_price_snapshot", "qty", "line_total"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def add_order_item(order_id: int, catalog_item_id: int, name: str, unit_price: float, qty: float):
    conn = _get_conn()
    line_total = round(unit_price * qty, 2)
    conn.execute("""
        INSERT INTO order_items (order_id, catalog_item_id, item_name_snapshot, unit_price_snapshot, qty, line_total)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (order_id, catalog_item_id, name, unit_price, qty, line_total))
    conn.commit()
    _recalc_order_totals(order_id)


def remove_order_item(item_id: int):
    conn = _get_conn()
    cur = conn.execute("SELECT order_id FROM order_items WHERE id = ?", (item_id,))
    row = cur.fetchone()
    if not row:
        return
    order_id = row[0]
    conn.execute("DELETE FROM order_items WHERE id = ?", (item_id,))
    conn.commit()
    _recalc_order_totals(order_id)


def set_order_item_qty(item_id: int, qty: float):
    """Updates an existing order_items row's quantity (and line_total) in
    place - used for a partial REMOVE (e.g. 3 in cart, customer says "remove
    1"), as opposed to remove_order_item's full-row delete."""
    conn = _get_conn()
    cur = conn.execute("SELECT order_id, unit_price_snapshot FROM order_items WHERE id = ?", (item_id,))
    row = cur.fetchone()
    if not row:
        return
    order_id, unit_price = row
    line_total = round(unit_price * qty, 2)
    conn.execute("UPDATE order_items SET qty = ?, line_total = ? WHERE id = ?", (qty, line_total, item_id))
    conn.commit()
    _recalc_order_totals(order_id)


def _recalc_order_totals(order_id: int):
    """Recomputes subtotal/total from line items. If a discount offer is
    already applied (discount_applied), re-derives its amount via
    offers.store.compute_discount so adding/removing items keeps the
    discount consistent with the new subtotal."""
    conn = _get_conn()
    subtotal = conn.execute(
        "SELECT COALESCE(SUM(line_total), 0) FROM order_items WHERE order_id = ?", (order_id,)
    ).fetchone()[0]
    order = get_order(order_id)
    delivery_fee = order["delivery_fee"] or 0

    discount = 0.0
    if order["discount_applied"]:
        from offers.store import compute_discount, get_offer
        offer = get_offer(order["discount_applied"])
        if offer:
            discount = compute_discount(offer, subtotal)

    conn.execute(
        "UPDATE orders SET subtotal = ?, total = ? WHERE id = ?",
        (subtotal, max(subtotal - discount, 0) + delivery_fee, order_id),
    )
    conn.commit()


def set_order_delivery(order_id: int, lat: float, lng: float, delivery_fee: float, address_text: str = None):
    conn = _get_conn()
    order = get_order(order_id)
    discount = 0.0
    if order["discount_applied"]:
        from offers.store import compute_discount, get_offer
        offer = get_offer(order["discount_applied"])
        if offer:
            discount = compute_discount(offer, order["subtotal"] or 0)
    total = max((order["subtotal"] or 0) - discount, 0) + delivery_fee
    conn.execute("""
        UPDATE orders SET delivery_lat = ?, delivery_lng = ?, delivery_fee = ?, total = ?, delivery_address_text = ?,
            order_type = 'delivery'
        WHERE id = ?
    """, (lat, lng, delivery_fee, total, address_text, order_id))
    conn.commit()


def set_order_delivery_text(order_id: int, address_text: str, delivery_fee: float):
    """Sibling to set_order_delivery for customers who type their address as
    plain text instead of sharing a WhatsApp location pin - delivery_lat/lng
    are left NULL (no coordinates available), so callers that build a map
    link (e.g. main.py's delivery-agent notification) correctly skip it for
    these orders, same as if location were simply never shared."""
    conn = _get_conn()
    order = get_order(order_id)
    discount = 0.0
    if order["discount_applied"]:
        from offers.store import compute_discount, get_offer
        offer = get_offer(order["discount_applied"])
        if offer:
            discount = compute_discount(offer, order["subtotal"] or 0)
    total = max((order["subtotal"] or 0) - discount, 0) + delivery_fee
    conn.execute("""
        UPDATE orders SET delivery_lat = NULL, delivery_lng = NULL, delivery_fee = ?, total = ?, delivery_address_text = ?,
            order_type = 'delivery'
        WHERE id = ?
    """, (delivery_fee, total, address_text, order_id))
    conn.commit()


def _set_order_no_delivery(order_id: int, order_type: str):
    """Shared by set_order_pickup/set_order_dine_in: no delivery fee, no
    location needed, and the delivery-agent flow is skipped entirely since
    there's nothing to hand off - identical handling, just a different
    order_type label for the receipt/admin UI."""
    conn = _get_conn()
    order = get_order(order_id)
    discount = 0.0
    if order["discount_applied"]:
        from offers.store import compute_discount, get_offer
        offer = get_offer(order["discount_applied"])
        if offer:
            discount = compute_discount(offer, order["subtotal"] or 0)
    total = max((order["subtotal"] or 0) - discount, 0)
    conn.execute(
        "UPDATE orders SET is_pickup = 1, order_type = ?, delivery_fee = 0, total = ? WHERE id = ?",
        (order_type, total, order_id),
    )
    conn.commit()


def set_order_pickup(order_id: int):
    _set_order_no_delivery(order_id, "pickup")


def set_order_dine_in(order_id: int):
    _set_order_no_delivery(order_id, "dine_in")


def apply_order_discount(order_id: int, offer_id: int | None):
    """Sets/clears which offer applies to an order and recomputes its total.
    offer_id=None clears any applied discount."""
    conn = _get_conn()
    conn.execute("UPDATE orders SET discount_applied = ? WHERE id = ?", (offer_id, order_id))
    conn.commit()
    _recalc_order_totals(order_id)


ORDER_STATUSES = ["draft", "awaiting_confirmation", "confirmed", "packed", "picked_up", "delivered", "cancelled"]


def _generate_order_code(conn) -> str:
    """Customer-facing order reference, e.g. FD-261007-K7Q2 - date (store
    local time) plus 4 random chars, so it can't be confused with or
    guessed from the internal sequential id. Retries on the (unlikely)
    collision, enforced by the unique index on orders.order_code."""
    import secrets
    from zoneinfo import ZoneInfo

    try:
        today = datetime.now(ZoneInfo(config.STORE_TIMEZONE))
    except Exception:
        today = datetime.now(timezone.utc)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I - easy to read aloud
    for _ in range(10):
        code = f"FD-{today:%y%m%d}-" + "".join(secrets.choice(alphabet) for _ in range(4))
        if not conn.execute("SELECT 1 FROM orders WHERE order_code = ?", (code,)).fetchone():
            return code
    raise RuntimeError("Could not generate a unique order code")


def set_order_status(order_id: int, status: str):
    conn = _get_conn()
    if status == "confirmed":
        existing = conn.execute("SELECT order_code FROM orders WHERE id = ?", (order_id,)).fetchone()
        code = existing[0] if existing and existing[0] else _generate_order_code(conn)
        conn.execute(
            "UPDATE orders SET status = ?, confirmed_at = ?, order_code = ? WHERE id = ?",
            (status, datetime.now(timezone.utc).isoformat(), code, order_id),
        )
    else:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()


def assign_delivery_agent(order_id: int, agent_phone: str):
    conn = _get_conn()
    conn.execute("UPDATE orders SET delivery_agent_phone = ? WHERE id = ?", (agent_phone, order_id))
    conn.commit()


def get_order_assigned_to_agent(agent_phone: str):
    """The delivery agent's current active order (packed or picked_up) -
    used to route the agent's next PACKED/PICKED/DELIVERED reply to the
    right order without them needing to type an order number."""
    conn = _get_conn()
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders
        WHERE delivery_agent_phone = ? AND status IN ('confirmed', 'packed', 'picked_up')
        ORDER BY confirmed_at DESC LIMIT 1
    """, (agent_phone,))
    row = cur.fetchone()
    return _order_row_to_dict(row) if row else None


# ---- Thermal printer integration ----
# printed_at tracks whether a confirmed order has already been sent to the
# restaurant's print agent, so a flaky network retry from that agent can
# never print the same receipt twice (same idempotency shape as
# processed_messages, but a nullable column rather than a separate table
# since this is a simple one-shot per-order flag). Printer integration is
# deferred for this restaurant but the mechanism is ready to go.

def is_order_printed(order_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute("SELECT printed_at FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    return bool(row and row[0])


def mark_order_printed(order_id: int):
    conn = _get_conn()
    conn.execute(
        "UPDATE orders SET printed_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), order_id),
    )
    conn.commit()


def get_unprinted_confirmed_orders():
    conn = _get_conn()
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders
        WHERE status = 'confirmed' AND printed_at IS NULL
        ORDER BY confirmed_at ASC
    """)
    return [_order_row_to_dict(row) for row in cur.fetchall()]


def _order_row_to_dict(row):
    keys = ["id", "phone", "status", "subtotal", "delivery_fee", "total", "delivery_lat", "delivery_lng",
            "delivery_address_text", "is_pickup", "delivery_agent_phone", "notes", "created_at", "confirmed_at",
            "discount_applied", "printed_at", "payment_status", "order_type", "order_code"]
    return dict(zip(keys, row))


def set_order_payment_status(order_id: int, payment_status: str):
    """Independent of order status (see orders table comment) - set by a
    delivery agent's PAID reply (main.py's _AGENT_PAYMENT_KEYWORDS) for a
    card-machine payment. Never required: stays NULL for cash-on-delivery."""
    conn = _get_conn()
    conn.execute("UPDATE orders SET payment_status = ? WHERE id = ?", (payment_status, order_id))
    conn.commit()


# ---- Delivery agents ----
# The restaurant's registered delivery riders. Each drives their assigned
# order's status (packed -> picked_up -> delivered) by replying on WhatsApp -
# see main.py::handle_delivery_agent_message.

def list_delivery_agents(active_only: bool = False):
    conn = _get_conn()
    where = "WHERE active = 1" if active_only else ""
    cur = conn.execute(f"SELECT id, phone, name, active, created_at FROM delivery_agents {where} ORDER BY name")
    keys = ["id", "phone", "name", "active", "created_at"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_delivery_agent_by_phone(phone: str):
    conn = _get_conn()
    cur = conn.execute("SELECT id, phone, name, active, created_at FROM delivery_agents WHERE phone = ?", (phone,))
    row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "phone", "name", "active", "created_at"]
    return dict(zip(keys, row))


def add_delivery_agent(phone: str, name: str):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO delivery_agents (phone, name, active, created_at) VALUES (?, ?, 1, ?)
        ON CONFLICT(phone) DO UPDATE SET name = excluded.name, active = 1
    """, (phone, name, datetime.now(timezone.utc).isoformat()))
    conn.commit()


def set_delivery_agent_active(agent_id: int, active: bool):
    conn = _get_conn()
    conn.execute("UPDATE delivery_agents SET active = ? WHERE id = ?", (int(active), agent_id))
    conn.commit()


def get_next_available_delivery_agent():
    """Simplest v1 assignment: the first active agent. A small restaurant
    with one or two riders doesn't need real dispatch/load-balancing yet."""
    agents = list_delivery_agents(active_only=True)
    return agents[0] if agents else None


# ---- Admins ----

def get_admin_by_username(username: str):
    conn = _get_conn()
    cur = conn.execute(
        "SELECT id, username, password_hash, created_at FROM admins WHERE username = ?", (username,)
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "username", "password_hash", "created_at"]
    return dict(zip(keys, row))


def get_admin_by_id(admin_id: int):
    conn = _get_conn()
    cur = conn.execute(
        "SELECT id, username, password_hash, created_at FROM admins WHERE id = ?", (admin_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["id", "username", "password_hash", "created_at"]
    return dict(zip(keys, row))


def create_admin(username: str, password_hash: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO admins (username, password_hash, created_at) VALUES (?, ?, ?)",
        (username, password_hash, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def set_admin_password(admin_id: int, password_hash: str):
    conn = _get_conn()
    conn.execute("UPDATE admins SET password_hash = ? WHERE id = ?", (password_hash, admin_id))
    conn.commit()


def any_admin_exists() -> bool:
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM admins").fetchone()[0] > 0


# ---- Printer settings ----
# Single-row config (id is constrained to 1) for the restaurant's thermal
# printer connection, editable from the admin dashboard's Printer page.
# Not required to be configured - printer integration is deferred for this
# project, but the mechanism (and print_agent/) is copied over ready to go.

def get_printer_settings():
    conn = _get_conn()
    cur = conn.execute(
        "SELECT connection_type, printer_ip, printer_port, updated_at FROM printer_settings WHERE id = 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["connection_type", "printer_ip", "printer_port", "updated_at"]
    return dict(zip(keys, row))


def set_printer_settings(connection_type: str, printer_ip: str = None, printer_port: str = None):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO printer_settings (id, connection_type, printer_ip, printer_port, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            connection_type=excluded.connection_type,
            printer_ip=excluded.printer_ip,
            printer_port=excluded.printer_port,
            updated_at=excluded.updated_at
    """, (connection_type, printer_ip, printer_port, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ---- Billing connector settings ----
# Single-row config, same shape as printer_settings. Not required to be
# configured - this panel is informational for now; the actual on/off switch
# for billing/connector.py's best-effort push is the BILLING_API_BASE_URL env
# var (config.py), not this "enabled" flag - see main.py's
# _push_invoice_best_effort(). Wire this row up as authoritative once a real
# billing vendor implementation lands.

def get_billing_settings():
    conn = _get_conn()
    cur = conn.execute(
        "SELECT enabled, api_base_url, api_key, updated_at FROM billing_settings WHERE id = 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["enabled", "api_base_url", "api_key", "updated_at"]
    d = dict(zip(keys, row))
    d["enabled"] = bool(d["enabled"])
    return d


def set_billing_settings(enabled: bool, api_base_url: str = None, api_key: str = None):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO billing_settings (id, enabled, api_base_url, api_key, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            enabled=excluded.enabled,
            api_base_url=excluded.api_base_url,
            api_key=excluded.api_key,
            updated_at=excluded.updated_at
    """, (int(enabled), api_base_url, api_key, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ---- Store settings (editable from the admin Settings page) ----
# Single-row override on top of config.py's .env-sourced defaults - a value
# saved here takes precedence over the environment variable until changed
# again. See config.py's apply_store_settings_override(), called once at
# startup and again right after a save so the change takes effect without
# a restart.

def get_store_settings():
    conn = _get_conn()
    cur = conn.execute(
        "SELECT store_name, currency, delivery_fee, free_delivery_threshold, store_phone, updated_at "
        "FROM store_settings WHERE id = 1"
    )
    row = cur.fetchone()
    if not row:
        return None
    keys = ["store_name", "currency", "delivery_fee", "free_delivery_threshold", "store_phone", "updated_at"]
    return dict(zip(keys, row))


def set_store_settings(store_name: str = None, currency: str = None, delivery_fee: float = None,
                        free_delivery_threshold: float = None, store_phone: str = None):
    conn = _get_conn()
    conn.execute("""
        INSERT INTO store_settings (id, store_name, currency, delivery_fee, free_delivery_threshold, store_phone, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            store_name=excluded.store_name,
            currency=excluded.currency,
            delivery_fee=excluded.delivery_fee,
            free_delivery_threshold=excluded.free_delivery_threshold,
            store_phone=excluded.store_phone,
            updated_at=excluded.updated_at
    """, (store_name, currency, delivery_fee, free_delivery_threshold, store_phone, datetime.now(timezone.utc).isoformat()))
    conn.commit()


# ---- WhatsApp connection (Embedded Signup / Coexistence) ----
# Single-row, same shape as store_settings/billing_settings. Overrides the
# .env-configured WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID once a
# restaurant connects via Embedded Signup - see whatsapp/coexistence.py and
# config.py's apply_whatsapp_connection_override().

def get_whatsapp_connection():
    conn = _get_conn()
    cur = conn.execute("""
        SELECT access_token, phone_number_id, waba_id, is_coexistence,
               history_sync_status, connected_at, updated_at
        FROM whatsapp_connection WHERE id = 1
    """)
    row = cur.fetchone()
    if not row:
        return None
    keys = ["access_token", "phone_number_id", "waba_id", "is_coexistence",
            "history_sync_status", "connected_at", "updated_at"]
    d = dict(zip(keys, row))
    d["is_coexistence"] = bool(d["is_coexistence"])
    return d


def set_whatsapp_connection(access_token: str, phone_number_id: str, waba_id: str = None,
                             is_coexistence: bool = False):
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
        INSERT INTO whatsapp_connection
            (id, access_token, phone_number_id, waba_id, is_coexistence, history_sync_status, connected_at, updated_at)
        VALUES (1, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            access_token=excluded.access_token,
            phone_number_id=excluded.phone_number_id,
            waba_id=excluded.waba_id,
            is_coexistence=excluded.is_coexistence,
            history_sync_status='pending',
            connected_at=excluded.connected_at,
            updated_at=excluded.updated_at
    """, (access_token, phone_number_id, waba_id, int(is_coexistence), now, now))
    conn.commit()


def set_whatsapp_history_sync_status(status: str):
    conn = _get_conn()
    conn.execute(
        "UPDATE whatsapp_connection SET history_sync_status = ?, updated_at = ? WHERE id = 1",
        (status, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def clear_whatsapp_connection():
    """Disconnects the Embedded Signup-connected number, falling back to the
    .env-configured WHATSAPP_ACCESS_TOKEN/WHATSAPP_PHONE_NUMBER_ID again."""
    conn = _get_conn()
    conn.execute("DELETE FROM whatsapp_connection WHERE id = 1")
    conn.commit()


# ---- Dashboard / analytics queries ----
# created_at is stored as ISO 8601 UTC. start/end below are "YYYY-MM-DD"
# strings (inclusive), compared as text - this works against ISO timestamps.

def get_stats(start: str = None, end: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)

    total_in = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='in' {where}", params).fetchone()[0]
    total_out = conn.execute(f"SELECT COUNT(*) FROM conversations WHERE direction='out' {where}", params).fetchone()[0]
    unique_customers = conn.execute(f"SELECT COUNT(DISTINCT phone) FROM conversations WHERE 1=1 {where}", params).fetchone()[0]
    orders_confirmed = conn.execute(
        f"SELECT COUNT(*) FROM orders WHERE status IN ('confirmed','packed','picked_up','delivered') {where}", params
    ).fetchone()[0]
    revenue = conn.execute(
        f"SELECT COALESCE(SUM(total), 0) FROM orders WHERE status IN ('confirmed','packed','picked_up','delivered') {where}", params
    ).fetchone()[0]

    return {
        "messages_received": total_in,
        "replies_sent": total_out,
        "unique_customers": unique_customers,
        "orders_confirmed": orders_confirmed,
        "revenue": revenue,
    }


def get_daily_counts(start: str = None, end: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT substr(created_at, 1, 10) AS day,
               SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END) AS received,
               SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS sent
        FROM conversations
        WHERE 1=1 {where}
        GROUP BY day
        ORDER BY day
    """, params)
    return [{"day": row[0], "received": row[1], "sent": row[2]} for row in cur.fetchall()]


def get_customers_summary(start: str = None, end: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT c.phone,
               COALESCE(cu.name, ''),
               COUNT(*) AS message_count,
               MAX(c.created_at) AS last_message_at,
               cu.last_location_label,
               cu.last_address_text,
               cu.last_lat,
               cu.last_lng
        FROM conversations c
        LEFT JOIN customers cu ON cu.phone = c.phone
        WHERE 1=1 {where}
        GROUP BY c.phone
        ORDER BY last_message_at DESC
    """, params)
    keys = ["phone", "name", "message_count", "last_message_at",
            "last_location_label", "last_address_text", "last_lat", "last_lng"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_conversation(phone: str, start: str = None, end: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT direction, message, created_at
        FROM conversations
        WHERE phone = ? {where}
        ORDER BY id ASC
    """, (phone, *params))
    keys = ["direction", "message", "created_at"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_all_messages(start: str = None, end: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT c.created_at, c.phone, COALESCE(cu.name, ''), c.direction, c.message
        FROM conversations c
        LEFT JOIN customers cu ON cu.phone = c.phone
        WHERE 1=1 {where}
        ORDER BY c.created_at ASC
    """, params)
    keys = ["created_at", "phone", "name", "direction", "message"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_orders_since(last_seen_id: int) -> list:
    """Confirmed-or-later orders with id > last_seen_id, oldest first -
    powers the admin dashboard's live new-order alert (popup + beep), polled
    from the browser every few seconds (see base.html). Only confirmed+
    orders count as "new" here (not draft/awaiting_confirmation), since
    those aren't real orders yet."""
    conn = _get_conn()
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders
        WHERE id > ? AND status IN ('confirmed', 'packed', 'picked_up', 'delivered')
        ORDER BY id ASC
    """, (last_seen_id,))
    return [_order_row_to_dict(row) for row in cur.fetchall()]


def get_latest_order_id() -> int:
    """Highest order id that currently exists (any status) - used to
    initialize the dashboard's "last seen" baseline on first page load, so
    the alert doesn't fire for every pre-existing order the moment the admin
    opens the dashboard."""
    conn = _get_conn()
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM orders").fetchone()
    return row[0]


def get_orders(start: str = None, end: str = None, status: str = None):
    conn = _get_conn()
    where, params = _date_where(start, end)
    if status:
        where += " AND status = ?"
        params = list(params) + [status]
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders
        WHERE 1=1 {where}
        ORDER BY created_at DESC
    """, params)
    return [_order_row_to_dict(row) for row in cur.fetchall()]


def get_customer_orders(phone: str, start: str = None, end: str = None):
    """All orders for one customer, newest first - used for the per-customer
    order history view (grouped further by get_customer_orders_by_month)."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT {_ORDER_COLUMNS}
        FROM orders
        WHERE phone = ? {where}
        ORDER BY created_at DESC
    """, (phone, *params))
    return [_order_row_to_dict(row) for row in cur.fetchall()]


def get_customer_orders_by_month(phone: str, start: str = None, end: str = None):
    """One row per calendar month this customer has orders in, newest first -
    same substr(created_at, 1, N) grouping style as get_daily_counts, just
    grouped by month (YYYY-MM) instead of day, and scoped to one phone."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT substr(created_at, 1, 7) AS year_month,
               COUNT(*) AS order_count,
               COALESCE(SUM(total), 0) AS total_spent
        FROM orders
        WHERE phone = ? {where}
        GROUP BY year_month
        ORDER BY year_month DESC
    """, (phone, *params))
    return [{"year_month": row[0], "order_count": row[1], "total_spent": row[2]} for row in cur.fetchall()]


def get_orders_by_month(start: str = None, end: str = None):
    """Store-wide version of get_customer_orders_by_month - one row per
    calendar month across all customers, for the Reports yearly rollup."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT substr(created_at, 1, 7) AS year_month,
               COUNT(*) AS order_count,
               COALESCE(SUM(total), 0) AS total_revenue,
               COUNT(DISTINCT phone) AS unique_customers
        FROM orders
        WHERE status IN ('confirmed', 'packed', 'picked_up', 'delivered') {where}
        GROUP BY year_month
        ORDER BY year_month DESC
    """, params)
    keys = ["year_month", "order_count", "total_revenue", "unique_customers"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_top_selling_items(start: str = None, end: str = None, limit: int = 10):
    """Best-selling menu items by quantity sold, for the Reports monthly
    summary - grouped over order_items joined to confirmed+ orders in range."""
    conn = _get_conn()
    where, params = _date_where(start, end)
    cur = conn.execute(f"""
        SELECT oi.item_name_snapshot AS name,
               SUM(oi.qty) AS total_qty,
               COALESCE(SUM(oi.line_total), 0) AS total_revenue
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE o.status IN ('confirmed', 'packed', 'picked_up', 'delivered') {where}
        GROUP BY oi.item_name_snapshot
        ORDER BY total_qty DESC
        LIMIT ?
    """, (*params, limit))
    keys = ["name", "total_qty", "total_revenue"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def get_customer_favorite_items(phone: str, limit: int = 5):
    """A customer's most-ordered menu items by quantity - purchase-behavior
    signal shown on their dashboard profile, same join shape as
    get_top_selling_items but scoped to one phone, across all time."""
    conn = _get_conn()
    cur = conn.execute("""
        SELECT oi.item_name_snapshot AS name,
               SUM(oi.qty) AS total_qty,
               COALESCE(SUM(oi.line_total), 0) AS total_spent
        FROM order_items oi
        JOIN orders o ON o.id = oi.order_id
        WHERE o.phone = ? AND o.status IN ('confirmed', 'packed', 'picked_up', 'delivered')
        GROUP BY oi.item_name_snapshot
        ORDER BY total_qty DESC
        LIMIT ?
    """, (phone, limit))
    keys = ["name", "total_qty", "total_spent"]
    return [dict(zip(keys, row)) for row in cur.fetchall()]


def _date_where(start: str, end: str):
    clauses, params = [], []
    if start:
        clauses.append("created_at >= ?")
        params.append(f"{start}T00:00:00")
    if end:
        clauses.append("created_at <= ?")
        params.append(f"{end}T23:59:59.999999")
    where = ("AND " + " AND ".join(clauses)) if clauses else ""
    return where, params
