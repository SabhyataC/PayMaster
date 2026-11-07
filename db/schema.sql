CREATE TABLE IF NOT EXISTS vendors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    name TEXT NOT NULL,
    country TEXT NOT NULL,
    is_synthetic BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vendor_bank_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    vendor_id UUID NOT NULL REFERENCES vendors (id),
    destination_email TEXT NOT NULL, -- PayPal email the payout goes to
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ, -- NULL = this is the current destination
    change_source TEXT NOT NULL DEFAULT 'onboarding' -- e.g. onboarding, invoice_email
);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    po_number TEXT NOT NULL UNIQUE, -- e.g. PO-1001
    vendor_id UUID NOT NULL REFERENCES vendors (id),
    amount NUMERIC(12, 2) NOT NULL, -- agreed total
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'open', -- open, closed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    po_id UUID NOT NULL REFERENCES purchase_orders (id),
    description TEXT NOT NULL, -- what arrived
    amount NUMERIC(12, 2) NOT NULL, -- value of what arrived
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vbh_vendor ON vendor_bank_history (vendor_id);

CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders (vendor_id);

CREATE INDEX IF NOT EXISTS idx_deliv_po ON deliveries (po_id);

CREATE TABLE IF NOT EXISTS invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    vendor_id UUID NOT NULL REFERENCES vendors (id),
    po_id UUID REFERENCES purchase_orders (id), -- NULL = invoice has no PO
    invoice_number TEXT NOT NULL, -- the vendor's own number
    amount NUMERIC(12, 2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    destination_email TEXT, -- payout email stated on the invoice
    status TEXT NOT NULL DEFAULT 'received',
    -- received, verified, flagged, approved, paid, rejected
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (vendor_id, invoice_number)
);

CREATE INDEX IF NOT EXISTS idx_inv_vendor ON invoices (vendor_id);

CREATE INDEX IF NOT EXISTS idx_inv_po ON invoices (po_id);

CREATE INDEX IF NOT EXISTS idx_inv_status ON invoices (status);

CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    invoice_id UUID NOT NULL REFERENCES invoices (id),
    idempotency_key TEXT NOT NULL UNIQUE, -- becomes PayPal sender_batch_id
    destination_email TEXT NOT NULL, -- where we actually sent it
    amount NUMERIC(12, 2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    status TEXT NOT NULL DEFAULT 'created',
    -- created, submitted, pending, success, unclaimed, failed, unknown
    paypal_batch_id TEXT, -- PayPal's own ID, filled after the call
    paypal_item_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS settlements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    payment_id UUID NOT NULL UNIQUE REFERENCES payments (id),
    amount_sent NUMERIC(12, 2) NOT NULL, -- debited from our account
    currency_sent TEXT NOT NULL,
    amount_received NUMERIC(12, 2), -- what the vendor got
    currency_received TEXT,
    fx_rate NUMERIC(12, 6), -- NULL if no conversion
    fee_amount NUMERIC(12, 2),
    fee_currency TEXT,
    settled_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pay_invoice ON payments (invoice_id);

CREATE INDEX IF NOT EXISTS idx_pay_status ON payments (status);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    payment_id UUID REFERENCES payments (id),
    entry_type TEXT NOT NULL, -- payment, fee, fx_difference, refund
    account TEXT NOT NULL, -- accounts_payable, paypal_balance, fees_expense
    direction TEXT NOT NULL CHECK (
        direction IN ('debit', 'credit')
    ),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    currency TEXT NOT NULL DEFAULT 'USD',
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    stage TEXT NOT NULL, -- verify, pay, reconcile, advise
    entity_type TEXT NOT NULL, -- invoice, payment, vendor ...
    entity_id UUID NOT NULL,
    decision TEXT NOT NULL, -- e.g. approved, flagged, held, paid
    reason TEXT NOT NULL, -- plain-English why
    inputs JSONB NOT NULL DEFAULT '{}'::jsonb, -- the data used to decide
    actor TEXT NOT NULL DEFAULT 'system', -- system, ai, human
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ledger_payment ON ledger_entries (payment_id);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log (entity_type, entity_id);

-- Invoice intake: one row per document the AI reads. Keeps the original so a human can compare.
CREATE TABLE IF NOT EXISTS extractions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid (),
    invoice_id uuid REFERENCES invoices (id) ON DELETE SET NULL,
    source_name text NOT NULL,
    source_format text NOT NULL CHECK (
        source_format IN (
            'text_pdf',
            'scan',
            'photo',
            'email'
        )
    ),
    source_text text,
    source_bytes bytea,
    source_sha256 text NOT NULL,
    extracted jsonb,
    confidence numeric(4, 3),
    field_confidence jsonb,
    model_name text,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending',
            'extracted',
            'needs_review',
            'accepted',
            'failed'
        )
    ),
    reason text,
    is_synthetic boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_extractions_status ON extractions (status);

CREATE INDEX IF NOT EXISTS idx_extractions_invoice ON extractions (invoice_id);

CREATE TABLE IF NOT EXISTS exceptions (
    id SERIAL PRIMARY KEY,
    payment_id UUID REFERENCES payments (id),
    type TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open' CHECK (
        status IN ('open', 'resolved')
    ),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    note TEXT
);

-- One open exception per payment per type. This makes run 2 safe:
-- the same problem can't be logged twice while it's still open.
CREATE UNIQUE INDEX IF NOT EXISTS exceptions_one_open
    ON exceptions (payment_id, type) WHERE status = 'open';

ALTER TABLE payments
ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;

-- One payment per invoice per attempt number. This is the database-level
-- never-pay-twice guard, even if two requests arrive at the same moment.
CREATE UNIQUE INDEX IF NOT EXISTS payments_invoice_attempt ON payments (invoice_id, attempt);

ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS explanation JSONB;

-- Human review decisions. Case memory (Nov 4) learns from this table.
CREATE TABLE IF NOT EXISTS reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    invoice_id UUID NOT NULL REFERENCES invoices (id),
    original_ai_decision TEXT, -- approve / hold / reject, from the Verify audit row
    human_decision TEXT NOT NULL CHECK (
        human_decision IN ('approve', 'reject')
    ),
    reviewer TEXT NOT NULL,
    note TEXT NOT NULL,
    confirmed_with_vendor BOOLEAN NOT NULL DEFAULT false,
    status_before TEXT,
    status_after TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reviews_invoice ON reviews (invoice_id);

-- Case memory: one row per past human review, with the signals that fired and the outcome.

-- Needs-review corrections: the first AI-extracted values are kept here, never overwritten.
ALTER TABLE extractions
ADD COLUMN IF NOT EXISTS original_extracted jsonb;

-- Week 2: companies and users
CREATE TABLE IF NOT EXISTS companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    company_id UUID REFERENCES companies (id),
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'reviewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users (lower(email));

-- company_id on the top-level tables (nullable for now; NOT NULL after seed.py is updated)
ALTER TABLE vendors
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE purchase_orders
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE invoices
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE payments
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE ledger_entries
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE audit_log
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE exceptions
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE reviews
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE extractions
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

-- PO numbers must be unique per company, not globally
ALTER TABLE purchase_orders
DROP CONSTRAINT IF EXISTS purchase_orders_po_number_key;

CREATE UNIQUE INDEX IF NOT EXISTS po_unique_per_company ON purchase_orders (company_id, po_number);

CREATE INDEX IF NOT EXISTS idx_vendors_company ON vendors (company_id);

CREATE INDEX IF NOT EXISTS idx_invoices_company ON invoices (company_id);

CREATE INDEX IF NOT EXISTS idx_payments_company ON payments (company_id);

CREATE INDEX IF NOT EXISTS idx_audit_company ON audit_log (company_id);

ALTER TABLE deliveries
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE users ALTER COLUMN company_id DROP NOT NULL;

-- =====================================================================================
-- Security hardening (re-runnable). Tenant isolation, roles, auth, webhooks.
-- =====================================================================================

ALTER TABLE companies
ADD COLUMN IF NOT EXISTS auto_pay_limit NUMERIC(12, 2);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'companies_auto_pay_limit_positive') THEN
        ALTER TABLE companies ADD CONSTRAINT companies_auto_pay_limit_positive
            CHECK (auto_pay_limit IS NULL OR auto_pay_limit > 0);
    END IF;
END $$;

ALTER TABLE vendor_bank_history
ADD COLUMN IF NOT EXISTS company_id UUID REFERENCES companies (id);

ALTER TABLE users
ADD COLUMN IF NOT EXISTS token_version INTEGER NOT NULL DEFAULT 0;

ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled_at TIMESTAMPTZ;

ALTER TABLE users
ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;

ALTER TABLE users
ADD CONSTRAINT users_role_check CHECK (
    role IN ('admin', 'reviewer', 'viewer')
);

ALTER TABLE invoices
ADD COLUMN IF NOT EXISTS uploaded_by UUID REFERENCES users (id);

ALTER TABLE extractions
ADD COLUMN IF NOT EXISTS uploaded_by UUID REFERENCES users (id);

-- Fill company_id on old rows from their parents; whatever has no owner at all goes to one
-- visible "Legacy (unassigned)" company, so nothing is left invisible and NOT NULL can hold.
DO $$
DECLARE
    legacy UUID;
    orphans BIGINT;
BEGIN
    UPDATE purchase_orders x SET company_id = v.company_id FROM vendors v
        WHERE x.vendor_id = v.id AND x.company_id IS NULL AND v.company_id IS NOT NULL;
    UPDATE invoices x SET company_id = v.company_id FROM vendors v
        WHERE x.vendor_id = v.id AND x.company_id IS NULL AND v.company_id IS NOT NULL;
    UPDATE vendor_bank_history x SET company_id = v.company_id FROM vendors v
        WHERE x.vendor_id = v.id AND x.company_id IS NULL AND v.company_id IS NOT NULL;
    UPDATE deliveries x SET company_id = p.company_id FROM purchase_orders p
        WHERE x.po_id = p.id AND x.company_id IS NULL AND p.company_id IS NOT NULL;
    UPDATE payments x SET company_id = i.company_id FROM invoices i
        WHERE x.invoice_id = i.id AND x.company_id IS NULL AND i.company_id IS NOT NULL;
    UPDATE ledger_entries x SET company_id = p.company_id FROM payments p
        WHERE x.payment_id = p.id AND x.company_id IS NULL AND p.company_id IS NOT NULL;
    UPDATE exceptions x SET company_id = p.company_id FROM payments p
        WHERE x.payment_id = p.id AND x.company_id IS NULL AND p.company_id IS NOT NULL;
    UPDATE reviews x SET company_id = i.company_id FROM invoices i
        WHERE x.invoice_id = i.id AND x.company_id IS NULL AND i.company_id IS NOT NULL;
    UPDATE extractions x SET company_id = i.company_id FROM invoices i
        WHERE x.invoice_id = i.id AND x.company_id IS NULL AND i.company_id IS NOT NULL;
    UPDATE audit_log x SET company_id = i.company_id FROM invoices i
        WHERE x.entity_type = 'invoice' AND x.entity_id = i.id AND x.company_id IS NULL;
    UPDATE audit_log x SET company_id = p.company_id FROM payments p
        WHERE x.entity_type = 'payment' AND x.entity_id = p.id AND x.company_id IS NULL;
    UPDATE audit_log x SET company_id = e.company_id FROM extractions e
        WHERE x.entity_type = 'extraction' AND x.entity_id = e.id AND x.company_id IS NULL;
    UPDATE audit_log x SET company_id = v.company_id FROM vendors v
        WHERE x.entity_type = 'vendor' AND x.entity_id = v.id AND x.company_id IS NULL;

    SELECT (SELECT count(*) FROM vendors WHERE company_id IS NULL)
         + (SELECT count(*) FROM purchase_orders WHERE company_id IS NULL)
         + (SELECT count(*) FROM invoices WHERE company_id IS NULL)
         + (SELECT count(*) FROM extractions WHERE company_id IS NULL)
         + (SELECT count(*) FROM exceptions WHERE company_id IS NULL)
         + (SELECT count(*) FROM ledger_entries WHERE company_id IS NULL)
         + (SELECT count(*) FROM audit_log WHERE company_id IS NULL)
         + (SELECT count(*) FROM payments WHERE company_id IS NULL)
         + (SELECT count(*) FROM reviews WHERE company_id IS NULL)
         + (SELECT count(*) FROM deliveries WHERE company_id IS NULL)
         + (SELECT count(*) FROM vendor_bank_history WHERE company_id IS NULL)
    INTO orphans;

    IF orphans > 0 THEN
        SELECT id INTO legacy FROM companies WHERE name = 'Legacy (unassigned)' LIMIT 1;
        IF legacy IS NULL THEN
            INSERT INTO companies (name) VALUES ('Legacy (unassigned)') RETURNING id INTO legacy;
        END IF;
        UPDATE vendors SET company_id = legacy WHERE company_id IS NULL;
        UPDATE purchase_orders x SET company_id = COALESCE(
            (SELECT company_id FROM vendors v WHERE v.id = x.vendor_id), legacy) WHERE company_id IS NULL;
        UPDATE invoices x SET company_id = COALESCE(
            (SELECT company_id FROM vendors v WHERE v.id = x.vendor_id), legacy) WHERE company_id IS NULL;
        UPDATE vendor_bank_history x SET company_id = COALESCE(
            (SELECT company_id FROM vendors v WHERE v.id = x.vendor_id), legacy) WHERE company_id IS NULL;
        UPDATE deliveries x SET company_id = COALESCE(
            (SELECT company_id FROM purchase_orders p WHERE p.id = x.po_id), legacy) WHERE company_id IS NULL;
        UPDATE payments x SET company_id = COALESCE(
            (SELECT company_id FROM invoices i WHERE i.id = x.invoice_id), legacy) WHERE company_id IS NULL;
        UPDATE ledger_entries x SET company_id = COALESCE(
            (SELECT company_id FROM payments p WHERE p.id = x.payment_id), legacy) WHERE company_id IS NULL;
        UPDATE exceptions x SET company_id = COALESCE(
            (SELECT company_id FROM payments p WHERE p.id = x.payment_id), legacy) WHERE company_id IS NULL;
        UPDATE reviews x SET company_id = COALESCE(
            (SELECT company_id FROM invoices i WHERE i.id = x.invoice_id), legacy) WHERE company_id IS NULL;
        UPDATE extractions SET company_id = legacy WHERE company_id IS NULL;
        UPDATE audit_log SET company_id = legacy WHERE company_id IS NULL;
    END IF;
END $$;

ALTER TABLE vendors ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE vendor_bank_history ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE purchase_orders ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE deliveries ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE invoices ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE payments ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE ledger_entries ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE exceptions ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE reviews ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE extractions ALTER COLUMN company_id SET NOT NULL;

ALTER TABLE audit_log ALTER COLUMN company_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS vendors_company_id_id ON vendors (company_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS purchase_orders_company_id_id ON purchase_orders (company_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS invoices_company_id_id ON invoices (company_id, id);

CREATE UNIQUE INDEX IF NOT EXISTS payments_company_id_id ON payments (company_id, id);

-- A row can only point at a parent that belongs to the SAME company. The database refuses the rest.
DO $$
DECLARE
    fk RECORD;
BEGIN
    FOR fk IN
        SELECT * FROM (VALUES
            ('vendor_bank_history', 'vbh_vendor_same_company',
             'FOREIGN KEY (company_id, vendor_id) REFERENCES vendors (company_id, id)'),
            ('purchase_orders', 'po_vendor_same_company',
             'FOREIGN KEY (company_id, vendor_id) REFERENCES vendors (company_id, id)'),
            ('deliveries', 'deliveries_po_same_company',
             'FOREIGN KEY (company_id, po_id) REFERENCES purchase_orders (company_id, id)'),
            ('invoices', 'invoices_vendor_same_company',
             'FOREIGN KEY (company_id, vendor_id) REFERENCES vendors (company_id, id)'),
            ('invoices', 'invoices_po_same_company',
             'FOREIGN KEY (company_id, po_id) REFERENCES purchase_orders (company_id, id)'),
            ('payments', 'payments_invoice_same_company',
             'FOREIGN KEY (company_id, invoice_id) REFERENCES invoices (company_id, id)'),
            ('ledger_entries', 'ledger_payment_same_company',
             'FOREIGN KEY (company_id, payment_id) REFERENCES payments (company_id, id)'),
            ('exceptions', 'exceptions_payment_same_company',
             'FOREIGN KEY (company_id, payment_id) REFERENCES payments (company_id, id)'),
            ('reviews', 'reviews_invoice_same_company',
             'FOREIGN KEY (company_id, invoice_id) REFERENCES invoices (company_id, id)'),
            ('extractions', 'extractions_invoice_same_company',
             'FOREIGN KEY (company_id, invoice_id) REFERENCES invoices (company_id, id) ON DELETE SET NULL (invoice_id)')
        ) AS t (tbl, name, def)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = fk.name) THEN
            EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I %s', fk.tbl, fk.name, fk.def);
        END IF;
    END LOOP;
END $$;

-- company_id can never be changed after a row exists.
CREATE OR REPLACE FUNCTION forbid_company_change() RETURNS trigger AS $$
BEGIN
    IF NEW.company_id IS DISTINCT FROM OLD.company_id THEN
        RAISE EXCEPTION 'company_id cannot be changed on %', TG_TABLE_NAME;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['vendors', 'vendor_bank_history', 'purchase_orders', 'deliveries', 'invoices',
                             'payments', 'ledger_entries', 'exceptions', 'reviews', 'extractions', 'audit_log']
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = t || '_no_company_change') THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE UPDATE OF company_id ON %I FOR EACH ROW EXECUTE FUNCTION forbid_company_change()',
                t || '_no_company_change', t);
        END IF;
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS idx_vbh_company ON vendor_bank_history (company_id);

CREATE INDEX IF NOT EXISTS idx_po_company ON purchase_orders (company_id);

CREATE INDEX IF NOT EXISTS idx_deliveries_company ON deliveries (company_id);

CREATE INDEX IF NOT EXISTS idx_extractions_company ON extractions (company_id);

CREATE INDEX IF NOT EXISTS idx_exceptions_company ON exceptions (company_id);

-- Ledger: the same line can never be written twice for one payment.
CREATE UNIQUE INDEX IF NOT EXISTS vbh_one_current ON vendor_bank_history (vendor_id)
WHERE
    valid_to IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ledger_line_once ON ledger_entries (
    payment_id,
    entry_type,
    account,
    direction
)
WHERE
    payment_id IS NOT NULL;

-- Webhooks: every PayPal event is recorded once; a replay is recognised and not processed again.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    batch_id TEXT,
    payload JSONB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    result TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);

-- Login rate limiting that survives a restart and works across several app processes.
CREATE TABLE IF NOT EXISTS login_attempts (
    id BIGSERIAL PRIMARY KEY,
    bucket TEXT NOT NULL,
    attempted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_bucket ON login_attempts (bucket, attempted_at);

-- One-time password reset tokens (only the hash is stored).
CREATE TABLE IF NOT EXISTS password_resets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_by UUID REFERENCES users (id),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A child row that does not name a company takes its parent's. A parent in another company is
-- still refused by the composite foreign keys above.
CREATE OR REPLACE FUNCTION fill_company_from_parent() RETURNS trigger AS $$
DECLARE
    parent_id UUID;
    cid UUID;
BEGIN
    IF NEW.company_id IS NULL THEN
        parent_id := (to_jsonb(NEW) ->> TG_ARGV[1])::uuid;
        IF parent_id IS NOT NULL THEN
            EXECUTE format('SELECT company_id FROM %I WHERE id = $1', TG_ARGV[0]) INTO cid USING parent_id;
            NEW.company_id := cid;
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('vendor_bank_history', 'vendors', 'vendor_id'),
            ('purchase_orders', 'vendors', 'vendor_id'),
            ('deliveries', 'purchase_orders', 'po_id'),
            ('invoices', 'vendors', 'vendor_id'),
            ('payments', 'invoices', 'invoice_id'),
            ('ledger_entries', 'payments', 'payment_id'),
            ('exceptions', 'payments', 'payment_id'),
            ('reviews', 'invoices', 'invoice_id'),
            ('extractions', 'invoices', 'invoice_id')
        ) AS t (tbl, parent, col)
    LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = r.tbl || '_fill_company') THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE INSERT ON %I FOR EACH ROW EXECUTE FUNCTION fill_company_from_parent(%L, %L)',
                r.tbl || '_fill_company', r.tbl, r.parent, r.col);
        END IF;
    END LOOP;
END $$;

CREATE OR REPLACE VIEW precedents AS
SELECT
    r.id AS review_id,
    r.invoice_id,
    i.vendor_id,
    i.invoice_number,
    r.original_ai_decision AS ai_decision,
    r.human_decision,
    r.reviewer,
    r.note,
    r.created_at AS reviewed_at,
    COALESCE(
        (
            SELECT array_agg(
                    s
                    ORDER BY s
                )
            FROM jsonb_array_elements_text(f.inputs -> 'signals') AS s
        ),
        ARRAY[]::text []
    ) AS signals,
    CASE
        WHEN r.human_decision = 'reject' THEN 'rejected'
        WHEN EXISTS (
            SELECT 1
            FROM exceptions e
                JOIN payments p ON p.id = e.payment_id
            WHERE
                p.invoice_id = i.id
        ) THEN 'exception'
        WHEN i.status = 'paid' THEN 'paid_fine'
        ELSE 'not_paid_yet'
    END AS outcome,
    i.company_id
FROM reviews r
    JOIN invoices i ON i.id = r.invoice_id
    LEFT JOIN LATERAL (
        SELECT a.inputs
        FROM audit_log a
        WHERE
            a.entity_id = r.invoice_id
            AND a.stage = 'fraud'
            AND a.reason LIKE 'Fraud risk %'
            AND a.created_at <= r.created_at
        ORDER BY a.id DESC
        LIMIT 1
    ) f ON true;

-- B6: quantity. All three are optional: the quantity check runs only when the PO and the
-- invoice both state one (see backend/checks.py). Deliveries without a quantity are not counted.
ALTER TABLE purchase_orders
ADD COLUMN IF NOT EXISTS quantity NUMERIC(14, 3) CHECK (
    quantity IS NULL
    OR quantity > 0
);

ALTER TABLE deliveries
ADD COLUMN IF NOT EXISTS quantity NUMERIC(14, 3) CHECK (
    quantity IS NULL
    OR quantity > 0
);

ALTER TABLE invoices
ADD COLUMN IF NOT EXISTS quantity NUMERIC(14, 3) CHECK (
    quantity IS NULL
    OR quantity > 0
);

-- A2: maker-checker. One admin asks, a different admin approves.
CREATE TABLE IF NOT EXISTS pending_changes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    company_id UUID NOT NULL REFERENCES companies (id),
    kind TEXT NOT NULL, -- 'vendor_email'
    target_id UUID NOT NULL, -- the vendor id
    payload JSONB NOT NULL, -- {"new_email": "..."}
    requested_by UUID NOT NULL REFERENCES users (id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending',
            'approved',
            'rejected'
        )
    ),
    decided_by UUID REFERENCES users (id),
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- only one open request per vendor at a time
CREATE UNIQUE INDEX IF NOT EXISTS pending_one_per_target ON pending_changes (kind, target_id)
WHERE
    status = 'pending';

ALTER TABLE vendors
ADD COLUMN IF NOT EXISTS deactivated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS attachments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    company_id UUID NOT NULL REFERENCES companies (id),
    entity_type TEXT NOT NULL CHECK (
        entity_type IN ('purchase_order', 'delivery')
    ),
    entity_id UUID NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    data BYTEA NOT NULL,
    uploaded_by UUID REFERENCES users (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attachments_entity ON attachments (entity_type, entity_id);

-- Four-eyes rule: ON by default. A company may switch it off (admin, audited), e.g. a one-person demo.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS require_four_eyes BOOLEAN NOT NULL DEFAULT TRUE;
