"""Ask PayMaster endpoint. Same X-Review-Token as the review API. Read-only."""
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends

from backend.auth import current_member
from backend.chat import ask

router = APIRouter(tags=["chat"])


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


@router.post("/chat")
def chat(body: ChatIn, user: dict = Depends(current_member)):
    return ask(body.question, user["company_id"])