-- PostgreSQL migration to fix the schema mismatch behind the 500 errors.
-- Run this against your live database before redeploying the API.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------
-- users table fixes
-- ---------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(120);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR(120);
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS role VARCHAR(50) NOT NULL DEFAULT 'user';
ALTER TABLE users ADD COLUMN IF NOT EXISTS vendor_id UUID;
ALTER TABLE users ADD COLUMN IF NOT EXISTS warehouse_id UUID;
ALTER TABLE users ADD COLUMN IF NOT EXISTS country VARCHAR(120);
ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(50);
ALTER TABLE users ADD COLUMN IF NOT EXISTS payment_terms VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS container_lead_time_days INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_new_user BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_trucker_email BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_invoice_delayed BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_shipment_delayed BOOLEAN DEFAULT FALSE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_code VARCHAR(10);
ALTER TABLE users ADD COLUMN IF NOT EXISTS otp_expires_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ;

-- Add foreign key constraints only if they do not already exist.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_type = 'FOREIGN KEY'
          AND table_name = 'users'
          AND constraint_name = 'users_vendor_id_fkey'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_vendor_id_fkey
            FOREIGN KEY (vendor_id) REFERENCES vendors(id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_type = 'FOREIGN KEY'
          AND table_name = 'users'
          AND constraint_name = 'users_warehouse_id_fkey'
    ) THEN
        ALTER TABLE users
            ADD CONSTRAINT users_warehouse_id_fkey
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id) ON DELETE SET NULL;
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- token blacklist table for auth logout/revocation
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS token_blacklist (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token          VARCHAR(500) UNIQUE NOT NULL,
    blacklisted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_token_blacklist_token ON token_blacklist(token);

-- ---------------------------------------------------------------------
-- activity logs table
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_activity_logs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID REFERENCES users(id) ON DELETE SET NULL,
    action        VARCHAR(50) NOT NULL,
    entity_type   VARCHAR(50),
    entity_id     VARCHAR(255),
    details       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE user_activity_logs ADD COLUMN IF NOT EXISTS category VARCHAR(100);

CREATE INDEX IF NOT EXISTS idx_activity_logs_user ON user_activity_logs(user_id);

-- ---------------------------------------------------------------------
-- channels table + purchase_orders channel linkage
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channels (
    id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS channel_order_id VARCHAR(255) NULL;
ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS channel_id UUID NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE constraint_type = 'FOREIGN KEY'
          AND table_name = 'purchase_orders'
          AND constraint_name = 'fk_purchase_orders_channel_id'
    ) THEN
        ALTER TABLE purchase_orders
            ADD CONSTRAINT fk_purchase_orders_channel_id
            FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE SET NULL;
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- sync logs table
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_logs (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type   VARCHAR(50) NOT NULL,
    status        VARCHAR(20) NOT NULL,
    records_synced INTEGER DEFAULT 0,
    message       TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- ---------------------------------------------------------------------
-- users table: per-user table column visibility preferences
-- ---------------------------------------------------------------------
ALTER TABLE users ADD COLUMN IF NOT EXISTS po_columns JSON DEFAULT '{
    "id": true, "orderId": true, "channel_order_id": true, "status": true,
    "delay_reason": true, "commentsCount": true, "creationDate": true,
    "vendorName": true, "customerName": true, "items": true, "orderedQty": true,
    "invoiceDetails": true, "invoiceDelayStatus": true, "expected_delivery_date": true,
    "containerIds": true, "actions": true
}'::json;
ALTER TABLE users ADD COLUMN IF NOT EXISTS container_columns JSON DEFAULT '{
    "id": true, "name": true, "warehouse_name": true, "total_items": true,
    "total_qty_in_container": true, "total_qty_received": true, "arrivalDate": true,
    "received_date": true, "actions": true
}'::json;

-- ---------------------------------------------------------------------
-- shipping_container_tracking table (AllWays container tracking)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shipping_container_tracking (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    shipping_container_id UUID NOT NULL UNIQUE REFERENCES shipping_containers(id) ON DELETE CASCADE,
    container_number      VARCHAR(255) NOT NULL,

    origin_port           VARCHAR(255),
    destination_port      VARCHAR(255),
    carrier               VARCHAR(255),
    vessel_and_voyage     VARCHAR(255),
    etd                   TIMESTAMPTZ,
    eta                   TIMESTAMPTZ,
    status                VARCHAR(100),

    latitude              NUMERIC(10, 6),
    longitude             NUMERIC(10, 6),
    location_status       VARCHAR(100),

    raw_response          JSONB,
    error_message         TEXT,
    last_tracked_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_shipping_container_tracking_container_id
    ON shipping_container_tracking (shipping_container_id);
CREATE INDEX IF NOT EXISTS idx_shipping_container_tracking_container_number
    ON shipping_container_tracking (container_number);

-- ---------------------------------------------------------------------
-- optional safe verification queries
-- ---------------------------------------------------------------------
SELECT column_name
FROM information_schema.columns
WHERE table_name = 'users'
ORDER BY ordinal_position;

-- ---------------------------------------------------------------------
-- shipping_containers: rename container_cost_drayage -> container_shipping_cost,
-- add drayage_cost (commit 66e3963)
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shipping_containers' AND column_name = 'container_cost_drayage'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'shipping_containers' AND column_name = 'container_shipping_cost'
    ) THEN
        ALTER TABLE shipping_containers RENAME COLUMN container_cost_drayage TO container_shipping_cost;
    END IF;
END $$;

ALTER TABLE shipping_containers ADD COLUMN IF NOT EXISTS container_shipping_cost NUMERIC(14,2);
ALTER TABLE shipping_containers ADD COLUMN IF NOT EXISTS drayage_cost NUMERIC(14,2);

-- ---------------------------------------------------------------------
-- purchase_order_items: add image_url (commit 3ba58ed)
-- ---------------------------------------------------------------------
ALTER TABLE purchase_order_items ADD COLUMN IF NOT EXISTS image_url VARCHAR(500);
