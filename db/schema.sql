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

ALTER TABLE payments ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 1;

-- One payment per invoice per attempt number. This is the database-level
-- never-pay-twice guard, even if two requests arrive at the same moment.
CREATE UNIQUE INDEX IF NOT EXISTS payments_invoice_attempt
    ON payments (invoice_id, attempt);

ALTER TABLE exceptions ADD COLUMN IF NOT EXISTS explanation JSONB;

-- Human review decisions. Case memory (Nov 4) learns from this table.
CREATE TABLE IF NOT EXISTS reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid (),
    invoice_id UUID NOT NULL REFERENCES invoices (id),
    original_ai_decision TEXT, -- approve / hold / reject, from the Verify audit row
    human_decision TEXT NOT NULL CHECK (human_decision IN ('approve', 'reject')),
    reviewer TEXT NOT NULL,
    note TEXT NOT NULL,
    confirmed_with_vendor BOOLEAN NOT NULL DEFAULT false,
    status_before TEXT,
    status_after TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reviews_invoice ON reviews (invoice_id);

-- Case memory: one row per past human review, with the signals that fired and the outcome.
CREATE OR REPLACE VIEW precedents AS
SELECT r.id AS review_id,
       r.invoice_id,
       i.vendor_id,
       i.invoice_number,
       r.original_ai_decision AS ai_decision,
       r.human_decision,
       r.reviewer,
       r.note,
       r.created_at AS reviewed_at,
       COALESCE(
           (SELECT array_agg(s ORDER BY s)
            FROM jsonb_array_elements_text(f.inputs -> 'signals') AS s),
           ARRAY[]::text[]) AS signals,
       CASE
           WHEN r.human_decision = 'reject' THEN 'rejected'
           WHEN EXISTS (SELECT 1 FROM exceptions e
                        JOIN payments p ON p.id = e.payment_id
                        WHERE p.invoice_id = i.id) THEN 'exception'
           WHEN i.status = 'paid' THEN 'paid_fine'
           ELSE 'not_paid_yet'
       END AS outcome
FROM reviews r
JOIN invoices i ON i.id = r.invoice_id
LEFT JOIN LATERAL (
    SELECT a.inputs FROM audit_log a
    WHERE a.entity_id = r.invoice_id AND a.stage = 'fraud'
      AND a.reason LIKE 'Fraud risk %' AND a.created_at <= r.created_at
    ORDER BY a.id DESC LIMIT 1
) f ON true;