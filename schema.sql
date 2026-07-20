-- =========================================================
-- Neon Postgres schema for Manhattan Comfort CRM
-- Run this once against your Neon database.
-- =========================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto"; -- for gen_random_uuid()

-- ---------------------------------------------------------
-- 1. Users (your app's own login, NOT SellerCloud accounts)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name       VARCHAR(255),
    role            VARCHAR(50) NOT NULL DEFAULT 'user', -- user | admin
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- 2. Companies (SellerCloud "Companies" - the entity that
--    owns catalog/orders, e.g. multi-brand setups)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sellercloud_company_id INTEGER UNIQUE,          -- ID field from SC API
    name                  VARCHAR(255) NOT NULL,
    email                 VARCHAR(255),
    phone                 VARCHAR(50),
    address_line1         VARCHAR(255),
    address_line2         VARCHAR(255),
    city                  VARCHAR(120),
    state                 VARCHAR(120),
    postal_code           VARCHAR(20),
    country               VARCHAR(120),
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    raw_json              JSONB,                    -- full SellerCloud payload, for anything not modeled
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- 3. Vendors (SellerCloud Purchase Orders are placed
--    against Vendors, not Customers)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS vendors (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sellercloud_vendor_id INTEGER UNIQUE,
    name                 VARCHAR(255) NOT NULL,
    email                VARCHAR(255),
    phone                VARCHAR(50),
    address_line1        VARCHAR(255),
    city                 VARCHAR(120),
    state                VARCHAR(120),
    postal_code          VARCHAR(20),
    country              VARCHAR(120),
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    raw_json             JSONB,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- 4. Customers (SellerCloud "Customers" tied to Orders,
--    linked to a Company)
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS customers (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sellercloud_customer_id INTEGER UNIQUE,
    company_id             UUID REFERENCES companies(id) ON DELETE SET NULL,
    first_name             VARCHAR(120),
    last_name              VARCHAR(120),
    email                  VARCHAR(255),
    phone                  VARCHAR(50),
    billing_address_line1  VARCHAR(255),
    billing_city           VARCHAR(120),
    billing_state          VARCHAR(120),
    billing_postal_code    VARCHAR(20),
    billing_country        VARCHAR(120),
    shipping_address_line1 VARCHAR(255),
    shipping_city          VARCHAR(120),
    shipping_state         VARCHAR(120),
    shipping_postal_code   VARCHAR(20),
    shipping_country       VARCHAR(120),
    raw_json                JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- 5. Purchase Orders
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchase_orders (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sellercloud_po_id      INTEGER UNIQUE,
    purchase_title         VARCHAR(255),          -- SellerCloud "PurchaseTitle" (there's no separate PO number field)
    company_id             UUID REFERENCES companies(id) ON DELETE SET NULL,
    vendor_id               UUID REFERENCES vendors(id) ON DELETE SET NULL,
    purchase_order_status_code INTEGER,           -- raw SellerCloud "PurchaseOrderStatus" enum int
    receiving_status_code       INTEGER,          -- raw SellerCloud "ReceivingStatus" enum int
    status_label                 VARCHAR(50),     -- human-readable, filled in once you map the enum values (see README)
    created_on                    TIMESTAMPTZ,     -- SellerCloud "CreatedOn"
    date_ordered                  TIMESTAMPTZ,     -- SellerCloud "DateOrdered"
    expected_delivery_date         TIMESTAMPTZ,
    invoice_date                    TIMESTAMPTZ,   -- from Invoices[0].InvoiceDate
    total_amount                    NUMERIC(14,2),
    currency                        VARCHAR(10) DEFAULT 'USD',
    notes                            TEXT,
    raw_json                        JSONB,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders(vendor_id);
CREATE INDEX IF NOT EXISTS idx_po_company ON purchase_orders(company_id);
CREATE INDEX IF NOT EXISTS idx_po_status ON purchase_orders(status);

-- ---------------------------------------------------------
-- 6. Purchase Order Line Items
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS purchase_order_items (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_order_id      UUID NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
    sellercloud_item_id     INTEGER,               -- SellerCloud Items[].ID (a.k.a. "POItemID")
    sku                    VARCHAR(120),
    product_name            VARCHAR(255),
    qty_ordered               INTEGER DEFAULT 0,   -- SellerCloud "QtyOrdered"
    qty_received              INTEGER DEFAULT 0,   -- SellerCloud "QtyReceived"
    qty_in_container          INTEGER DEFAULT 0,   -- SellerCloud "QtyInContainer" - only present on the full PO detail call, not the list view
    unit_price               NUMERIC(14,2) DEFAULT 0,
    qty_cases_ordered         INTEGER DEFAULT 0,
    qty_units_per_case        INTEGER DEFAULT 0,
    case_price                NUMERIC(14,2) DEFAULT 0,
    is_bundle_component        BOOLEAN DEFAULT FALSE, -- true if this came from item.Items[] (a kit/bundle line)
    parent_sellercloud_item_id  INTEGER,             -- if bundle component, the ID of the parent kit item
    expected_delivery_date     TIMESTAMPTZ,
    raw_json                   JSONB,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_poitems_po ON purchase_order_items(purchase_order_id);

-- ---------------------------------------------------------
-- 6b. Per-vendor container lead time (days from payment/order
--     to first container arrival) - used to flag overdue POs
-- ---------------------------------------------------------
ALTER TABLE vendors ADD COLUMN IF NOT EXISTS container_lead_time_days INTEGER;

-- ---------------------------------------------------------
-- 6c. Shipping containers (SellerCloud ShippingContainers)
--     Two-step lookup per your Apps Script's pattern:
--     1) GET /ShippingContainers?model.poIds=X&model.productIds=Y -> container IDs
--     2) GET /ShippingContainers/{id} -> container details + quantities
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS shipping_containers (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sellercloud_container_id   INTEGER UNIQUE,
    container_name             VARCHAR(255),
    raw_json                   JSONB,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchase_order_item_containers (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purchase_order_item_id     UUID NOT NULL REFERENCES purchase_order_items(id) ON DELETE CASCADE,
    shipping_container_id       UUID NOT NULL REFERENCES shipping_containers(id) ON DELETE CASCADE,
    qty_in_container             INTEGER DEFAULT 0,
    raw_json                     JSONB,
    created_at                   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------
-- 7. Sync logs - track every SellerCloud pull for debugging
-- ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_logs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type   VARCHAR(50) NOT NULL,   -- companies | vendors | customers | purchase_orders
    status        VARCHAR(20) NOT NULL,   -- success | failed
    records_synced INTEGER DEFAULT 0,
    message        TEXT,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at    TIMESTAMPTZ
);
