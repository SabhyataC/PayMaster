"""Ask PayMaster endpoint. Same X-Review-Token as the review API. Read-only."""
from fastapi import APIRouter, Header
from pydantic import BaseModel, Field

from backend.chat import ask
from backend.review_api import require_token

router = APIRouter(tags=["chat"])


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@router.post("/chat")
def chat(body: ChatIn, x_review_token: str | None = Header(default=None)):
    require_token(x_review_token)
    return ask(body.question)