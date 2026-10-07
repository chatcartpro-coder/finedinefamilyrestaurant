"""
Stock data layer - the "database" the AI checks before quoting availability
or price. Populated via Excel import (catalog/excel_import.py) today; a real
POS API can replace that import path later without touching this module or
the AI/webhook code that reads from it (see catalog/pos_client.py).
"""
import sqlite3
from datetime import datetime, timezone

from rapidfuzz import fuzz

from storage.store import _get_conn

# Below this score (0-100), a fuzzy match is more likely noise than a real
# spelling variant - tuned against real customer typos observed live (e.g.
# "idli" vs the menu's "Idly Set" scores 75, "set idli" vs "Idly Set" scores
# 87.5, both comfortably above unrelated items in the low 40s-60s).
_FUZZY_MATCH_THRESHOLD = 65

# Common conversational English words never worth fuzzy-matching against
# dish names - confirmed live: "hello" fuzzy-matched "Bhel Puri" and "whats"
# fuzzy-matched "Wheat Porotta" purely on coincidental letter overlap, which
# broke the "no items found -> show menu categories" fallback for a generic
# "what's on the menu?" question (ai/agent.py's MENU_BROWSE_PHRASES path).
_FUZZY_STOP_WORDS = {
    "hello", "hey", "hi", "please", "thanks", "thank", "want", "would",
    "like", "have", "whats", "what", "menu", "today", "order", "give",
    "need", "could", "can", "there", "available", "about", "your",
}


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


def list_items_by_category(category: str, in_stock_only: bool = True) -> list:
    """All items in one exact category (case-insensitive), name order - used
    when a customer asks for a category by name (e.g. "meals", "breakfast")
    so the AI can list every real option in that category cleanly, instead
    of a noisy keyword search that mixes in unrelated items that happen to
    fuzzy-match (confirmed live: searching "meals" returned several
    different Meal items correctly, but also unrelated fish dishes mixed
    into the same result list, which the AI then struggled to resolve into
    a clean answer and silently failed to add anything)."""
    _init_schema()
    conn = _get_conn()
    where = "WHERE category = ? COLLATE NOCASE"
    if in_stock_only:
        where += " AND in_stock = 1 AND stock_qty > 0"
    cur = conn.execute(f"""
        SELECT id, sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at
        FROM catalog_items
        {where}
        ORDER BY name
    """, (category,))
    return [_row_to_dict(row) for row in cur.fetchall()]


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
    """Exact substring match over name/category first (fast, precise); if
    that finds nothing, falls back to fuzzy matching (rapidfuzz) over all
    item names so common spelling variants of transliterated dish names
    still resolve - confirmed live: a customer typing "idli" found nothing
    against the menu's "Idly Set", a very common spelling difference for
    South Indian dishes transliterated from script. Fuzzy results are
    ordered by match score (best first), restricted to in-stock items, and
    only returned above _FUZZY_MATCH_THRESHOLD so unrelated items don't
    surface just because fuzzy matching is lenient."""
    _init_schema()
    query = query.strip()
    if not query:
        return []
    conn = _get_conn()
    like = f"%{query}%"
    cur = conn.execute("""
        SELECT id, sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at
        FROM catalog_items
        WHERE name LIKE ? OR category LIKE ?
        ORDER BY in_stock DESC, name
        LIMIT ?
    """, (like, like, limit))
    results = [_row_to_dict(row) for row in cur.fetchall()]
    if results:
        return results

    # Don't fuzzy-match a query that's entirely conversational filler (e.g.
    # "hello", "whats on the menu") - nothing meaningful would survive to
    # score against dish names anyway, and this is what lets a genuine
    # "no specific dish named" message correctly find zero items (see
    # ai/agent.py's category-browse fallback for MENU_BROWSE_PHRASES).
    query_words = [w for w in query.lower().split() if len(w) >= 3]
    if query_words and all(w in _FUZZY_STOP_WORDS for w in query_words):
        return []

    return _fuzzy_search_items(query, limit)


def _word_match_score(query: str, name: str) -> float:
    """Average, over each significant word in the query, of that word's best
    fuzz.ratio against any significant word in the item name - handles
    spelling variants of individual dish-name words (idli/idly, dosa/dossa)
    without the false-positive noise plain whole-string fuzzy scorers (e.g.
    partial_ratio) produce for short queries against a few hundred mostly-
    unrelated item names (confirmed: partial_ratio alone ranked "Chilli
    Potato" above "Idly Set" for the query "idli"). Words under 3 characters
    are ignored on both sides - too short to carry real signal."""
    q_words = [w for w in query.lower().split() if len(w) >= 3]
    n_words = [w.strip("()") for w in name.lower().split() if len(w.strip("()")) >= 3]
    if not q_words or not n_words:
        return 0.0
    return sum(max(fuzz.ratio(qw, nw) for nw in n_words) for qw in q_words) / len(q_words)


def _fuzzy_search_items(query: str, limit: int) -> list:
    conn = _get_conn()
    cur = conn.execute("""
        SELECT id, sku, name, category, unit, price, stock_qty, in_stock, image_url, source, updated_at
        FROM catalog_items WHERE in_stock = 1 AND stock_qty > 0
    """)
    rows = [_row_to_dict(row) for row in cur.fetchall()]
    scored = [(r, _word_match_score(query, r["name"])) for r in rows]
    scored = [(r, s) for r, s in scored if s >= _FUZZY_MATCH_THRESHOLD]
    scored.sort(key=lambda pair: -pair[1])
    return [r for r, _s in scored[:limit]]


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
