# backend/telemetry/decorators.py
from __future__ import annotations

import asyncio
import functools
import inspect
from typing import Callable, Sequence

from opentelemetry import trace

_MODULE = "travel-agent-pro"


def traced(
    name: str | None = None,
    record_args: Sequence[str] | None = None,
) -> Callable:
    """装饰器：为函数创建 OTel span，支持 sync 和 async。"""

    def decorator(fn: Callable) -> Callable:
        span_name = name or f"{fn.__module__}.{fn.__qualname__}"
        sig = inspect.signature(fn)

        def _set_arg_attrs(span: trace.Span, args, kwargs):
            if not record_args:
                return
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            for arg_name in record_args:
                if arg_name in bound.arguments:
                    val = bound.arguments[arg_name]
                    if isinstance(val, (str, int, float, bool)):
                        span.set_attribute(f"arg.{arg_name}", val)
                    else:
                        span.set_attribute(f"arg.{arg_name}", str(val))

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                tracer = trace.get_tracer(_MODULE)
                with tracer.start_as_current_span(span_name) as span:
                    _set_arg_attrs(span, args, kwargs)
                    try:
                        return await fn(*args, **kwargs)
                    except Exception as exc:
                        span.set_status(
                            trace.Status(trace.StatusCode.ERROR, str(exc))
                        )
                        span.record_exception(exc)
                        raise

            return async_wrapper
        else:
            @functools.wraps(fn)
            def sync_wrapper(*args, **kwargs):
                tracer = trace.get_tracer(_MODULE)
                with tracer.start_as_current_span(span_name) as span:
                    _set_arg_attrs(span, args, kwargs)
                    try:
                        return fn(*args, **kwargs)
                    except Exception as exc:
                        span.set_status(
                            trace.Status(trace.StatusCode.ERROR, str(exc))
                        )
                        span.record_exception(exc)
                        raise

            return sync_wrapper

    return decorator
