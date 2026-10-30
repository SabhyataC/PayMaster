"""Audit endpoint: one invoice's full story, in order. Same X-Review-Token as the review API.
Read-only. The timeline itself comes from backend/timeline.py."""
from fastapi import APIRouter, Header, HTTPException

from backend.db import get_conn
from backend.review_api import require_token
from backend.timeline import find_invoice_id, get_timeline

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/{invoice_id}")
def audit_timeline(invoice_id: str, x_review_token: str | None = Header(default=None)):
    """invoice_id can be the invoice UUID or the invoice number (INV-2001)."""
    require_token(x_review_token)
    with get_conn() as conn:
        inv = find_invoice_id(conn, invoice_id)
        if inv is None:
            raise HTTPException(404, f"no invoice {invoice_id}")
        head = conn.execute(
            """SELECT i.invoice_number, v.name, i.status
               FROM invoices i JOIN vendors v ON v.id = i.vendor_id
               WHERE i.id = %s""", (inv,)).fetchone()
        events = get_timeline(conn, inv)
    return {
        "invoice_id": str(inv),
        "invoice_number": head[0],
        "vendor": head[1],
        "status": head[2],
        "events": events,
    }