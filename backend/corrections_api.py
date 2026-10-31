"""Endpoints for the needs-review flow. Same X-Review-Token as the rest of the review API."""
from typing import Any


from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from backend.auth import current_member

from backend.corrections import CorrectionError, correct_extraction
from backend.db import get_conn

router = APIRouter(prefix="/extractions", tags=["extractions"])


class Correction(BaseModel):
    note: str
    corrections: dict[str, Any]


@router.get("")
def list_needs_review(user: dict = Depends(current_member)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source_name, reason, confidence, extracted, field_confidence, created_at "
            "FROM extractions WHERE status = 'needs_review' AND company_id = %s "
            "ORDER BY created_at DESC", (user["company_id"],)).fetchall()
    return {"items": [{"id": str(r[0]), "source_name": r[1], "reason": r[2],
                       "confidence": float(r[3]) if r[3] is not None else None,
                       "extracted": r[4], "field_confidence": r[5],
                       "created_at": r[6].isoformat()} for r in rows]}


@router.post("/{extraction_id}/correct")
def correct(extraction_id: str, body: Correction, user: dict = Depends(current_member)):
    try:
        return correct_extraction(extraction_id, user["full_name"], body.note, body.corrections, user["company_id"])
    except CorrectionError as e:
        status = {"not_found": 404, "not_allowed": 409}.get(e.code, 422)
        raise HTTPException(status, e.message)