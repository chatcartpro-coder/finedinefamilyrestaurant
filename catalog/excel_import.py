"""
Imports stock data from an Excel/CSV file exported from the store's POS
(or maintained manually by staff) into catalog_items. This is the practical
"fetch stock from POS" mechanism until a real POS API is wired up via
catalog/pos_client.py.

Expected columns (header row, case-insensitive, any order):
    SKU | Name | Category | Unit | Price | Stock Qty

Usage:
    python -m catalog.excel_import path/to/file.xlsx
"""
import sys

from openpyxl import load_workbook

from catalog import store as catalog_store

REQUIRED_COLUMNS = {"name", "price", "stock_qty"}


def _normalize_key(header) -> str:
    return str(header).strip().lower().replace(" ", "_")


def import_file(path: str, source: str = "excel") -> tuple[int, list]:
    """Returns (count_imported, skipped_rows) - skipped_rows is a list of
    (row_number, reason) for any row that wasn't imported, so a bad/blank
    cell shows up as a visible warning on the admin page instead of
    silently zeroing out a real item's price. Confirmed live: a row with a
    blank or non-numeric Price cell previously got coerced straight to
    0.0 with upsert_item() overwriting the existing price unconditionally
    on every re-import - a live menu item could go from a real price to
    AED 0.00 from one re-upload with no error shown anywhere."""
    wb = load_workbook(path, data_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return 0, []

    headers = [_normalize_key(h) for h in rows[0]]
    missing = REQUIRED_COLUMNS - set(headers)
    if missing:
        raise ValueError(
            f"Import file is missing required column(s): {', '.join(sorted(missing))}. "
            f"Expected headers like: SKU, Name, Category, Unit, Price, Stock Qty"
        )

    count = 0
    skipped = []
    for row_num, raw_row in enumerate(rows[1:], start=2):
        if raw_row is None or all(v is None for v in raw_row):
            continue
        record = dict(zip(headers, raw_row))

        name = str(record.get("name") or "").strip()
        if not name:
            skipped.append((row_num, "missing item name"))
            continue

        price_raw = record.get("price")
        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            skipped.append((row_num, f"'{name}': invalid/blank price ({price_raw!r}) - kept existing price, not overwritten"))
            continue
        if price < 0:
            skipped.append((row_num, f"'{name}': negative price ({price_raw!r}) - kept existing price, not overwritten"))
            continue

        try:
            stock_qty = float(record.get("stock_qty") or 0)
        except (TypeError, ValueError):
            stock_qty = 0.0

        sku = str(record.get("sku")).strip() if record.get("sku") not in (None, "") else None
        category = str(record.get("category") or "").strip() or None
        unit = str(record.get("unit") or "").strip() or None

        catalog_store.upsert_item(
            name=name, price=price, stock_qty=stock_qty, sku=sku,
            category=category, unit=unit, source=source,
        )
        count += 1

    return count, skipped


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m catalog.excel_import path/to/file.xlsx")
        sys.exit(1)
    path = sys.argv[1]
    count, skipped = import_file(path)
    print(f"Imported/updated {count} catalog item(s) from {path}")
    for row_num, reason in skipped:
        print(f"  SKIPPED row {row_num}: {reason}")


if __name__ == "__main__":
    main()
