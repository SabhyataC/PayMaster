"""Read-only console endpoints. Same X-Review-Token as the review API."""
from fastapi import APIRouter, Header

from backend.db import get_conn
from backend.review_api import require_token

router = APIRouter(tags=["console"])


def _rows(conn, sql, params=()):
    cur = conn.execute(sql, params)
    cols = [c.name for c in cur.description]
    out = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif v.__class__.__name__ == "Decimal":
                d[k] = float(v)
            elif v.__class__.__name__ == "UUID":
                d[k] = str(v)
        out.append(d)
    return out


@router.get("/invoices")
def invoices(x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        return {"items": _rows(conn, """
            SELECT i.id, i.invoice_number, v.name AS vendor, i.amount, i.currency, i.status,
                   (SELECT a.decision FROM audit_log a
                     WHERE a.entity_id = i.id AND a.stage = 'fraud'
                       AND a.reason LIKE 'Fraud risk %%'
                     ORDER BY a.id DESC LIMIT 1) AS fraud_risk,
                   i.received_at
            FROM invoices i JOIN vendors v ON v.id = i.vendor_id
            ORDER BY i.received_at DESC""")}


@router.get("/payments")
def payments(x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        return {"items": _rows(conn, """
            SELECT p.id, p.invoice_id, i.invoice_number, p.amount, p.currency, p.status,
                   p.paypal_batch_id, p.created_at
            FROM payments p JOIN invoices i ON i.id = p.invoice_id
            ORDER BY p.created_at DESC""")}


@router.get("/ledger")
def ledger(invoice_id: str | None = None, x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        return {"items": _rows(conn, """
            SELECT l.id, l.payment_id, i.invoice_number, l.entry_type, l.account,
                   l.direction, l.amount, l.currency, l.description, l.created_at
            FROM ledger_entries l
            JOIN payments p ON p.id = l.payment_id
            JOIN invoices i ON i.id = p.invoice_id
            WHERE (%s::text IS NULL OR i.id::text = %s OR i.invoice_number = %s)
            ORDER BY l.created_at, l.id""", (invoice_id, invoice_id, invoice_id))}