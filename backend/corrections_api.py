"""Endpoints for the needs-review flow. Same X-Review-Token as the rest of the review API."""
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from backend.corrections import CorrectionError, correct_extraction
from backend.db import get_conn
from backend.review_api import require_token

router = APIRouter(prefix="/extractions", tags=["extractions"])


class Correction(BaseModel):
    reviewer: str
    note: str
    corrections: dict[str, Any]


@router.get("")
def list_needs_review(x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source_name, reason, confidence, extracted, field_confidence, created_at "
            "FROM extractions WHERE status = 'needs_review' ORDER BY created_at DESC").fetchall()
    return {"items": [{"id": str(r[0]), "source_name": r[1], "reason": r[2],
                       "confidence": float(r[3]) if r[3] is not None else None,
                       "extracted": r[4], "field_confidence": r[5],
                       "created_at": r[6].isoformat()} for r in rows]}


@router.post("/{extraction_id}/correct")
def correct(extraction_id: str, body: Correction,
            x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    try:
        return correct_extraction(extraction_id, body.reviewer, body.note, body.corrections)
    except CorrectionError as e:
        status = {"not_found": 404, "not_allowed": 409}.get(e.code, 422)
        raise HTTPException(status, e.message)