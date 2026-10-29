"""Bulk import of products from a CSV or Excel (.xlsx) file.

Columns (header names are matched loosely, any order):
  name (required) | category | unit | price | stock | barcode | track_stock
Rows with the same name are the categories of one product. Existing products/categories (matched by name) are
updated: price (logged in the price history), stock if given, and barcodes are added. Nothing is ever deleted.
"""

import csv
import io
import re
from decimal import Decimal, InvalidOperation

from django.db import transaction

from .models import Barcode, PriceHistory, Product, Unit, Variant

MAX_ROWS = 20000

HEADERS = {
    "name": {"name", "product", "product name", "item", "item name", "title"},
    "category": {"category", "variant", "grade", "type", "size", "variety"},
    "unit": {"unit", "uom"},
    "price": {"price", "rate", "sale price", "selling price", "mrp"},
    "stock": {"stock", "qty", "quantity", "inventory", "in stock"},
    "barcode": {"barcode", "barcodes", "code", "qr", "qr code", "sku", "ean"},
    "track_stock": {"track stock", "track_stock", "track"},
}

UNIT_WORDS = {
    "kg": "kg", "kilo": "kg", "kgs": "kg", "g": "g", "gm": "g", "gram": "g", "l": "l", "ltr": "l", "litre": "l",
    "liter": "l", "piece": "piece", "pc": "piece", "pcs": "piece", "pieces": "piece", "meter": "meter", "m": "meter",
    "metre": "meter", "dozen": "dozen", "dz": "dozen", "pack": "pack", "packet": "pack", "box": "pack",
}
assert set(UNIT_WORDS.values()) <= {u.value for u in Unit}


class ImportFileError(Exception):
    pass


def _norm_header(h):
    return re.sub(r"\s+", " ", str(h or "").strip().lower().replace("_", " "))


def _cell(value):
    """Cell value as clean text. Excel stores long codes as numbers; turn 8901234567890.0 back into text."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_rows(upload):
    """Return a list of dicts with canonical keys, one per data row, plus the sheet row number."""
    name = (upload.name or "").lower()
    data = upload.read()
    if len(data) > 8 * 1024 * 1024:
        raise ImportFileError("File is too big (max 8 MB).")
    if name.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook

            wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
            raw = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
        except Exception as exc:
            raise ImportFileError("Could not read this Excel file. Save it as .xlsx or .csv and try again.") from exc
    elif name.endswith(".csv") or name.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        raw = list(csv.reader(io.StringIO(text), dialect))
    else:
        raise ImportFileError("Upload a .csv or .xlsx file.")

    raw = [r for r in raw if any(_cell(c) for c in r)]
    if not raw:
        raise ImportFileError("The file is empty.")
    mapping = {}
    for idx, h in enumerate(raw[0]):
        key = _norm_header(h)
        for canon, names in HEADERS.items():
            if key in names and canon not in mapping:
                mapping[canon] = idx
    if "name" not in mapping:
        raise ImportFileError('The first row must have a "name" column (also accepted: product, item).')
    if len(raw) - 1 > MAX_ROWS:
        raise ImportFileError(f"Too many rows (max {MAX_ROWS}). Split the file.")

    rows = []
    for n, r in enumerate(raw[1:], start=2):
        rows.append({"row": n, **{k: _cell(r[i]) if i < len(r) else "" for k, i in mapping.items()}})
    return rows


def _decimal(text, what):
    try:
        d = Decimal(text.replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"{what} “{text}” is not a number.")
    if d < 0:
        raise ValueError(f"{what} cannot be negative.")
    return d


def _split_codes(text):
    return [c.strip() for c in re.split(r"[;|,\n]+", text) if c.strip()]


def run(rows, apply=False):
    """Check every row (and write the good ones when apply=True). Returns the summary dict."""
    products = {p.name.lower(): p for p in Product.objects.all()}
    variants = {(v.product_id, v.name.lower()): v for v in Variant.objects.all()}
    owners = {c: vid for c, vid in Barcode.objects.values_list("code", "variant_id")}  # code -> variant id

    new_products, new_variants, updated, errors = set(), 0, 0, []
    planned = {}  # (product name lower, category lower) -> row number, to spot duplicates inside the file
    planned_codes = {}  # code -> (product, category) planned in this file

    def fail(row, msg):
        errors.append({"row": row["row"], "message": msg})

    with transaction.atomic():
        for row in rows:
            name = row.get("name", "").strip()
            if not name:
                fail(row, "Name is empty.")
                continue
            category = (row.get("category") or "Standard").strip() or "Standard"
            key = (name.lower(), category.lower())
            if key in planned:
                fail(row, f"Same product and category already appears on row {planned[key]}.")
                continue
            try:
                unit = UNIT_WORDS.get((row.get("unit") or "piece").strip().lower())
                if unit is None:
                    raise ValueError(f"Unit “{row.get('unit')}” is not known (use kg, g, l, piece, meter, dozen, pack).")
                price = _decimal(row["price"], "Price") if row.get("price") else None
                stock = _decimal(row["stock"], "Stock") if row.get("stock") else None
                codes = _split_codes(row.get("barcode", ""))
                for c in codes:
                    if re.fullmatch(r"\d(\.\d+)?[eE]\+\d+", c):
                        raise ValueError(f"Barcode “{c}” was turned into a number by Excel. Format the column as Text.")
            except ValueError as exc:
                fail(row, str(exc))
                continue

            product = products.get(name.lower())
            variant = variants.get((product.id, category.lower())) if product else None
            if variant is None and price is None:
                fail(row, "Price is required for a new product or category.")
                continue

            clash = None
            for c in codes:
                if c in planned_codes and planned_codes[c] != key:
                    clash = f"Code {c} is also used by another row in this file."
                elif c in owners and (variant is None or owners[c] != variant.id):
                    other = Barcode.objects.select_related("variant__product").get(code=c)
                    clash = f"Code {c} already belongs to {other.variant.product.name} ({other.variant.name})."
                if clash:
                    break
            if clash:
                fail(row, clash)
                continue

            planned[key] = row["row"]
            for c in codes:
                planned_codes[c] = key
            track = (row.get("track_stock") or "").strip().lower()
            track_stock = True if track in ("yes", "y", "true", "1") else False if track in ("no", "n", "false", "0") else None

            if product is None:
                new_products.add(name.lower())
            if variant is None:
                new_variants += 1
            elif price is not None and price != variant.price or stock is not None or codes:
                updated += 1

            if apply:
                if product is None:
                    product = Product.objects.create(
                        name=name, unit=unit,
                        track_stock=track_stock if track_stock is not None else stock is not None,
                    )
                    products[name.lower()] = product
                elif track_stock is not None or (stock is not None and not product.track_stock):
                    product.track_stock = track_stock if track_stock is not None else True
                    product.save(update_fields=["track_stock"])
                if variant is None:
                    variant = Variant.objects.create(product=product, name=category, price=price, stock=stock or 0)
                    variants[(product.id, category.lower())] = variant
                else:
                    if price is not None and price != variant.price:
                        PriceHistory.objects.create(variant=variant, old_price=variant.price, new_price=price)
                        variant.price = price
                    if stock is not None:
                        variant.stock = stock
                    variant.save()
                for c in codes:
                    if c not in owners:
                        Barcode.objects.create(variant=variant, code=c)
                        owners[c] = variant.id

    return {
        "dry_run": not apply,
        "rows": len(rows),
        "new_products": len(new_products),
        "new_categories": new_variants,
        "updated": updated,
        "skipped": len(errors),
        "errors": errors[:50],
    }
