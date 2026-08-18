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

CREATE INDEX IF NOT EXISTS idx_activity_logs_user ON user_activity_logs(user_id);

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
-- optional safe verification queries
-- ---------------------------------------------------------------------
SELECT column_name
FROM information_schema.columns
WHERE table_name = 'users'
ORDER BY ordinal_position;
