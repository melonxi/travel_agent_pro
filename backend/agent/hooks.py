# backend/agent/hooks.py
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Coroutine


HookFn = Callable[..., Coroutine[Any, Any, None]]
GateFn = Callable[..., Coroutine[Any, Any, "GateResult"]]


@dataclass
class GateResult:
    allowed: bool = True
    feedback: str | None = None


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFn]] = defaultdict(list)
        self._gates: dict[str, list[GateFn]] = defaultdict(list)

    def register(self, event: str, fn: HookFn) -> None:
        self._hooks[event].append(fn)

    def register_gate(self, event: str, fn: GateFn) -> None:
        self._gates[event].append(fn)

    async def run(self, event: str, *args: Any, **kwargs: Any) -> None:
        for fn in self._hooks.get(event, []):
            if args and not kwargs:
                await fn(args[0] if len(args) == 1 else args)
            elif kwargs:
                await fn(**kwargs)
            else:
                await fn()

    async def run_gate(self, event: str, **kwargs: Any) -> GateResult:
        for fn in self._gates.get(event, []):
            result = await fn(**kwargs)
            if not result.allowed:
                return result
        return GateResult(allowed=True)
