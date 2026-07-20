# Manhattan Comfort CRM — Backend

FastAPI + Neon Postgres backend that syncs Companies, Vendors, Customers and
Purchase Orders from SellerCloud, and serves them to your frontend
(`manhattancomfortcrm.vercel.app`) behind JWT-protected endpoints.

## Overall flow

```
SellerCloud REST API  --sync-->  Neon Postgres  --REST-->  Next.js frontend
   (source of truth)              (your cache)              (JWT-authed)
```

1. **SellerCloud → Neon**: A sync job (or manual `/companies/sync`,
   `/purchase-orders/sync` calls) logs into SellerCloud, pages through
   results, and upserts into Neon. This is what actually calls SellerCloud —
   your frontend never talks to SellerCloud directly.
2. **Your frontend → your API**: The frontend logs a user in via
   `/auth/login`, gets a JWT, and then calls `/companies`, `/customers`,
   `/purchase-orders` with `Authorization: Bearer <token>`. These read only
   from Neon — fast, and not subject to SellerCloud's rate limits.

## 1. Set up Neon

1. Create a project at neon.tech, copy the pooled connection string.
2. Run `schema.sql` against it (Neon SQL editor, or `psql "$DATABASE_URL" -f schema.sql`).

## 2. Configure environment

```bash
cp .env.example .env
# fill in DATABASE_URL, JWT_SECRET, SELLERCLOUD_USERNAME/PASSWORD, SELLERCLOUD_BASE_URL
```

To get your real `SELLERCLOUD_BASE_URL`, hit:
`https://api.sellercloud.com/api/server-by-team/?team={your_team_name}`
and use the `RestApiEndpoint` value from the response (append nothing extra —
the client code already appends `/api/...`).

## 3. Install & run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# create your first login user
python create_user.py

# run the API
uvicorn app.main:app --reload --port 8000
```

Docs available at `http://localhost:8000/docs`.

## 4. Auth flow (JWT)

- `POST /auth/login` — form-encoded `username` (email) + `password` → returns
  `{ access_token, token_type, user }`.
- Every other endpoint requires header `Authorization: Bearer <access_token>`.
- `GET /auth/me` — returns the current logged-in user, useful for the
  frontend to hydrate session state on load.

Frontend example (fetch):
```js
const res = await fetch("https://your-api.com/auth/login", {
  method: "POST",
  headers: { "Content-Type": "application/x-www-form-urlencoded" },
  body: new URLSearchParams({ username: email, password }),
});
const { access_token } = await res.json();
localStorage.setItem("token", access_token); // or an httpOnly cookie via your own /login route

// subsequent calls
await fetch("https://your-api.com/purchase-orders?page=1", {
  headers: { Authorization: `Bearer ${access_token}` },
});
```

## 5. Pulling data from SellerCloud

Call these (as an authenticated user) whenever you want fresh data in Neon:

- `POST /companies/sync`
- `POST /purchase-orders/sync` (also pulls/creates vendors referenced by each PO, and line items)

For production, don't rely on manual calls — wire these into a scheduler:
- Simplest: a cron-triggered endpoint hit by GitHub Actions / cron-job.org
  every N minutes.
- Cleaner: run `sync_companies()` / `sync_purchase_orders()` from
  `app/services/sync_service.py` inside a scheduled worker (e.g. APScheduler,
  a Railway/Render cron job, or a Celery beat task) so it's not dependent on
  an external HTTP call.

## 6. Field mapping

Purchase order fields are now confirmed against your working Apps Script
(not guessed), pulling from your real `cd.api.sellercloud.com` tenant:

- **List** — `GET /api/PurchaseOrders/GetAllByView?viewID=25&pageNumber=&pageSize=`
  returns `ID`, `VendorID`, `PurchaseTitle`, `CreatedOn`, `DateOrdered`,
  `PurchaseOrderStatus` (int), `ReceivingStatus` (int), `Invoices[]`,
  and `Items[]` — but **not** `QtyInContainer`.
- **Detail** — `GET /api/PurchaseOrders/{id}` returns everything the list does,
  plus `Items[].QtyInContainer`. `sync_service.py` fetches detail for every PO
  it finds via the list, so you always get complete item data.
- `PurchaseOrderStatus` / `ReceivingStatus` are enum ints. I don't have the
  code→label mapping — check the status filter dropdown in the SellerCloud
  PO admin UI, or watch Network tab while filtering by status in the UI, to
  get the mapping, then fill in `status_label` in `_map_po()` in
  `sync_service.py` accordingly. Until then, the raw codes are stored in
  `purchase_order_status_code` / `receiving_status_code` so nothing is lost.
- `GetAllByView` is a **saved/filtered view** (view 25 in your script) — it
  won't return every PO in your account, only what that view's filter
  matches. If you need *all* POs regardless of status, ask your SellerCloud
  admin what filter view 25 uses, or check if there's an "all POs" view ID
  to use instead via `SELLERCLOUD_PO_VIEW_ID` / the `view_id` query param on
  `POST /purchase-orders/sync?view_id=...`.
- Companies/Customers/Vendors endpoints weren't in your script, so those
  mappings (`_map_company` in `sync_service.py`) are still based on
  SellerCloud's general public docs and should be verified the same way
  once you're ready to sync them — open Swagger UI, get a token via
  `POST /api/token`, try a real `GET`, and check the JSON keys.

Everything is also stored unmodified in the `raw_json` JSONB column on each
table, so nothing is lost even if a mapped field is initially wrong — you can
backfill from `raw_json` later without re-hitting SellerCloud.

### Security note

Your Apps Script had a live SellerCloud username/password hardcoded in one
function. Rotate that password in SellerCloud, and store credentials in
`PropertiesService.getScriptProperties()` (as your second function already
does correctly) rather than inline in the script body.

## 7. Deploying

- API: Railway, Render, or Fly.io all work well with FastAPI + Neon.
- Set the same env vars there as in `.env`.
- Set `FRONTEND_ORIGIN=https://manhattancomfortcrm.vercel.app` for CORS.
- Neon's pooled connection string handles serverless cold starts fine with
  `pool_pre_ping=True` (already set in `app/database.py`).

## Project structure

```
sellercloud_backend/
├── schema.sql                  # run this in Neon once
├── requirements.txt
├── .env.example
├── create_user.py              # create your first login user
└── app/
    ├── main.py                 # FastAPI app + CORS + routers
    ├── config.py                # env-based settings
    ├── database.py               # SQLAlchemy engine/session
    ├── models.py                  # ORM tables
    ├── schemas.py                  # Pydantic request/response models
    ├── auth.py                      # password hashing + JWT
    ├── routers/
    │   ├── auth.py                    # POST /auth/login, GET /auth/me
    │   ├── companies.py                # GET /companies, POST /companies/sync
    │   ├── customers.py                 # GET /customers
    │   └── purchase_orders.py            # GET /purchase-orders, POST /purchase-orders/sync
    └── services/
        ├── sellercloud_client.py          # raw SellerCloud API calls + token caching
        └── sync_service.py                 # maps SellerCloud JSON -> Neon rows
```
