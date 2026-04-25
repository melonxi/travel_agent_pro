from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


class BacktrackRequest(BaseModel):
    to_phase: int
    reason: str = ""
