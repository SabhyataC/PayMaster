"""Review endpoints. Every call needs the X-Review-Token header (REVIEW_TOKEN in .env).
Without a token configured the endpoints refuse to work, because a tunnel to the webhook
can expose this API to the internet."""
import hmac
import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.db import get_conn
from backend.pay import pay_invoice
from backend.review import ReviewError, decide, get_case, list_queue

router = APIRouter(prefix="/review", tags=["review"])

STATUS_FOR = {"not_found": 404, "already_decided": 409, "not_allowed": 409, "hard_reject": 409}


def require_token(x_review_token: str | None = Header(default=None)):
    expected = os.environ.get("REVIEW_TOKEN", "")
    if not expected:
        raise HTTPException(503, "REVIEW_TOKEN is not set on the server")
    if not x_review_token or not hmac.compare_digest(x_review_token, expected):
        raise HTTPException(401, "bad or missing X-Review-Token")


class Decision(BaseModel):
    decision: str     # "approve" | "reject"
    reviewer: str
    note: str


class PayRelease(BaseModel):
    reviewer: str


def _fail(e: ReviewError):
    raise HTTPException(STATUS_FOR.get(e.code, 422), {"code": e.code, "message": e.message})


@router.get("/queue")
def queue(x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        return {"items": list_queue(conn)}


@router.get("/{invoice_id}")
def case(invoice_id: str, x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    try:
        with get_conn() as conn:
            return get_case(conn, invoice_id)
    except ReviewError as e:
        _fail(e)


@router.post("/{invoice_id}/decision")
def decision(invoice_id: str, body: Decision, x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    try:
        with get_conn() as conn:
            return decide(conn, invoice_id, body.decision, body.reviewer, body.note)
    except ReviewError as e:
        _fail(e)


@router.post("/{invoice_id}/pay")
def release_payment(invoice_id: str, body: PayRelease, x_review_token: str | None = Header(default=None)):
    """Pay an approved invoice. A person naming themselves here is what releases a policy hold."""
    require_token(x_review_token)
    if not body.reviewer.strip():
        raise HTTPException(422, {"code": "reviewer_required", "message": "Say who is releasing the payment"})
    with get_conn() as conn:
        return pay_invoice(conn, invoice_id, approved_by=body.reviewer.strip())