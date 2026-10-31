"""Read-only console endpoints. Same X-Review-Token as the review API."""
from fastapi import APIRouter, Header

from backend.db import get_conn
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from backend.auth import current_member

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
def invoices(user: dict = Depends(current_member)):
    with get_conn() as conn:
        return {"items": _rows(conn, """
            SELECT i.id, i.invoice_number, v.name AS vendor, i.amount, i.currency, i.status,
                   (SELECT a.decision FROM audit_log a
                     WHERE a.entity_id = i.id AND a.stage = 'fraud'
                       AND a.reason LIKE 'Fraud risk %%'
                     ORDER BY a.id DESC LIMIT 1) AS fraud_risk,
                   i.received_at
            FROM invoices i JOIN vendors v ON v.id = i.vendor_id
            WHERE i.company_id = %s
            ORDER BY i.received_at DESC""", (user["company_id"],))}


@router.get("/payments")
def payments(user: dict = Depends(current_member)):
    with get_conn() as conn:
        return {"items": _rows(conn, """
            SELECT p.id, p.invoice_id, i.invoice_number, p.amount, p.currency, p.status,
                   p.paypal_batch_id, p.created_at
            FROM payments p JOIN invoices i ON i.id = p.invoice_id
            WHERE i.company_id = %s
            ORDER BY p.created_at DESC""", (user["company_id"],))}


@router.get("/ledger")
def ledger(invoice_id: str | None = None, user: dict = Depends(current_member)):
    with get_conn() as conn:
        return {"items": _rows(conn, """
            SELECT l.id, l.payment_id, i.invoice_number, l.entry_type, l.account,
                   l.direction, l.amount, l.currency, l.description, l.created_at
            FROM ledger_entries l
            JOIN payments p ON p.id = l.payment_id
            JOIN invoices i ON i.id = p.invoice_id
            WHERE i.company_id = %s
              AND (%s::text IS NULL OR i.id::text = %s OR i.invoice_number = %s)
            ORDER BY l.created_at, l.id""", (user["company_id"], invoice_id, invoice_id, invoice_id))}


@router.get("/summary")
def summary(user: dict = Depends(current_member)):
    """Numbers for the console's summary cards."""
    cid = user["company_id"]
    with get_conn() as conn:
        by_status = {s: n for s, n in conn.execute(
            "SELECT status, count(*) FROM invoices WHERE company_id = %s GROUP BY status", (cid,)).fetchall()}
        paid_total = {c: float(t) for c, t in conn.execute(
            "SELECT p.currency, COALESCE(sum(p.amount), 0) FROM payments p "
            "JOIN invoices i ON i.id = p.invoice_id "
            "WHERE p.status = 'success' AND i.company_id = %s GROUP BY p.currency", (cid,)).fetchall()}
        open_exceptions = conn.execute(
            "SELECT count(*) FROM exceptions e JOIN payments p ON p.id = e.payment_id "
            "JOIN invoices i ON i.id = p.invoice_id "
            "WHERE e.status = 'open' AND i.company_id = %s", (cid,)).fetchone()[0]
        needs_review = conn.execute(
            "SELECT count(*) FROM extractions WHERE status = 'needs_review' AND company_id = %s", (cid,)).fetchone()[0]
    return {
        "invoices_by_status": by_status,
        "invoices_total": sum(by_status.values()),
        "held_count": by_status.get("held", 0),
        "paid_total": paid_total,
        "open_exceptions": open_exceptions,
        "needs_review_documents": needs_review,
    }

@router.get("/payments/{payment_id}")
def payment_detail(payment_id: str, user: dict = Depends(current_member)):
    """One payment with its settlement (fx rate, fee) and ledger rows, plus a balance check."""
    with get_conn() as conn:
        pay = _rows(conn, """
            SELECT p.id, p.invoice_id, i.invoice_number, v.name AS vendor, p.destination_email,
                   p.amount, p.currency, p.status, p.paypal_batch_id, p.created_at
            FROM payments p
            JOIN invoices i ON i.id = p.invoice_id
            JOIN vendors v ON v.id = i.vendor_id
            WHERE p.id::text = %s AND i.company_id = %s""", (payment_id, user["company_id"]))
        if not pay:
            raise HTTPException(404, f"no payment {payment_id}")
        settlement = _rows(conn, """
            SELECT amount_sent, currency_sent, amount_received, currency_received,
                   fx_rate, fee_amount, fee_currency, settled_at
            FROM settlements WHERE payment_id::text = %s""", (payment_id,))
        entries = _rows(conn, """
            SELECT id, entry_type, account, direction, amount, currency, description, created_at
            FROM ledger_entries WHERE payment_id::text = %s
            ORDER BY created_at, id""", (payment_id,))

    # Same rule as settle.check_balanced: debits equal credits within each currency.
    totals = {}
    for e in entries:
        t = totals.setdefault(e["currency"], {"debit": Decimal("0"), "credit": Decimal("0")})
        t[e["direction"]] += Decimal(str(e["amount"]))
    balance = {c: {"debit": float(t["debit"]), "credit": float(t["credit"]),
                   "balanced": t["debit"] == t["credit"]} for c, t in totals.items()}
    return {
        "payment": pay[0],
        "settlement": settlement[0] if settlement else None,
        "ledger": entries,
        "balance": balance,
        # None when there are no ledger rows yet (payment not settled), so the UI can say so.
        "balanced": all(b["balanced"] for b in balance.values()) if balance else None,
    }