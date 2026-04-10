# backend/tools/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine


class ToolError(Exception):
    def __init__(self, message: str, error_code: str = "UNKNOWN", suggestion: str = ""):
        super().__init__(message)
        self.error_code = error_code
        self.suggestion = suggestion


@dataclass
class ToolDef:
    name: str
    description: str
    phases: list[int]
    parameters: dict[str, Any]
    _fn: Callable[..., Coroutine[Any, Any, Any]] = field(repr=False)
    side_effect: str = "read"

    async def __call__(self, **kwargs: Any) -> Any:
        return await self._fn(**kwargs)

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def tool(
    name: str,
    description: str,
    phases: list[int],
    parameters: dict[str, Any],
    side_effect: str = "read",
) -> Callable:
    def decorator(fn: Callable) -> ToolDef:
        return ToolDef(
            name=name,
            description=description,
            phases=phases,
            parameters=parameters,
            _fn=fn,
            side_effect=side_effect,
        )

    return decorator
