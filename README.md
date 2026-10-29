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
7. Stock never goes below 0. `add_item`, `edit_item` and `confirm` reject more than the stock (`code: out_of_stock`) unless the request has `"ignore_stock": true`. Each line remembers what it really took (`stock_deducted`), so reopening or deleting a bill gives back exactly that. Run `python manage.py migrate` (also sets any old negative stock to 0).

## Barcodes / QR codes

- `Barcode` links a scanned text (EAN, QR or any custom code) to one category (variant). A category can have several codes; one code never belongs to two items.
- `POST /api/bills/{id}/scan/ {code}` adds the item at its catalog price, or +1 when it is already on the bill (stock check and `ignore_stock` work as for any line). Unknown code: `404 {code: "unknown_barcode"}`. Stock itself still drops only when the bill is confirmed.
- `POST /api/bills/{id}/scan/ {code, quantity?, repeat_ok?, ignore_stock?}` — adds the item. If it is already on the bill the answer is 409 `already_on_bill` (nothing changes) until the client resends with `repeat_ok: true`.
- `GET/PUT/PATCH /api/settings/` also carries `dark_mode` (true by default): the app colour theme for the whole shop, so a PC and a phone always agree.
- `GET /api/bills/?limit=&offset=` returns `{count, results}` newest first (`q` and date filters still work); without limit/offset it is the plain list.
- `GET /api/summary/` — home-page numbers: `today_sales` (confirmed today), `bills_today` (not cancelled), `out_of_stock` (tracked categories at 0).
- `GET /api/barcodes/lookup/?code=`, `POST /api/barcodes/ {variant, code}`, `DELETE /api/barcodes/{id}/`. Codes can also be sent inside `variants[].barcodes` when creating/editing a product.
- `POST /api/variants/{id}/add_stock/ {quantity}` — goods arrived: adds to stock (quantity > 0), turns stock tracking on if it was off and clears any out-of-stock alert.
- Big catalogs: `GET /api/products/?q=&limit=&offset=` searches name, category, spoken name and barcode and returns `{count, results}` (without these params you get the plain list as before). Voice only sends the AI the products that match what was said once the catalog has more than 200 items.
- `POST /api/products/import/` (multipart `file` = .csv or .xlsx, add `dry_run=1` for a preview). Columns: name, category, unit, price, stock, barcode. Good rows are saved, bad rows are reported with their row number.

## Notifications and stock monitor

- The bell icon in the app shows `Notification` records (`GET /api/notifications/` -> `{unread, items}`, `POST /api/notifications/{id}/read/`, `POST /api/notifications/read_all/`).
- **Out of stock alert**: created the moment a tracked item reaches 0 (bill confirmed, or stock edited by hand); cleared automatically when it is restocked. One alert per item, never duplicated.
- **Reminder**: while anything is still out of stock, one summary notification every `reminder_days` days (default 3, change it in Settings).
- **Background monitor**: starts automatically with `python manage.py runserver` and checks every 5 minutes (`STOCK_CHECK_INTERVAL_SECONDS`, turn off with `STOCK_MONITOR=0`). Without runserver (production), run `python manage.py check_stock --loop` as a service, or `python manage.py check_stock` from cron / Task Scheduler.

## Settings and bill templates

- `ShopSettings` (one record): bill language (`en`, `ur`, `roman`, `both` = Urdu + English) and the shop name, address and phone printed on bills.
- `BillTemplate`: paper (`58mm`, `80mm`, `a4`), layout (`receipt`, `invoice`, `minimal`), text size, header and footer line, and show/hide options (customer, date, units, discount, item numbers).
- Layouts: `receipt`, `invoice`, `minimal`, plus the designed layouts `modern`, `boutique`, `royal`, `sunset`, `noir` (A4) and `ticket` (80 mm). Ready-made templates of them are added by migrations 0005 and 0006; they are editable and deletable.
- A built-in **Default receipt** template always exists. It cannot be edited or deleted. Users add their own templates.
- Exactly one template is selected. If the selected one is deleted, the default becomes selected again.

## Voice pipeline

`audio -> /api/voice/transcribe/ -> text -> /api/voice/interpret/ -> proposed actions -> shopkeeper confirms -> normal API calls`

The AI only **proposes**. `interpret` never writes to the database. Every id the AI returns is checked against the real
catalog and bill, and anything unclear is listed in each item's `missing` field: `variant` (which category?), `price` (price of ONE piece/kg) or
`quantity` (never assumed, so "sugar ka bill" asks how much). A later correction in the same sentence wins, and the
app sends the current proposal back as `pending` so a spoken answer or correction updates it. Action types: `add_items`, `edit_item`, `remove_item`, `update_catalog_price`,
`price_scope` (bill only or permanent?), `add_product`, `confirm_bill`, `cancel_bill`, `clarify`.

## Endpoints

| Method | Path | What it does |
|---|---|---|
| GET | `/api/health/` | Health check |
| GET/PUT/PATCH | `/api/settings/` | Bill language and shop details |
| GET/POST | `/api/templates/` | List / add bill templates |
| PATCH/DELETE | `/api/templates/{id}/` | Edit / delete a custom template |
| POST | `/api/templates/{id}/select/` | Use this template for printing |
| POST | `/api/voice/transcribe/` | Audio (`audio` form field) to text |
| POST | `/api/voice/interpret/` | `{ text, bill_id, pending? }` to proposed actions (saves nothing) |
| GET/POST | `/api/products/` | List (or search with `?q=`) / create product with variants |
| PATCH/DELETE | `/api/products/{id}/` | Edit product and categories (send variant `id`s to keep them) / delete |
| POST | `/api/products/{id}/add_variant/` | Add a category |
| POST | `/api/products/{id}/add_alias/` | Add a spoken name |
| GET | `/api/variants/` | List variants |
| POST | `/api/variants/{id}/update_price/` | Permanent price change `{ "price": 160 }` |
| GET | `/api/variants/{id}/price_history/` | Price change log |
| GET/POST | `/api/customers/` | Customers |
| GET/POST | `/api/bills/` | List (filters: `?q=name&date=YYYY-MM-DD`, also `date_from`, `date_to`) / create draft bill `{ "customer_name": "Ali" }` |
| POST | `/api/bills/{id}/add_item/` | Add a line (see below) |
| PATCH/DELETE | `/api/bills/{id}/items/{item_id}/` | Edit / remove a line |
| POST | `/api/bills/{id}/confirm/` | Finalize bill, reduce stock |
| POST | `/api/bills/{id}/reopen/` | Edit a confirmed bill again (stock is given back until re-confirmed) |
| DELETE | `/api/bills/{id}/` | Delete a bill (stock restored if it was confirmed) |
| POST | `/api/bills/{id}/cancel/` | Cancel draft |

`add_item` examples:

```json
{ "variant": 1, "quantity": 0.5 }                        // catalog price
{ "variant": 1, "quantity": 1, "sold_price": 130 }       // bargained, catalog stays
{ "item_name": "Finger ring", "sold_price": 150 }        // open-price item
```
