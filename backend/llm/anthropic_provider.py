# backend/llm/anthropic_provider.py
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic
from opentelemetry import trace as otel_trace

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import (
    EVENT_LLM_REQUEST,
    EVENT_LLM_RESPONSE,
    LLM_MODEL,
    LLM_PROVIDER,
    truncate,
)


class AnthropicProvider:
    _RETRY_DELAYS = (1.0, 3.0)

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncAnthropic()

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def _classify_error(
        self, exc: Exception, *, failure_phase: str = "connection"
    ) -> "LLMError":
        import anthropic
        from llm.errors import (
            LLMError,
            LLMErrorCode,
            classify_by_http_status,
            classify_opaque_api_error,
        )

        if isinstance(exc, LLMError):
            return exc
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMError(
                code=LLMErrorCode.TRANSIENT,
                message=str(exc),
                retryable=True,
                provider="anthropic",
                model=self.model,
                failure_phase="connection",
                raw_error=repr(exc),
            )
        if isinstance(exc, anthropic.APIStatusError):
            err = classify_by_http_status(
                exc.status_code,
                provider="anthropic",
                model=self.model,
                raw_error=str(exc),
            )
            if exc.status_code == 429:
                retry_after_header = getattr(exc.response, "headers", {}).get(
                    "retry-after"
                )
                if retry_after_header:
                    try:
                        err.retry_after = float(retry_after_header)
                    except (ValueError, TypeError):
                        pass
            return err
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return LLMError(
                code=LLMErrorCode.TRANSIENT,
                message=str(exc),
                retryable=True,
                provider="anthropic",
                model=self.model,
                failure_phase="connection",
                raw_error=repr(exc),
            )
        if isinstance(exc, json.JSONDecodeError):
            return LLMError(
                code=LLMErrorCode.PROTOCOL_ERROR,
                message="Failed to parse LLM response JSON",
                retryable=False,
                provider="anthropic",
                model=self.model,
                failure_phase="parsing",
                raw_error=repr(exc),
            )
        return classify_opaque_api_error(
            exc,
            provider="anthropic",
            model=self.model,
            failure_phase=failure_phase,
        )

    def _summarize_converted_message(self, message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content")
        summary: dict[str, Any] = {"role": message.get("role")}
        if isinstance(content, str):
            summary["content_kind"] = "text"
            summary["content_length"] = len(content)
            summary["content_preview"] = truncate(content, max_len=120)
            return summary

        if isinstance(content, list):
            blocks: list[dict[str, Any]] = []
            for block in content:
                block_summary = {"type": block.get("type")}
                if block.get("type") == "text":
                    text = block.get("text", "")
                    block_summary["length"] = len(text)
                    block_summary["preview"] = truncate(text, max_len=120)
                elif block.get("type") == "tool_use":
                    block_summary["id"] = block.get("id")
                    block_summary["name"] = block.get("name")
                    block_summary["input_keys"] = sorted(
                        (block.get("input") or {}).keys()
                    )
                elif block.get("type") == "tool_result":
                    tool_content = block.get("content", "")
                    block_summary["tool_use_id"] = block.get("tool_use_id")
                    block_summary["content_length"] = len(tool_content)
                    block_summary["preview"] = truncate(tool_content, max_len=120)
                blocks.append(block_summary)
            summary["content_kind"] = "blocks"
            summary["blocks"] = blocks
            return summary

        summary["content_kind"] = type(content).__name__
        summary["content_repr"] = truncate(repr(content), max_len=120)
        return summary

    def _write_debug_log(self, event: str, payload: dict[str, Any]) -> None:
        log_path = os.environ.get("ANTHROPIC_DEBUG_LOG")
        if not log_path:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "model": self.model,
            **payload,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _split_system_and_convert(
        self, messages: list[Message]
    ) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        converted: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == Role.SYSTEM:
                system_parts.append(msg.content or "")
            elif msg.role == Role.TOOL and msg.tool_result:
                converted.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.tool_result.tool_call_id,
                                "content": json.dumps(
                                    {
                                        "status": msg.tool_result.status,
                                        "data": msg.tool_result.data,
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                )
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                content: list[dict] = []
                if msg.content:
                    content.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )
                converted.append({"role": "assistant", "content": content})
            else:
                converted.append(
                    {
                        "role": msg.role.value,
                        "content": msg.content or "",
                    }
                )

        return "\n\n".join(system_parts), converted

    def _convert_tools(self, tool_defs: list[dict]) -> list[dict[str, Any]]:
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["parameters"],
            }
            for t in tool_defs
        ]

    def _convert_tool_choice(self, tool_choice: dict | str) -> dict | str:
        """Convert OpenAI tool_choice format to Anthropic format."""
        if isinstance(tool_choice, str):
            if tool_choice == "auto":
                return {"type": "auto"}
            if tool_choice == "none":
                return {}
            if tool_choice == "required":
                return {"type": "any"}
            return tool_choice
        if isinstance(tool_choice, dict):
            if tool_choice.get("type") == "function":
                fn = tool_choice.get("function", {})
                name = fn.get("name")
                if isinstance(name, str) and name:
                    return {"type": "tool", "name": name}
            if tool_choice.get("type") == "none":
                return {}
            if tool_choice.get("type") in {"auto", "any", "tool"}:
                return tool_choice
        return tool_choice

    async def _emit_nonstream_response(
        self,
        response: Any,
        *,
        span: Any,
    ) -> AsyncIterator[LLMChunk]:
        collected_text = ""
        tool_names: list[str] = []
        for block in response.content:
            if block.type == "text":
                collected_text += block.text
                yield LLMChunk(type=ChunkType.TEXT_DELTA, content=block.text)
            elif block.type == "tool_use":
                tool_names.append(block.name)
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    ),
                )
        span.add_event(
            EVENT_LLM_RESPONSE,
            {
                "text_preview": truncate(collected_text, max_len=200),
                "tool_calls": json.dumps(tool_names),
            },
        )
        if hasattr(response, "usage") and response.usage:
            yield LLMChunk(
                type=ChunkType.USAGE,
                usage_info={
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                },
            )
        yield LLMChunk(type=ChunkType.DONE)

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
        tool_choice: dict | str | None = None,
    ) -> AsyncIterator[LLMChunk]:
        tracer = otel_trace.get_tracer("travel-agent-pro")
        span = tracer.start_span("llm.chat")
        try:
            span.set_attribute(LLM_PROVIDER, "anthropic")
            span.set_attribute(LLM_MODEL, self.model)
            total_chars = sum(len(m.content or "") for m in messages)
            span.add_event(
                EVENT_LLM_REQUEST,
                {
                    "message_count": len(messages),
                    "total_chars": total_chars,
                    "has_tools": tools is not None and len(tools) > 0,
                },
            )
            system, converted = self._split_system_and_convert(messages)
            kwargs: dict[str, Any] = {
                "model": self.model,
                "system": system,
                "messages": converted,
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            if tools:
                kwargs["tools"] = self._convert_tools(tools)
            if tools and tool_choice is not None:
                converted_choice = self._convert_tool_choice(tool_choice)
                if converted_choice:
                    kwargs["tool_choice"] = converted_choice

            self._write_debug_log(
                "request",
                {
                    "stream": stream,
                    "used_nonstream_fallback": (not stream) or bool(tools),
                    "system_length": len(system),
                    "message_count": len(converted),
                    "messages": [
                        self._summarize_converted_message(message)
                        for message in converted
                    ],
                    "tool_count": len(kwargs.get("tools", [])),
                    "tool_names": [tool["name"] for tool in kwargs.get("tools", [])],
                },
            )

            # Anthropic streaming tool-use currently hits an SDK event accumulation
            # bug in our runtime. When tools are available, fall back to a
            # non-streaming completion and re-emit chunks in our internal format.
            import asyncio as _asyncio
            from llm.errors import LLMError

            _has_yielded = False
            max_conn_retries = 2
            for _attempt in range(max_conn_retries + 1):
                try:
                    if not stream or tools:
                        response = await self.client.messages.create(**kwargs)
                        async for chunk in self._emit_nonstream_response(
                            response,
                            span=span,
                        ):
                            yield chunk
                        return

                    async with self.client.messages.stream(**kwargs) as stream_resp:
                        current_tool_id: str | None = None
                        current_tool_name: str | None = None
                        current_tool_json: str = ""
                        collected_text = ""
                        tool_call_names: list[str] = []

                        async for event in stream_resp:
                            if event.type == "content_block_start":
                                if hasattr(event.content_block, "type"):
                                    if event.content_block.type == "tool_use":
                                        current_tool_id = event.content_block.id
                                        current_tool_name = event.content_block.name
                                        current_tool_json = ""
                            elif event.type == "content_block_delta":
                                if hasattr(event.delta, "text"):
                                    collected_text += event.delta.text
                                    _has_yielded = True
                                    yield LLMChunk(
                                        type=ChunkType.TEXT_DELTA,
                                        content=event.delta.text,
                                    )
                                elif hasattr(event.delta, "partial_json"):
                                    current_tool_json += event.delta.partial_json
                            elif event.type == "content_block_stop":
                                if current_tool_id and current_tool_name:
                                    tool_call_names.append(current_tool_name)
                                    yield LLMChunk(
                                        type=ChunkType.TOOL_CALL_START,
                                        tool_call=ToolCall(
                                            id=current_tool_id,
                                            name=current_tool_name,
                                            arguments=json.loads(current_tool_json)
                                            if current_tool_json
                                            else {},
                                        ),
                                    )
                                    current_tool_id = None
                                    current_tool_name = None
                            elif event.type == "message_stop":
                                try:
                                    final_msg = await stream_resp.get_final_message()
                                    if hasattr(final_msg, "usage") and final_msg.usage:
                                        yield LLMChunk(
                                            type=ChunkType.USAGE,
                                            usage_info={
                                                "input_tokens": final_msg.usage.input_tokens,
                                                "output_tokens": final_msg.usage.output_tokens,
                                            },
                                        )
                                except Exception:
                                    pass
                                span.add_event(
                                    EVENT_LLM_RESPONSE,
                                    {
                                        "text_preview": truncate(
                                            collected_text, max_len=200
                                        ),
                                        "tool_calls": json.dumps(tool_call_names),
                                    },
                                )
                                yield LLMChunk(type=ChunkType.DONE)
                    return  # stream completed normally
                except LLMError:
                    raise  # already classified, re-raise
                except Exception as exc:
                    self._write_debug_log(
                        "error",
                        {
                            "stream": stream,
                            "used_nonstream_fallback": (not stream) or bool(tools),
                            "message_count": len(converted),
                            "tool_count": len(kwargs.get("tools", [])),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
                    llm_err = self._classify_error(exc)
                    if (
                        llm_err.failure_phase == "connection"
                        and llm_err.retryable
                        and _attempt < max_conn_retries
                        and not _has_yielded
                    ):
                        delay = self._RETRY_DELAYS[_attempt]
                        await _asyncio.sleep(delay)
                        continue
                    raise llm_err
        finally:
            span.end()

    async def count_tokens(self, messages: list[Message]) -> int:
        total = 0
        for msg in messages:
            if msg.content:
                total += len(msg.content) // 3  # rough estimate for Claude
        return total

    async def get_context_window(self) -> int | None:
        """Return context window for known Anthropic models."""
        _KNOWN_CONTEXT_WINDOWS = {
            "claude-sonnet-4": 200000,
            "claude-opus-4": 200000,
            "claude-haiku-4": 200000,
            "claude-3-5-sonnet": 200000,
            "claude-3-5-haiku": 200000,
            "claude-3-opus": 200000,
            "claude-3-sonnet": 200000,
            "claude-3-haiku": 200000,
        }
        for prefix, window in _KNOWN_CONTEXT_WINDOWS.items():
            if self.model.startswith(prefix):
                return window
        return None
