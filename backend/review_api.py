"""Review endpoints. Every call needs the X-Review-Token header (REVIEW_TOKEN in .env).
Without a token configured the endpoints refuse to work, because a tunnel to the webhook
can expose this API to the internet."""

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from fastapi import APIRouter, Depends, Header, HTTPException
from backend.auth import current_member
from backend.db import get_conn
from backend.pay import pay_invoice
from backend.review import ReviewError, decide, get_case, list_queue

router = APIRouter(prefix="/review", tags=["review"])

STATUS_FOR = {"not_found": 404, "already_decided": 409, "not_allowed": 409, "hard_reject": 409}


class Decision(BaseModel):
    decision: str     # "approve" | "reject"
    note: str
    confirmed_with_vendor: bool = False


def _fail(e: ReviewError):
    raise HTTPException(STATUS_FOR.get(e.code, 422), {"code": e.code, "message": e.message})

def _own(conn, invoice_id, company_id):
    """404 unless this invoice belongs to the user's company."""
    row = conn.execute(
        "SELECT 1 FROM invoices WHERE id::text = %s AND company_id = %s",
        (str(invoice_id), company_id)).fetchone()
    if not row:
        raise HTTPException(404, {"code": "not_found", "message": "No such invoice"})

@router.get("/queue")
def queue(user: dict = Depends(current_member)):
    with get_conn() as conn:
        return {"items": list_queue(conn, user["company_id"])}

@router.get("/{invoice_id}")
def case(invoice_id: str, user: dict = Depends(current_member)):
    try:
        with get_conn() as conn:
            _own(conn, invoice_id, user["company_id"])
            return get_case(conn, invoice_id)
    except ReviewError as e:
        _fail(e)

@router.post("/{invoice_id}/decision")
def decision(invoice_id: str, body: Decision, user: dict = Depends(current_member)):
    try:
        with get_conn() as conn:
            _own(conn, invoice_id, user["company_id"])
            return decide(conn, invoice_id, body.decision, user["full_name"], body.note, body.confirmed_with_vendor)
    except ReviewError as e:
        _fail(e)

@router.post("/{invoice_id}/pay")
def release_payment(invoice_id: str, user: dict = Depends(current_member)):
    """Pay an approved invoice. The logged-in person releasing it is recorded as the approver."""
    with get_conn() as conn:
        _own(conn, invoice_id, user["company_id"])
        return pay_invoice(conn, invoice_id, approved_by=user["full_name"])