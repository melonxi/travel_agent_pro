from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default_user"


class BacktrackRequest(BaseModel):
    to_phase: int
    reason: str = ""


class SteerRequest(BaseModel):
    """D4：运行中引导。run 进行中由 /steer 入队，不中断主 run。"""

    text: str
