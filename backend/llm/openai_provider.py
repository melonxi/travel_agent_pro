# backend/llm/openai_provider.py
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import tiktoken
from openai import AsyncOpenAI
from opentelemetry import trace as otel_trace

from agent.types import Message, Role, ToolCall
from llm.types import ChunkType, LLMChunk
from telemetry.attributes import LLM_PROVIDER, LLM_MODEL, EVENT_LLM_REQUEST, EVENT_LLM_RESPONSE, truncate


class OpenAIProvider:
    def __init__(
        self, model: str = "gpt-4o", temperature: float = 0.7, max_tokens: int = 4096
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = AsyncOpenAI()

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == Role.TOOL and msg.tool_result:
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": msg.tool_result.tool_call_id,
                        "content": json.dumps(
                            {
                                "status": msg.tool_result.status,
                                "data": msg.tool_result.data,
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            elif msg.role == Role.ASSISTANT and msg.tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": json.dumps(
                                        tc.arguments, ensure_ascii=False
                                    ),
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
            else:
                result.append(
                    {
                        "role": msg.role.value,
                        "content": msg.content or "",
                    }
                )
        return result

    def _convert_tools(self, tool_defs: list[dict]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in tool_defs
        ]

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        stream: bool = True,
    ) -> AsyncIterator[LLMChunk]:
        tracer = otel_trace.get_tracer("travel-agent-pro")
        with tracer.start_as_current_span("llm.chat") as span:
            span.set_attribute(LLM_PROVIDER, "openai")
            span.set_attribute(LLM_MODEL, self.model)
            total_chars = sum(len(m.content or "") for m in messages)
            span.add_event(EVENT_LLM_REQUEST, {
                "message_count": len(messages),
                "total_chars": total_chars,
                "has_tools": tools is not None and len(tools) > 0,
            })
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": self._convert_messages(messages),
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": stream,
            }
            if tools:
                kwargs["tools"] = self._convert_tools(tools)

            if not stream:
                response = await self.client.chat.completions.create(**kwargs)
                choice = response.choices[0]
                if choice.message.content:
                    yield LLMChunk(
                        type=ChunkType.TEXT_DELTA, content=choice.message.content
                    )
                if choice.message.tool_calls:
                    for tc in choice.message.tool_calls:
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=tc.id,
                                name=tc.function.name,
                                arguments=json.loads(tc.function.arguments),
                            ),
                        )
                text_preview = truncate(choice.message.content or "", max_len=200)
                tool_names = []
                if choice.message.tool_calls:
                    tool_names = [tc.function.name for tc in choice.message.tool_calls]
                span.add_event(EVENT_LLM_RESPONSE, {
                    "text_preview": text_preview,
                    "tool_calls": json.dumps(tool_names),
                })
                yield LLMChunk(type=ChunkType.DONE)
                return

            response = await self.client.chat.completions.create(**kwargs)
            current_tool_calls: dict[int, dict] = {}
            collected_text = ""

            async for chunk in response:
                choice = chunk.choices[0] if chunk.choices else None
                if not choice:
                    continue

                delta = choice.delta
                if delta:
                    if delta.content:
                        collected_text += delta.content
                        yield LLMChunk(type=ChunkType.TEXT_DELTA, content=delta.content)

                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in current_tool_calls:
                                current_tool_calls[idx] = {
                                    "id": tc_delta.id or "",
                                    "name": "",
                                    "arguments": "",
                                }
                            entry = current_tool_calls[idx]
                            if tc_delta.id:
                                entry["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    entry["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    entry["arguments"] += tc_delta.function.arguments

                if choice.finish_reason:
                    for entry in current_tool_calls.values():
                        yield LLMChunk(
                            type=ChunkType.TOOL_CALL_START,
                            tool_call=ToolCall(
                                id=entry["id"],
                                name=entry["name"],
                                arguments=json.loads(entry["arguments"])
                                if entry["arguments"]
                                else {},
                            ),
                        )
                    tool_call_names = [
                        entry["name"] for entry in current_tool_calls.values()
                    ]
                    span.add_event(EVENT_LLM_RESPONSE, {
                        "text_preview": truncate(collected_text, max_len=200),
                        "tool_calls": json.dumps(tool_call_names),
                    })
                    yield LLMChunk(type=ChunkType.DONE)
                    return

            tool_call_names = [entry["name"] for entry in current_tool_calls.values()]
            span.add_event(EVENT_LLM_RESPONSE, {
                "text_preview": truncate(collected_text, max_len=200),
                "tool_calls": json.dumps(tool_call_names),
            })
            for entry in current_tool_calls.values():
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id=entry["id"],
                        name=entry["name"],
                        arguments=json.loads(entry["arguments"])
                        if entry["arguments"]
                        else {},
                    ),
                )
            yield LLMChunk(type=ChunkType.DONE)

    async def count_tokens(self, messages: list[Message]) -> int:
        try:
            enc = tiktoken.encoding_for_model(self.model)
        except KeyError:
            enc = tiktoken.get_encoding("cl100k_base")
        total = 0
        for msg in messages:
            total += 4  # message overhead
            if msg.content:
                total += len(enc.encode(msg.content))
        return total

    # Known context windows for common models — used as fallback when
    # the /v1/models endpoint is unavailable (e.g. behind One API proxy).
    _KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
        "gpt-4o": 128000,
        "gpt-4o-mini": 128000,
        "gpt-4-turbo": 128000,
        "gpt-4-1": 1047576,
        "gpt-4.1": 1047576,
        "gpt-4": 8192,
        "gpt-3.5-turbo": 16385,
        "o1": 200000,
        "o1-mini": 128000,
        "o1-pro": 200000,
        "o3": 200000,
        "o3-mini": 200000,
        "o4-mini": 200000,
        "deepseek-chat": 65536,
        "deepseek-coder": 65536,
        "deepseek-r1": 65536,
        "deepseek-v3": 65536,
        "qwen-turbo": 131072,
        "qwen-plus": 131072,
        "qwen-max": 131072,
        "qwen-long": 1000000,
        "glm-4": 128000,
        "glm-4-plus": 128000,
        "gemini-2.5-pro": 1048576,
        "gemini-2.5-flash": 1048576,
        "gemini-2.0-flash": 1048576,
        "gemini-1.5-pro": 2097152,
        "gemini-1.5-flash": 1048576,
    }

    async def get_context_window(self) -> int | None:
        """Query the model's context window.

        Strategy:
        1. Try /v1/models API (works with OpenAI, vLLM, OpenRouter, etc.)
        2. Fall back to built-in model registry (prefix match)
        3. Return None if unknown — caller uses config default
        """
        # Strategy 1: API query
        try:
            model_info = await self.client.models.retrieve(self.model)
            if hasattr(model_info, "model_dump"):
                raw = model_info.model_dump()
            elif isinstance(model_info, dict):
                raw = model_info
            else:
                raw = {}
            for key in ("context_window", "max_model_len", "context_length", "max_context_length"):
                value = raw.get(key)
                if isinstance(value, int) and value > 0:
                    return value
        except Exception:
            pass

        # Strategy 2: known model registry (prefix match)
        model_lower = self.model.lower()
        for prefix, window in self._KNOWN_CONTEXT_WINDOWS.items():
            if model_lower.startswith(prefix):
                return window

        return None
