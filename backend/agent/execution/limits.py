from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentLoopLimits:
    max_iterations: int = 3
    max_llm_errors: int = 1

    @classmethod
    def from_constructor_args(
        cls,
        *,
        max_iterations: int | None,
        max_retries: int | None,
        max_llm_errors: int | None,
    ) -> AgentLoopLimits:
        effective_iterations = (
            max_iterations
            if max_iterations is not None
            else max_retries
            if max_retries is not None
            else cls.max_iterations
        )
        effective_llm_errors = (
            max_llm_errors if max_llm_errors is not None else cls.max_llm_errors
        )
        if effective_iterations < 1:
            raise ValueError("max_iterations must be >= 1")
        if effective_llm_errors < 0:
            raise ValueError("max_llm_errors must be >= 0")
        return cls(
            max_iterations=effective_iterations,
            max_llm_errors=effective_llm_errors,
        )
