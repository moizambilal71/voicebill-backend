"""Voice pipeline: audio -> text (OpenAI) -> structured actions (OpenAI) -> resolved proposal.

The AI never writes to the database. It only proposes actions. This module validates
every id the AI returns against the real catalog/bill and marks what still needs the
shopkeeper (which category? what price?). The frontend shows the proposal and only
calls the normal API endpoints after the shopkeeper confirms.
"""

import json
import re
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .models import Bill, Product, Unit, Variant

VALID_UNITS = {u.value for u in Unit}


class VoiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- OpenAI calls

def _client():
    if not settings.OPENAI_API_KEY:
        raise VoiceError("OPENAI_API_KEY is not set in the backend .env file.", 503)
    from openai import OpenAI

    return OpenAI(api_key=settings.OPENAI_API_KEY, timeout=60, max_retries=1)


def transcribe(shop, filename, data, content_type):
    """Audio bytes -> text."""
    hint_names = []
    for p in Product.objects.filter(shop=shop).prefetch_related("aliases", "variants")[:80]:
        hint_names.append(p.name)
        hint_names.extend(a.text for a in p.aliases.all())
    prompt = (
        "Pakistani local shop billing. Speech is Urdu mixed with English. "
        "Items and units: " + ", ".join(hint_names[:120]) + ", kilo, gram, pao, aadha, dhai, suit, rupay."
    )
    kwargs = {
        "model": settings.OPENAI_TRANSCRIBE_MODEL,
        "file": (filename, data, content_type or "audio/webm"),
        "prompt": prompt[:900],
    }
    if settings.VOICE_LANGUAGE:
        kwargs["language"] = settings.VOICE_LANGUAGE
    result = _client().audio.transcriptions.create(**kwargs)
    return (getattr(result, "text", "") or "").strip()


SYSTEM_PROMPT = """You are the voice command parser for a billing app used by shopkeepers in Pakistan.
The shopkeeper speaks Urdu / Roman Urdu / Hindi / English, often mixed. The text may be in Urdu script
or Latin script. You receive the spoken text, the shop catalog, the current bill and, sometimes, a pending
proposal. Return ONLY a JSON object: {"actions": [ ... ]}. You never invent product or variant ids: use only ids from the catalog,
or null when nothing matches.

ACTION TYPES
1. {"type":"add_items","items":[{"spoken_name":str,"product_id":int|null,"variant_id":int|null,"quantity":number|null,"unit":str|null,"price":number|null}]}
   Add things to the bill. "price" is the price of ONE unit (1 kg, 1 piece, 1 pair...) and only if the shopkeeper said one, else null.
   "quantity" is only what the shopkeeper said, else null.
2. {"type":"edit_item","bill_item_id":int,"quantity":number|null,"price":number|null}
   Change a line already on the bill (ids are in CURRENT BILL).
3. {"type":"remove_item","bill_item_id":int}
4. {"type":"update_catalog_price","product_id":int,"variant_id":int|null,"new_price":number}
   ONLY when the shopkeeper clearly asks to change the saved/permanent price ("cheeni ki price 150 kar do", "price update karo").
5. {"type":"price_scope","product_id":int,"variant_id":int|null,"price":number}
   When a price is mentioned for a catalog product and you cannot tell if it is for this bill only or a permanent change
   (e.g. just "cheeni 130 kar do"). Do NOT guess.
6. {"type":"add_product","name":str,"unit":str,"track_stock":bool,"variants":[{"name":str,"price":number,"stock":number}]}
   Adding a new item to the catalog/inventory. Categories become variants; no categories -> one variant named "Standard".
7. {"type":"confirm_bill"}  (e.g. "bill bana do", "bill confirm karo", "ho gaya")
8. {"type":"cancel_bill"}
9. {"type":"clarify","question":str}  Use when the command is unclear. Write the question in Roman Urdu.
10. {"type":"unknown"}  Not a billing command.

RULES
- MASTER RULE: if the shopkeeper says a price for an item, put it in "price" (it is used on the bill only; the catalog is not changed).
  If no price is said, set price null (the catalog price will be used). A bargained price ("130 laga do") is a bill price, not a catalog update.
- Match catalog items by name or alias, tolerating spelling and language differences (cheeni = chini = sugar = چینی).
- A spoken number like "150 wala daliya" is the price of that item; set price 150 and leave variant_id null unless a category name was said.
- If the shopkeeper names a category (alias or variant name), set variant_id. If an item has several variants and none was named, leave variant_id null.
- Item not in the catalog: product_id null, keep spoken_name as spoken (translate to a short English/Roman name), keep price if said.
- Convert quantities to the product's unit: aadha kilo = 0.5 kg, pao = 0.25 kg, dhai kilo = 2.5 kg, 250 gram = 0.25 kg, ek dozen = 1 dozen.
  NEVER assume a quantity: if the shopkeeper did not say how many / how much, quantity is null (the app will ask).
  "ek", "one", "a" count as an explicit 1. For non-catalog items use unit "piece" unless another unit is clear.
- Prices are always for ONE unit. If the shopkeeper gives the total for the whole quantity ("dono ke 3000", "total 3000"),
  divide: price = total / quantity. Never ask for a total.
- Later words in the same sentence override earlier ones. If a price or quantity is corrected ("catcher 200 ka... nahi 100 kar do",
  "catcher ki price 100 kar do"), output that item ONCE with the FINAL value. Never output the same item twice with different prices
  because of a correction. A correction of an item that is only being added in this sentence is NOT an edit_item or update_catalog_price.
- PENDING PROPOSAL: when "pending_proposal" is not empty the shopkeeper is looking at it. Their words are usually an answer to a missing
  detail (quantity, price, category) or a correction. Return the COMPLETE updated list of actions (keep everything that was not changed,
  with its quantity/price/variant_id), not just the change. If the words are clearly a new, unrelated command, return only the new actions.
  A price said about an item in the pending proposal changes that item in the proposal; it is not a catalog update unless the shopkeeper
  says it is permanent.

EXAMPLES
"sugar ka bill bana do" -> {"actions":[{"type":"add_items","items":[{"spoken_name":"sugar","product_id":<sugar id>,"variant_id":null,"quantity":null,"unit":"kg","price":null}]}]}
"ek catcher 200 ka, ek lipstick 500 ki, catcher ki price 100 kar do" ->
  {"actions":[{"type":"add_items","items":[{"spoken_name":"catcher","product_id":null,"variant_id":null,"quantity":1,"unit":"piece","price":100},{"spoken_name":"lipstick","product_id":null,"variant_id":null,"quantity":1,"unit":"piece","price":500}]}]}
"2 t-shirt aur 1 shoe ka bill bana do" -> two items, quantity 2 and 1, unit "piece", price null (the app asks the price of ONE piece).
pending has sugar with quantity null; shopkeeper says "paanch kilo" -> same actions, sugar quantity 5.
- Valid units: kg, g, l, piece, meter, dozen, pack.
- One utterance can contain several actions, in the order spoken.
- Numbers may be in words (sau, do sau, dhai sau) or Urdu digits; output plain numbers.
"""


def _chat_json(messages):
    resp = _client().chat.completions.create(
        model=settings.OPENAI_CHAT_MODEL,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0,
    )
    return json.loads(resp.choices[0].message.content or "{}")


CATALOG_FULL_LIMIT = 200  # up to this many products are sent whole; bigger catalogs are narrowed down
CATALOG_MATCH_LIMIT = 150


def _relevant_products(shop, text, bill, pending):
    """For big catalogs only send the products that the sentence (or the open bill/proposal) could be about."""
    products = list(Product.objects.filter(shop=shop).prefetch_related("variants", "aliases"))
    if len(products) <= CATALOG_FULL_LIMIT:
        return products
    tokens = {t for t in re.findall(r"\w+", (text or "").lower()) if len(t) >= 2}
    keep_ids = set()
    if bill:
        keep_ids |= {i.variant.product_id for i in bill.items.select_related("variant") if i.variant}
    for action in pending if isinstance(pending, list) else []:
        for item in (action.get("items") or []) if isinstance(action, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("product_id"), int):
                keep_ids.add(item["product_id"])
        if isinstance(action, dict) and isinstance(action.get("product_id"), int):
            keep_ids.add(action["product_id"])
    matched = []
    for p in products:
        hay = " ".join([p.name] + [a.text for a in p.aliases.all()] + [v.name for v in p.variants.all()]).lower()
        if p.id in keep_ids or any(t in hay for t in tokens):
            matched.append(p)
    return matched[:CATALOG_MATCH_LIMIT] or products[:CATALOG_MATCH_LIMIT]


def _catalog_context(shop, text="", bill=None, pending=None):
    out = []
    for p in _relevant_products(shop, text, bill, pending):
        out.append({
            "product_id": p.id,
            "name": p.name,
            "unit": p.unit,
            "aliases": [a.text for a in p.aliases.all() if a.variant_id is None],
            "variants": [
                {
                    "variant_id": v.id,
                    "name": v.name,
                    "price": float(v.price),
                    "aliases": [a.text for a in p.aliases.all() if a.variant_id == v.id],
                }
                for v in p.variants.all()
            ],
        })
    return out


def _bill_context(bill):
    if not bill:
        return []
    return [
        {
            "bill_item_id": i.id,
            "name": i.item_name,
            "quantity": float(i.quantity),
            "price": float(i.sold_price),
            "variant_id": i.variant_id,
        }
        for i in bill.items.all()
    ]


def interpret(text, shop, bill=None, pending=None):
    text = (text or "").strip()
    if not text:
        raise VoiceError("Nothing was heard. Please try again.")
    payload = {
        "spoken_text": text,
        "catalog": _catalog_context(shop, text, bill, pending),
        "current_bill": _bill_context(bill),
        "pending_proposal": pending if isinstance(pending, list) else [],
    }
    raw = _chat_json([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ])
    actions = raw.get("actions") if isinstance(raw, dict) else None
    if not isinstance(actions, list):
        actions = []
    return {"transcript": text, "actions": resolve_actions(actions, bill, shop)}


# ------------------------------------------------------------------ resolution

def _num(value):
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d >= 0 else None


def _f(d):
    return None if d is None else float(d)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _product(pid, shop):
    pid = _int(pid)
    if pid is None:
        return None
    return Product.objects.prefetch_related("variants").filter(shop=shop, pk=pid).first()


def _variant_of(product, vid):
    vid = _int(vid)
    if product is None or vid is None:
        return None
    return next((v for v in product.variants.all() if v.id == vid), None)


def _options(product):
    return [{"id": v.id, "name": v.name, "price": float(v.price)} for v in product.variants.all()]


def _resolve_add_items(action, shop):
    items = []
    for raw in action.get("items") or []:
        if not isinstance(raw, dict):
            continue
        product = _product(raw.get("product_id"), shop)
        variant = _variant_of(product, raw.get("variant_id"))
        price = _num(raw.get("price"))
        qty = _num(raw.get("quantity"))
        if qty is not None and qty <= 0:
            qty = None
        unit = raw.get("unit") if raw.get("unit") in VALID_UNITS else None
        spoken = str(raw.get("spoken_name") or "").strip() or "Item"

        item = {
            "spoken_name": spoken,
            "product_id": product.id if product else None,
            "product_name": product.name if product else None,
            "variant_id": None,
            "item_name": spoken,
            "quantity": _f(qty),
            "unit": unit or (product.unit if product else "piece"),
            "price": _f(price),
            "list_price": None,
            "options": [],
            "missing": [],  # what the shopkeeper still has to tell us: variant / quantity / price
        }

        if product:
            variants = list(product.variants.all())
            if variant is None and len(variants) == 1:
                variant = variants[0]
            if variant is None and price is not None:
                matches = [v for v in variants if v.price == price]
                if len(matches) == 1:  # "150 wala daliya" -> the 150 category
                    variant = matches[0]
            if variant is None:
                item["missing"].append("variant")
                item["options"] = _options(product)
                item["item_name"] = product.name
            else:
                item["variant_id"] = variant.id
                item["item_name"] = str(variant)
                item["list_price"] = float(variant.price)
        elif price is None:
            item["missing"].append("price")  # not in the catalog: ask the price of ONE unit

        if qty is None:
            item["missing"].append("quantity")
        items.append(item)
    return {"type": "add_items", "items": items} if items else None


def _resolve_variant_action(action, price_key, shop):
    product = _product(action.get("product_id"), shop)
    price = _num(action.get(price_key))
    if product is None or price is None:
        return None
    variant = _variant_of(product, action.get("variant_id"))
    variants = list(product.variants.all())
    if variant is None and len(variants) == 1:
        variant = variants[0]
    return {
        "product_id": product.id,
        "product_name": product.name,
        "variant_id": variant.id if variant else None,
        "variant_name": variant.name if variant else None,
        "old_price": float(variant.price) if variant else None,
        "options": [] if variant else _options(product),
        "new_price": float(price),
    }


def _norm(text):
    return " ".join(str(text or "").lower().split())


def _merge_correction(resolved, action):
    """"catcher ki price 100 kar do" said in the same sentence that added the catcher:
    change the item being added instead of looking for a bill line that does not exist yet."""
    name = _norm(action.get("spoken_name") or action.get("name") or action.get("item_name"))
    price, qty = _num(action.get("price")), _num(action.get("quantity"))
    if not name or (price is None and not qty):
        return False
    for prev in reversed(resolved):
        if prev.get("type") != "add_items":
            continue
        for item in reversed(prev["items"]):
            names = {_norm(item["spoken_name"]), _norm(item["item_name"]), _norm(item.get("product_name"))}
            if name in names:
                if price is not None:
                    item["price"] = _f(price)
                    if "price" in item["missing"]:
                        item["missing"].remove("price")
                if qty:
                    item["quantity"] = _f(qty)
                    if "quantity" in item["missing"]:
                        item["missing"].remove("quantity")
                return True
    return False


_PRICE_CLARIFY = {"type": "clarify", "question": "Kaun si cheez ki price? Dobara bataiye."}


def resolve_actions(actions, bill, shop):
    resolved = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        kind = action.get("type")
        out = None

        if kind == "add_items":
            out = _resolve_add_items(action, shop)

        elif kind in ("edit_item", "remove_item"):
            item = bill.items.filter(pk=_int(action.get("bill_item_id"))).first() if bill else None
            if item is None and kind == "edit_item":
                if _merge_correction(resolved, action):
                    continue
            if item:
                out = {"type": kind, "bill_item_id": item.id, "item_name": item.item_name}
                if kind == "edit_item":
                    out["quantity"] = _f(_num(action.get("quantity")))
                    out["price"] = _f(_num(action.get("price")))
                    if out["quantity"] is None and out["price"] is None:
                        out = None

        elif kind == "update_catalog_price":
            data = _resolve_variant_action(action, "new_price", shop)
            out = {"type": kind, **data} if data else _PRICE_CLARIFY

        elif kind == "price_scope":
            data = _resolve_variant_action(action, "price", shop)
            if data:
                data["price"] = data.pop("new_price")
                out = {"type": kind, **data}
            else:
                out = _PRICE_CLARIFY

        elif kind == "add_product":
            name = str(action.get("name") or "").strip()
            variants = []
            for v in action.get("variants") or []:
                if not isinstance(v, dict):
                    continue
                price = _num(v.get("price"))
                vname = str(v.get("name") or "Standard").strip() or "Standard"
                if price is not None:
                    variants.append({
                        "name": vname,
                        "price": float(price),
                        "stock": float(_num(v.get("stock")) or 0),
                    })
            if name and variants:
                out = {
                    "type": kind,
                    "name": name,
                    "unit": action.get("unit") if action.get("unit") in VALID_UNITS else "piece",
                    "track_stock": bool(action.get("track_stock")),
                    "variants": variants,
                }
            else:
                out = {"type": "clarify", "question": "Product ka naam aur price dobara bataiye."}

        elif kind in ("confirm_bill", "cancel_bill") and bill and bill.status == Bill.Status.DRAFT:
            out = {"type": kind}

        elif kind == "clarify":
            out = {"type": "clarify", "question": str(action.get("question") or "Dobara bataiye.")}

        if out:
            resolved.append(out)

    if not resolved:
        resolved.append({"type": "clarify", "question": "Samajh nahi aaya. Dobara bataiye."})
    return resolved
