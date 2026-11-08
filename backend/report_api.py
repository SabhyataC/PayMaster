"""Investigation report endpoint (read-only, any logged-in member of the company)."""
from fastapi import APIRouter, Depends, HTTPException

from backend.auth import current_member
from backend.db import get_conn
from backend.report import build_report
from backend.timeline import find_invoice_id

router = APIRouter(prefix="/report", tags=["report"])


@router.get("/invoice/{ref}")
def report_for_invoice(ref: str, user: dict = Depends(current_member)):
    with get_conn() as conn:
        inv = find_invoice_id(conn, ref, user["company_id"])
        out = build_report(conn, inv, user["company_id"]) if inv else None
    if out is None:
        raise HTTPException(404, "Invoice not found")
    return out
