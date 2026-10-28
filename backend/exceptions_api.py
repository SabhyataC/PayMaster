"""Exception endpoints (Reconcile). Same X-Review-Token as the review API."""
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.audit import log_decision
from backend.db import get_conn
from backend.review_api import require_token
from backend.explain import explain_exception

router = APIRouter(prefix="/exceptions", tags=["exceptions"])

COLS = "id, payment_id, type, detail, status, created_at, resolved_by, resolved_at, note, explanation"

def _row(r):
    keys = ["id", "payment_id", "type", "detail", "status",
            "created_at", "resolved_by", "resolved_at", "note", "explanation"]
    d = dict(zip(keys, r))
    d["payment_id"] = str(d["payment_id"]) if d["payment_id"] else None
    return d


class Resolve(BaseModel):
    resolved_by: str
    note: str


@router.get("")
def list_exceptions(status: str = "open", x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    if status not in ("open", "resolved"):
        raise HTTPException(422, "status must be open or resolved")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {COLS} FROM exceptions WHERE status = %s ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    return {"items": [_row(r) for r in rows]}


@router.get("/{exception_id}")
def get_exception(exception_id: int, x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        r = conn.execute(
            f"SELECT {COLS} FROM exceptions WHERE id = %s", (exception_id,)
        ).fetchone()
    if not r:
        raise HTTPException(404, "exception not found")
    return _row(r)


@router.post("/{exception_id}/resolve")
def resolve_exception(exception_id: int, body: Resolve,
                      x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    if not body.resolved_by.strip() or not body.note.strip():
        raise HTTPException(422, "resolved_by and note are both required")
    with get_conn() as conn:
        r = conn.execute(
            "SELECT payment_id, type, status FROM exceptions WHERE id = %s",
            (exception_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, "exception not found")
        if r[2] != "open":
            raise HTTPException(409, "exception is already resolved")
        conn.execute(
            """UPDATE exceptions
               SET status = 'resolved', resolved_by = %s, resolved_at = now(), note = %s
               WHERE id = %s""",
            (body.resolved_by.strip(), body.note.strip(), exception_id),
        )
        log_decision(
            conn, stage="reconcile", entity_type="payment", entity_id=r[0],
            decision="exception_resolved",
            reason=f"{r[1]} resolved by {body.resolved_by.strip()}: {body.note.strip()}",
            inputs={"exception_id": exception_id}, actor="human",
        )
        conn.commit()
    return {"id": exception_id, "status": "resolved"}

@router.post("/{exception_id}/explain")
def explain(exception_id: int, x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        result = explain_exception(conn, exception_id)
        if result is None:
            raise HTTPException(404, "exception not found")
        conn.commit()
    return result

import uuid

from backend.reconcile import run_reconcile

run_router = APIRouter(prefix="/reconcile", tags=["reconcile"])


@run_router.post("/run")
def run_now(x_review_token: str | None = Header(default=None)):
    """Check every payment against PayPal and log any new exceptions. Moves no money itself
    (the only automatic action is cancelling an unclaimed payout, as built on Nov 2)."""
    require_token(x_review_token)
    with get_conn() as conn:
        counts = run_reconcile(conn)
        log_decision(
            conn, stage="reconcile", entity_type="reconcile_run", entity_id=uuid.uuid4(),
            decision="ran", reason=f"Reconcile run: {sum(counts.values())} new exception(s)",
            inputs={"new_exceptions": counts}, actor="system")
        conn.commit()
    return {"new_exceptions": counts}