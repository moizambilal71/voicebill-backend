# VoiceBill Backend (Django)

REST API for the voice-controlled billing app for local shops.

## Run locally (first time)

```bash
python -m venv .venv
# Windows:      .venv\Scripts\activate
# Mac / Linux:  source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env        # Mac / Linux: cp .env.example .env
python manage.py migrate
python manage.py seed_demo    # optional demo products
python manage.py createsuperuser   # optional, for /admin
python manage.py runserver
```

API: http://127.0.0.1:8000/api/  ·  Admin: http://127.0.0.1:8000/admin/

## Voice setup (OpenAI)

1. Put your key in `.env` (this file is git-ignored, never commit it):
   `OPENAI_API_KEY=sk-...`
2. Optional: `OPENAI_TRANSCRIBE_MODEL` (default `gpt-4o-transcribe`), `OPENAI_CHAT_MODEL` (default `gpt-4.1-mini`),
   `VOICE_LANGUAGE` (default `ur`; leave empty for auto-detect).
3. Restart the server.

Run the tests (no OpenAI needed): `python manage.py test`

## After every `git pull`

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

## Data model

| Model | Purpose |
|---|---|
| `Product` | Catalog item (Sugar, Daliya, Washing wear). Has a unit: kg, g, l, piece, meter, dozen, pack |
| `Variant` | Category / grade with its own price and stock (Daliya Regular 100, Premium 200). Every product has at least one |
| `Alias` | Spoken names (cheeni → Sugar, "sasta wala" → Daliya Regular) |
| `PriceHistory` | Log of permanent price changes only |
| `Customer`, `Bill`, `BillItem` | Bills and their lines |

## Pricing rules (locked)

1. **No price spoken** → catalog price of the variant is used (`price_source = catalog`).
2. **Price spoken** → that price goes on the bill; catalog is not touched (`price_source = spoken`). Covers bargaining and open-price items.
3. **Item not in catalog, no price** → API rejects with "Kitne ka hai?" so the app asks.
4. **Permanent price change** → only via `POST /api/variants/{id}/update_price/`, after the shopkeeper confirms. Logged in `PriceHistory`.
5. Each bill line stores `list_price` and `sold_price`, so discounts are reportable.
6. Stock is reduced only when a bill is confirmed, and only for products with `track_stock = true`.

## Voice pipeline

`audio -> /api/voice/transcribe/ -> text -> /api/voice/interpret/ -> proposed actions -> shopkeeper confirms -> normal API calls`

The AI only **proposes**. `interpret` never writes to the database. Every id the AI returns is checked against the real
catalog and bill, and anything unclear is marked for the shopkeeper: `needs_variant` (which category?) or
`needs_price` (Kitne ka hai?). Action types: `add_items`, `edit_item`, `remove_item`, `update_catalog_price`,
`price_scope` (bill only or permanent?), `add_product`, `confirm_bill`, `cancel_bill`, `clarify`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health/` | Health check |
| POST | `/api/voice/transcribe/` | Audio (`audio` form field) to text |
| POST | `/api/voice/interpret/` | `{ text, bill_id }` to proposed actions (saves nothing) |
| GET/POST | `/api/products/` | List / create product with variants |
| POST | `/api/products/{id}/add_variant/` | Add a category |
| POST | `/api/products/{id}/add_alias/` | Add a spoken name |
| GET | `/api/variants/` | List variants |
| POST | `/api/variants/{id}/update_price/` | Permanent price change `{ "price": 160 }` |
| GET | `/api/variants/{id}/price_history/` | Price change log |
| GET/POST | `/api/customers/` | Customers |
| GET/POST | `/api/bills/` | List / create draft bill `{ "customer_name": "Ali" }` |
| POST | `/api/bills/{id}/add_item/` | Add a line (see below) |
| PATCH/DELETE | `/api/bills/{id}/items/{item_id}/` | Edit / remove a line |
| POST | `/api/bills/{id}/confirm/` | Finalize bill, reduce stock |
| POST | `/api/bills/{id}/cancel/` | Cancel draft |

`add_item` examples:

```json
{ "variant": 1, "quantity": 0.5 }                        // catalog price
{ "variant": 1, "quantity": 1, "sold_price": 130 }       // bargained, catalog stays
{ "item_name": "Finger ring", "sold_price": 150 }        // open-price item
```
