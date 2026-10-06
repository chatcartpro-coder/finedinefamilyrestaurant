"""
Stock data layer - the "database" the AI checks before quoting availability
or price. Populated via Excel import (catalog/excel_import.py) today; a real
POS API can replace that import path later without touching this module or
the AI/webhook code that reads from it (see catalog/pos_client.py).
"""
import sqlite3
from datetime import datetime, timezone

from storage.store import _get_conn


def _init_schema():
    conn = _get_conn()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS catalog_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sku TEXT UNIQUE,
        name TEXT NOT NULL,
        category TEXT,
        unit TEXT,
        price REAL NOT NULL DEFAULT 0,
        stock_qty REAL NOT NULL DEFAULT 0,
        in_stock INTEGER NOT NULL DEFAULT 1,
        image_url TEXT,
        source TEXT DEFAULT 'manual',
        updated_at TEXT
    );
    """)
    conn.commit()


def list_items(in_stock_only: bool = False):
    _init_schema()
    conn = _get_conn()
    where = "WHERE in_stock = 1 AND stock_qty > 0" if in_stock_only else ""
    cur = conn.execute(f"""
        SELECT id, sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at
        FROM catalog_items {where}
        ORDER BY category, name
    """)
    return [_row_to_dict(row) for row in cur.fetchall()]


def list_categories(in_stock_only: bool = True) -> list:
    """Distinct category names with at least one item, in stock-first/name
    order - used as a fallback when a customer asks a generic "what's on the
    menu?" question that doesn't match any specific dish by keyword (see
    ai/agent.py's search_catalog_for_message), so the AI can list categories
    instead of claiming nothing is available."""
    _init_schema()
    conn = _get_conn()
    where = "WHERE category IS NOT NULL AND category != '' AND in_stock = 1 AND stock_qty > 0" if in_stock_only else "WHERE category IS NOT NULL AND category != ''"
    cur = conn.execute(f"SELECT DISTINCT category FROM catalog_items {where} ORDER BY category")
    return [row[0] for row in cur.fetchall()]


def get_item(item_id: int):
    _init_schema()
    conn = _get_conn()
    cur = conn.execute("""
        SELECT id, sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at
        FROM catalog_items WHERE id = ?
    """, (item_id,))
    row = cur.fetchone()
    return _row_to_dict(row) if row else None


def search_items(query: str, limit: int = 8):
    """Simple substring match over name/category - good enough for a grocery
    catalog of a few hundred items; swap for fuzzy/embedding search if the
    catalog grows large or customers' phrasing drifts further from item names."""
    _init_schema()
    conn = _get_conn()
    like = f"%{query.strip()}%"
    cur = conn.execute("""
        SELECT id, sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at
        FROM catalog_items
        WHERE name LIKE ? OR category LIKE ?
        ORDER BY in_stock DESC, name
        LIMIT ?
    """, (like, like, limit))
    return [_row_to_dict(row) for row in cur.fetchall()]


def is_empty() -> bool:
    _init_schema()
    conn = _get_conn()
    return conn.execute("SELECT COUNT(*) FROM catalog_items").fetchone()[0] == 0


def upsert_item(name: str, price: float, stock_qty: float, sku: str = None, category: str = None,
                 unit: str = None, image_url: str = None, source: str = "manual"):
    _init_schema()
    conn = _get_conn()
    in_stock = 1 if stock_qty > 0 else 0
    now = datetime.now(timezone.utc).isoformat()

    if sku:
        conn.execute("""
            INSERT INTO catalog_items (sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                name=excluded.name, category=excluded.category, unit=excluded.unit,
                price=excluded.price, stock_qty=excluded.stock_qty, in_stock=excluded.in_stock,
                image_url=excluded.image_url, source=excluded.source, updated_at=excluded.updated_at
        """, (sku, name, category, unit, price, stock_qty, in_stock, image_url, source, now))
    else:
        # No SKU to key off - match by exact name instead, else insert new.
        existing = conn.execute("SELECT id FROM catalog_items WHERE sku IS NULL AND name = ?", (name,)).fetchone()
        if existing:
            conn.execute("""
                UPDATE catalog_items SET category=?, unit=?, price=?, stock_qty=?, in_stock=?,
                    image_url=?, source=?, updated_at=? WHERE id=?
            """, (category, unit, price, stock_qty, in_stock, image_url, source, now, existing[0]))
        else:
            conn.execute("""
                INSERT INTO catalog_items (sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at)
                VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (name, category, unit, price, stock_qty, in_stock, image_url, source, now))
    conn.commit()


def set_stock(item_id: int, stock_qty: float):
    _init_schema()
    conn = _get_conn()
    in_stock = 1 if stock_qty > 0 else 0
    conn.execute(
        "UPDATE catalog_items SET stock_qty = ?, in_stock = ?, updated_at = ? WHERE id = ?",
        (stock_qty, in_stock, datetime.now(timezone.utc).isoformat(), item_id),
    )
    conn.commit()


def last_synced_at():
    _init_schema()
    conn = _get_conn()
    row = conn.execute("SELECT MAX(updated_at) FROM catalog_items").fetchone()
    return row[0] if row else None


def _row_to_dict(row):
    keys = ["id", "sku", "name", "category", "unit", "price", "stock_qty", "in_stock", "image_url", "source", "updated_at"]
    return dict(zip(keys, row))
