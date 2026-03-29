# backend/agent/hooks.py
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable, Coroutine


HookFn = Callable[..., Coroutine[Any, Any, None]]


class HookManager:
    def __init__(self) -> None:
        self._hooks: dict[str, list[HookFn]] = defaultdict(list)

    def register(self, event: str, fn: HookFn) -> None:
        self._hooks[event].append(fn)

    async def run(self, event: str, *args: Any, **kwargs: Any) -> None:
        for fn in self._hooks.get(event, []):
            if args and not kwargs:
                await fn(args[0] if len(args) == 1 else args)
            elif kwargs:
                await fn(**kwargs)
            else:
                await fn()
