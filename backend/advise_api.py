"""Advise endpoint. Same X-Review-Token as the review API. Read-only, GET only."""
from backend.advise import advise
from fastapi import APIRouter, Depends
from backend.auth import current_member

router = APIRouter(tags=["advise"])

@router.get("/advise")
def get_advise(user: dict = Depends(current_member)):
    return advise(user["company_id"])