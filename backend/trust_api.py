"""Trust Graph endpoints (read-only, any logged-in member of the company)."""
from fastapi import APIRouter, Depends, HTTPException

from backend.auth import current_member
from backend.db import get_conn
from backend.timeline import find_invoice_id
from backend.trust import build_chain

router = APIRouter(prefix="/trust", tags=["trust"])


@router.get("/invoice/{ref}")
def trust_for_invoice(ref: str, user: dict = Depends(current_member)):
    """ref = invoice UUID or invoice number. Shows the most recent payment, if any."""
    with get_conn() as conn:
        inv = find_invoice_id(conn, ref, user["company_id"])
        out = build_chain(conn, inv, user["company_id"]) if inv else None
    if out is None:
        raise HTTPException(404, "Invoice not found")
    return out


@router.get("/payment/{payment_id}")
def trust_for_payment(payment_id: str, user: dict = Depends(current_member)):
    with get_conn() as conn:
        row = conn.execute("SELECT p.invoice_id FROM payments p JOIN invoices i ON i.id = p.invoice_id "
                           "WHERE p.id::text = %s AND i.company_id = %s", (payment_id, user["company_id"])).fetchone()
        out = build_chain(conn, row[0], user["company_id"], payment_id) if row else None
    if out is None:
        raise HTTPException(404, "Payment not found")
    return out
