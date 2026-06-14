from __future__ import annotations

import inspect
import json
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.trace_models import TraceEvent
from storage.trace_redaction import (
    REDACTION_STATUS_DISABLED,
    REDACTION_STATUS_HASH_ONLY,
    RedactionResult,
    canonical_json,
    redact_for_trace,
    stable_content_hash,
)

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TraceRecorderConfig:
    enabled: bool = True
    persist_artifacts: bool = True
    redact_pii: bool = False
    payload_schema_version: int = 2


@dataclass(frozen=True)
class TraceContext:
    run_id: str
    session_id: str
    trip_id: str | None = None
    context_epoch: int | None = None
    phase: int | None = None
    phase2_step: str | None = None
    parent_event_id: str | None = None
    root_event_id: str | None = None
    correlation_id: str | None = None
    actor: str = "main_agent"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceArtifactMetadata:
    artifact_id: str
    run_id: str
    event_id: str | None
    kind: str
    content_type: str
    content_hash: str
    redaction_status: str
    storage_path: str | None
    size_bytes: int
    created_at: str


def _safe_path_segment(value: str | None) -> str:
    text = str(value or "unknown")
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in text)
    return safe.strip("._")[:120] or "unknown"


class LocalTraceArtifactStore:
    def __init__(
        self,
        base_dir: str | Path = "backend/data/trace_artifacts",
        *,
        relative_to: str | Path | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.relative_to = Path(relative_to) if relative_to is not None else None

    def save(
        self,
        *,
        run_id: str,
        session_id: str,
        artifact_id: str,
        event_id: str | None,
        kind: str,
        content: Any,
        content_type: str,
        content_hash: str,
    ) -> str:
        del event_id, content_hash
        extension = self._extension(content_type, content)
        directory = self.base_dir / _safe_path_segment(session_id) / _safe_path_segment(run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_path_segment(artifact_id)}{extension}"
        path.write_bytes(self._encode_content(content, content_type))
        if self.relative_to is None:
            return str(path)
        try:
            return str(path.relative_to(self.relative_to))
        except ValueError:
            return str(path)

    @staticmethod
    def _extension(content_type: str, content: Any) -> str:
        if content_type == "application/json" or not isinstance(content, (str, bytes)):
            return ".json"
        if content_type in {"text/markdown", "text/x-markdown"}:
            return ".md"
        if content_type.startswith("text/"):
            return ".txt"
        return ".bin"

    @staticmethod
    def _encode_content(content: Any, content_type: str) -> bytes:
        if isinstance(content, bytes):
            return content
        if content_type == "application/json" or not isinstance(content, str):
            return canonical_json(content).encode("utf-8")
        return content.encode("utf-8")


class TraceRecorder:
    def __init__(
        self,
        *,
        trace_store: Any | None,
        redactor: Any = redact_for_trace,
        artifact_store: Any | None = None,
        config: TraceRecorderConfig | None = None,
    ) -> None:
        self.trace_store = trace_store
        self.redactor = redactor
        self.artifact_store = artifact_store
        self.config = config or TraceRecorderConfig()
        self._sequences: dict[str, int] = {}
        self._sequence_lock = threading.Lock()

    def next_sequence(self, run_id: str) -> int:
        with self._sequence_lock:
            next_value = self._sequences.get(run_id, 0) + 1
            self._sequences[run_id] = next_value
            return next_value

    def child_context(
        self,
        context: TraceContext,
        *,
        parent_event: TraceEvent | None = None,
        parent_event_id: str | None = None,
        actor: str | None = None,
        correlation_id: str | None = None,
    ) -> TraceContext:
        resolved_parent_id = (
            parent_event.event_id if parent_event is not None else parent_event_id
        )
        root_event_id = None
        if parent_event is not None:
            root_event_id = parent_event.root_event_id or parent_event.event_id
        elif context.root_event_id:
            root_event_id = context.root_event_id
        elif resolved_parent_id:
            root_event_id = resolved_parent_id

        return TraceContext(
            run_id=context.run_id,
            session_id=context.session_id,
            trip_id=context.trip_id,
            context_epoch=context.context_epoch,
            phase=context.phase,
            phase2_step=context.phase2_step,
            parent_event_id=resolved_parent_id,
            root_event_id=root_event_id,
            correlation_id=correlation_id or context.correlation_id,
            actor=actor or context.actor,
            metadata=dict(context.metadata),
        )

    async def start_run(
        self,
        context: TraceContext,
        *,
        started_at: str | None = None,
        status: str = "running",
        payload: dict[str, Any] | None = None,
        config_hash: str | None = None,
        prompt_version: str | None = None,
        model_config: dict[str, Any] | None = None,
        tool_schema_hash: str | None = None,
    ) -> TraceEvent | None:
        if not self.config.enabled:
            return None
        started = started_at or _now_iso()
        model_config_json = (
            json.dumps(
                self._redact(model_config).value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if model_config is not None
            else None
        )
        await self._safe_store_call(
            "create_run",
            run_id=context.run_id,
            session_id=context.session_id,
            trip_id=context.trip_id,
            context_epoch=context.context_epoch,
            started_at=started,
            status=status,
            config_hash=config_hash,
            prompt_version=prompt_version,
            model_config_json=model_config_json,
            tool_schema_hash=tool_schema_hash,
            trace_schema_version=2,
        )
        return await self.emit_event(
            context,
            event_type="run_start",
            status=status,
            payload=payload or {},
            started_at=started,
            ended_at=started,
        )

    async def end_run(
        self,
        context: TraceContext,
        *,
        status: str,
        final_phase: int | None = None,
        final_phase2_step: str | None = None,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_cost_usd: float = 0.0,
        total_duration_ms: float = 0.0,
        ended_at: str | None = None,
        payload: dict[str, Any] | None = None,
        update_summary: bool = True,
    ) -> TraceEvent | None:
        if not self.config.enabled:
            return None
        ended = ended_at or _now_iso()
        if update_summary:
            await self._safe_store_call(
                "update_run_summary",
                run_id=context.run_id,
                ended_at=ended,
                status=status,
                final_phase=final_phase if final_phase is not None else context.phase,
                final_phase2_step=(
                    final_phase2_step
                    if final_phase2_step is not None
                    else context.phase2_step
                ),
                total_input_tokens=total_input_tokens,
                total_output_tokens=total_output_tokens,
                total_cost_usd=total_cost_usd,
                total_duration_ms=total_duration_ms,
            )
        return await self.emit_event(
            context,
            event_type="run_end",
            status=status,
            payload=payload or {},
            started_at=ended,
            ended_at=ended,
        )

    async def emit_event(
        self,
        context: TraceContext,
        *,
        event_type: str,
        payload: dict[str, Any] | None = None,
        phase: int | None = None,
        phase2_step: str | None = None,
        iteration: int | None = None,
        tool_name: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        status: str | None = None,
        duration_ms: float | None = None,
        cost_usd: float | None = None,
        actor: str | None = None,
        parent_event_id: str | None = None,
        root_event_id: str | None = None,
        correlation_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> TraceEvent | None:
        if not self.config.enabled:
            return None
        event = self._build_event(
            context,
            event_type=event_type,
            payload=payload or {},
            phase=phase,
            phase2_step=phase2_step,
            iteration=iteration,
            tool_name=tool_name,
            llm_provider=llm_provider,
            llm_model=llm_model,
            status=status,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            actor=actor,
            parent_event_id=parent_event_id,
            root_event_id=root_event_id,
            correlation_id=correlation_id,
            started_at=started_at,
            ended_at=ended_at,
        )
        await self._persist_event(event)
        return event

    async def attach_artifact(
        self,
        context: TraceContext,
        *,
        event_id: str | None,
        kind: str,
        content: Any,
        content_type: str = "application/json",
    ) -> TraceArtifactMetadata | None:
        if not self.config.enabled:
            return None

        redacted = self._redact(content)
        redacted_value = redacted.value
        content_hash = stable_content_hash(redacted_value)
        artifact_id = f"artifact_{uuid.uuid4().hex}"
        storage_path: str | None = None
        size_bytes = len(
            LocalTraceArtifactStore._encode_content(redacted_value, content_type)
        )
        redaction_status = redacted.redaction_status

        if self.config.persist_artifacts and self.artifact_store is not None:
            storage_path = await self._safe_artifact_write(
                context=context,
                artifact_id=artifact_id,
                event_id=event_id,
                kind=kind,
                content=redacted_value,
                content_type=content_type,
                content_hash=content_hash,
            )
            if storage_path is None:
                redaction_status = REDACTION_STATUS_HASH_ONLY
        elif self.config.persist_artifacts:
            redaction_status = REDACTION_STATUS_HASH_ONLY
        elif not self.config.persist_artifacts:
            redaction_status = REDACTION_STATUS_DISABLED

        metadata = TraceArtifactMetadata(
            artifact_id=artifact_id,
            run_id=context.run_id,
            event_id=event_id,
            kind=kind,
            content_type=content_type,
            content_hash=content_hash,
            redaction_status=redaction_status,
            storage_path=storage_path,
            size_bytes=size_bytes,
            created_at=_now_iso(),
        )
        await self._safe_store_call("save_artifact_metadata", metadata)
        return metadata

    def _build_event(
        self,
        context: TraceContext,
        *,
        event_type: str,
        payload: dict[str, Any],
        phase: int | None = None,
        phase2_step: str | None = None,
        iteration: int | None = None,
        tool_name: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        status: str | None = None,
        duration_ms: float | None = None,
        cost_usd: float | None = None,
        actor: str | None = None,
        parent_event_id: str | None = None,
        root_event_id: str | None = None,
        correlation_id: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> TraceEvent:
        now = _now_iso()
        schema_version = self.config.payload_schema_version
        event_payload = {"schema_version": schema_version, **payload}
        resolved_parent = parent_event_id or context.parent_event_id
        resolved_root = root_event_id or context.root_event_id or resolved_parent
        return TraceEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            run_id=context.run_id,
            sequence=self.next_sequence(context.run_id),
            event_type=event_type,
            phase=phase if phase is not None else context.phase,
            phase2_step=phase2_step if phase2_step is not None else context.phase2_step,
            iteration=iteration,
            tool_name=tool_name,
            llm_provider=llm_provider,
            llm_model=llm_model,
            status=status,
            duration_ms=duration_ms,
            cost_usd=cost_usd,
            payload=event_payload,
            created_at=now,
            session_id=context.session_id,
            trip_id=context.trip_id,
            context_epoch=context.context_epoch,
            parent_event_id=resolved_parent,
            root_event_id=resolved_root,
            correlation_id=correlation_id or context.correlation_id,
            actor=actor or context.actor,
            started_at=started_at,
            ended_at=ended_at,
            payload_schema_version=schema_version,
        )

    def _redact(self, value: Any) -> RedactionResult:
        result = self.redactor(value, redact_pii=self.config.redact_pii)
        if isinstance(result, RedactionResult):
            return result
        return RedactionResult(value=result, redaction_status="redacted", redacted_count=1)

    async def _persist_event(self, event: TraceEvent) -> None:
        await self._safe_store_call("append_event", event)

    async def _safe_store_call(self, method_name: str, *args: Any, **kwargs: Any) -> Any:
        if self.trace_store is None:
            return None
        method = getattr(self.trace_store, method_name, None)
        if method is None:
            logger.warning("trace store missing method %s", method_name)
            return None
        try:
            result = method(*args, **kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception:
            logger.warning("trace recorder store call failed: %s", method_name, exc_info=True)
            return None

    async def _safe_artifact_write(
        self,
        *,
        context: TraceContext,
        artifact_id: str,
        event_id: str | None,
        kind: str,
        content: Any,
        content_type: str,
        content_hash: str,
    ) -> str | None:
        if self.artifact_store is None:
            return None
        method = getattr(self.artifact_store, "save", None)
        if method is None:
            logger.warning("artifact store missing save method")
            return None
        try:
            result = method(
                run_id=context.run_id,
                session_id=context.session_id,
                artifact_id=artifact_id,
                event_id=event_id,
                kind=kind,
                content=content,
                content_type=content_type,
                content_hash=content_hash,
            )
            if inspect.isawaitable(result):
                result = await result
            return str(result) if result else None
        except Exception:
            logger.warning("trace artifact write failed", exc_info=True)
            return None


class NoOpTraceRecorder(TraceRecorder):
    def __init__(self) -> None:
        super().__init__(trace_store=None, config=TraceRecorderConfig(enabled=False))
