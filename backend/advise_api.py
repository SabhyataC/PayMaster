"""Advise endpoint. Same X-Review-Token as the review API. Read-only, GET only."""
from fastapi import APIRouter, Header

from backend.advise import advise
from backend.review_api import require_token

router = APIRouter(tags=["advise"])


@router.get("/advise")
def get_advise(x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    return advise()