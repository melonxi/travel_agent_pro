from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar


@dataclass
class MemoryJobSnapshot:
    session_id: str
    user_id: str
    turn_id: str
    user_messages: list[str]
    submitted_user_count: int
    plan_snapshot: object | None = None


def _clip_user_messages(
    user_messages: list[str],
    *,
    max_messages: int,
    max_chars: int,
) -> list[str]:
    recent = [message for message in user_messages if message][-max_messages:]
    if not recent:
        return []

    kept: list[str] = []
    total_chars = 0
    for message in reversed(recent):
        message_len = len(message)
        if kept and total_chars + message_len > max_chars:
            break
        if not kept and message_len > max_chars:
            kept.append(message[-max_chars:])
            break
        kept.append(message)
        total_chars += message_len
    kept.reverse()
    return kept


def build_gate_user_window(
    user_messages: list[str],
    *,
    max_messages: int = 3,
    max_chars: int = 1200,
) -> list[str]:
    return _clip_user_messages(
        user_messages,
        max_messages=max_messages,
        max_chars=max_chars,
    )


def build_extraction_user_window(
    user_messages: list[str],
    *,
    last_consumed_user_count: int,
    submitted_user_count: int,
    max_messages: int = 8,
    max_chars: int = 3000,
) -> list[str]:
    start = max(0, min(last_consumed_user_count, len(user_messages)))
    end = max(start, min(submitted_user_count, len(user_messages)))
    incremental = user_messages[start:end]
    return _clip_user_messages(
        incremental,
        max_messages=max_messages,
        max_chars=max_chars,
    )


Runner = Callable[[MemoryJobSnapshot], Awaitable[None]]
T = TypeVar("T")


class MemoryJobScheduler:
    def __init__(self, *, runner: Runner):
        self._runner = runner
        self.running_task: asyncio.Task[None] | None = None
        self.pending_snapshot: MemoryJobSnapshot | None = None
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    def submit(self, snapshot: MemoryJobSnapshot) -> None:
        self._idle_event.clear()
        current_task = self.running_task
        if current_task is None or current_task.done():
            self._start(snapshot)
            return
        self.pending_snapshot = snapshot

    async def wait_for_idle(self) -> None:
        await self._idle_event.wait()

    def _start(self, snapshot: MemoryJobSnapshot) -> None:
        self.running_task = asyncio.create_task(self._run(snapshot))

    async def _run(self, snapshot: MemoryJobSnapshot) -> None:
        try:
            await self._runner(snapshot)
        finally:
            next_snapshot = self.pending_snapshot
            self.pending_snapshot = None
            if next_snapshot is None:
                self.running_task = None
                self._idle_event.set()
                return
            self._start(next_snapshot)
