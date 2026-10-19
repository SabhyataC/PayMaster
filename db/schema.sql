
CREATE TABLE IF NOT EXISTS vendors (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT NOT NULL,
    country      TEXT NOT NULL,
    is_synthetic BOOLEAN NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendor_bank_history (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id         UUID NOT NULL REFERENCES vendors(id),
    destination_email TEXT NOT NULL,       -- PayPal email the payout goes to
    valid_from        TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to          TIMESTAMPTZ,         -- NULL = this is the current destination
    change_source     TEXT NOT NULL DEFAULT 'onboarding'  -- e.g. onboarding, invoice_email
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_number  TEXT NOT NULL UNIQUE,          -- e.g. PO-1001
    vendor_id  UUID NOT NULL REFERENCES vendors(id),
    amount     NUMERIC(12,2) NOT NULL,        -- agreed total
    currency   TEXT NOT NULL DEFAULT 'USD',
    status     TEXT NOT NULL DEFAULT 'open',  -- open, closed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deliveries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    po_id       UUID NOT NULL REFERENCES purchase_orders(id),
    description TEXT NOT NULL,                -- what arrived
    amount      NUMERIC(12,2) NOT NULL,       -- value of what arrived
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vbh_vendor ON vendor_bank_history(vendor_id);
CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders(vendor_id);
CREATE INDEX IF NOT EXISTS idx_deliv_po ON deliveries(po_id);

CREATE TABLE IF NOT EXISTS invoices (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor_id      UUID NOT NULL REFERENCES vendors(id),
    po_id          UUID REFERENCES purchase_orders(id),   -- NULL = invoice has no PO
    invoice_number TEXT NOT NULL,                         -- the vendor's own number
    amount         NUMERIC(12,2) NOT NULL,
    currency       TEXT NOT NULL DEFAULT 'USD',
    destination_email TEXT,                               -- payout email stated on the invoice
    status         TEXT NOT NULL DEFAULT 'received',
        -- received, verified, flagged, approved, paid, rejected
    received_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vendor_id, invoice_number)
);

CREATE INDEX IF NOT EXISTS idx_inv_vendor ON invoices(vendor_id);
CREATE INDEX IF NOT EXISTS idx_inv_po ON invoices(po_id);
CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices(status);

CREATE TABLE IF NOT EXISTS payments (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_id        UUID NOT NULL REFERENCES invoices(id),
    idempotency_key   TEXT NOT NULL UNIQUE,   -- becomes PayPal sender_batch_id
    destination_email TEXT NOT NULL,          -- where we actually sent it
    amount            NUMERIC(12,2) NOT NULL,
    currency          TEXT NOT NULL DEFAULT 'USD',
    status            TEXT NOT NULL DEFAULT 'created',
        -- created, submitted, pending, success, unclaimed, failed, unknown
    paypal_batch_id   TEXT,                   -- PayPal's own ID, filled after the call
    paypal_item_id    TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settlements (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id        UUID NOT NULL UNIQUE REFERENCES payments(id),
    amount_sent       NUMERIC(12,2) NOT NULL,   -- debited from our account
    currency_sent     TEXT NOT NULL,
    amount_received   NUMERIC(12,2),            -- what the vendor got
    currency_received TEXT,
    fx_rate           NUMERIC(12,6),            -- NULL if no conversion
    fee_amount        NUMERIC(12,2),
    fee_currency      TEXT,
    settled_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pay_invoice ON payments(invoice_id);
CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id    UUID REFERENCES payments(id),
    entry_type    TEXT NOT NULL,        -- payment, fee, fx_difference, refund
    account       TEXT NOT NULL,        -- accounts_payable, paypal_balance, fees_expense
    direction     TEXT NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount        NUMERIC(12,2) NOT NULL CHECK (amount >= 0),
    currency      TEXT NOT NULL DEFAULT 'USD',
    description   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          BIGSERIAL PRIMARY KEY,
    stage       TEXT NOT NULL,          -- verify, pay, reconcile, advise
    entity_type TEXT NOT NULL,          -- invoice, payment, vendor ...
    entity_id   UUID NOT NULL,
    decision    TEXT NOT NULL,          -- e.g. approved, flagged, held, paid
    reason      TEXT NOT NULL,          -- plain-English why
    inputs      JSONB NOT NULL DEFAULT '{}'::jsonb,   -- the data used to decide
    actor       TEXT NOT NULL DEFAULT 'system',       -- system, ai, human
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledger_payment ON ledger_entries(payment_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);