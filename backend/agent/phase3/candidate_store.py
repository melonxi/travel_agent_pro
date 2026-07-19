from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.base import ToolError
from tools.plan_tools.evidence import validate_visit_info
from tools.source_registry import SourceRegistry

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class Phase3CandidateValidationError(ValueError):
    """候选校验失败。error_code/suggestion 供 Worker 自修复循环使用。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "INVALID_DAYPLAN",
        suggestion: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.suggestion = suggestion


@dataclass(frozen=True)
class Phase3CandidateStore:
    root: Path | str
    # 注入后启用 source_ref 绑定校验（与串行写入路径同一规则集）。
    source_registry: SourceRegistry | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    def run_dir(self, session_id: str, run_id: str) -> Path:
        _validate_safe_segment(session_id, "session_id")
        _validate_safe_segment(run_id, "run_id")
        return Path(self.root) / session_id / run_id

    def submit_candidate(
        self,
        session_id: str,
        run_id: str,
        worker_id: str,
        expected_day: int,
        attempt: int,
        dayplan: dict[str, Any],
    ) -> dict[str, Any]:
        _validate_safe_segment(worker_id, "worker_id")
        self._validate_dayplan(expected_day, dayplan, session_id=session_id)

        run_dir = self.run_dir(session_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        seq = _next_candidate_sequence(run_dir, expected_day)
        payload = {
            "session_id": session_id,
            "run_id": run_id,
            "worker_id": worker_id,
            "day": expected_day,
            "attempt": attempt,
            "seq": seq,
            "status": "submitted",
            "dayplan": dayplan,
        }
        path = run_dir / f"day_{expected_day}_attempt_{attempt}.json"
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return {
            "submitted": True,
            "day": expected_day,
            "attempt": attempt,
            "path": str(path),
        }

    def load_latest_candidates(
        self,
        session_id: str,
        run_id: str,
        *,
        accepted_only: bool = False,
    ) -> list[dict[str, Any]]:
        run_dir = self.run_dir(session_id, run_id)
        if not run_dir.exists():
            return []

        latest_by_day: dict[int, dict[str, Any]] = {}
        for path in sorted(run_dir.glob("day_*_attempt_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            day = int(payload["day"])
            seq = _candidate_sequence(payload)
            current = latest_by_day.get(day)
            if current is None or seq > _candidate_sequence(current):
                latest_by_day[day] = payload
        return [
            latest_by_day[day]
            for day in sorted(latest_by_day)
            if not accepted_only or latest_by_day[day].get("status") == "accepted"
        ]

    def update_candidate_status(
        self,
        session_id: str,
        run_id: str,
        expected_day: int,
        attempt: int,
        *,
        status: str,
        reason: str | None = None,
    ) -> bool:
        """Mark a staged candidate accepted/rejected by the Orchestrator.

        Patched/fallback workers may return a DayWorkerResult without writing an
        artifact. In that case this is a no-op and the caller can use the
        in-memory result.
        """
        if status not in {"accepted", "rejected"}:
            raise Phase3CandidateValidationError(
                f"unsupported candidate status: {status!r}"
            )
        path = self.run_dir(session_id, run_id) / (
            f"day_{expected_day}_attempt_{attempt}.json"
        )
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = status
        if reason:
            payload["status_reason"] = reason
        else:
            payload.pop("status_reason", None)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return True

    def get_latest_candidate(
        self,
        session_id: str,
        run_id: str,
        expected_day: int,
    ) -> dict[str, Any] | None:
        """Return the day's latest-written candidate payload, if any."""
        run_dir = self.run_dir(session_id, run_id)
        latest: dict[str, Any] | None = None
        for path in run_dir.glob(f"day_{expected_day}_attempt_*.json"):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if latest is None or _candidate_sequence(payload) > _candidate_sequence(
                latest
            ):
                latest = payload
        return latest

    def update_latest_candidate_status(
        self,
        session_id: str,
        run_id: str,
        expected_day: int,
        *,
        status: str,
        reason: str | None = None,
    ) -> bool:
        """Invalidate/supersede the current day version before redispatch."""
        payload = self.get_latest_candidate(session_id, run_id, expected_day)
        if payload is None:
            return False
        return self.update_candidate_status(
            session_id,
            run_id,
            expected_day,
            int(payload.get("attempt", 0)),
            status=status,
            reason=reason,
        )

    def restore_candidate_as_latest(
        self,
        session_id: str,
        run_id: str,
        expected_day: int,
        attempt: int,
        *,
        reason: str | None = None,
    ) -> bool:
        """Re-accept a superseded candidate and bump it back to latest seq.

        Redispatch optimistically rejects the current accepted version before
        the replacement worker runs. When that worker fails, the orchestrator
        restores the previous version explicitly; bumping ``seq`` keeps the
        "latest write wins, then check accepted" delivery semantics intact.
        """
        run_dir = self.run_dir(session_id, run_id)
        path = run_dir / f"day_{expected_day}_attempt_{attempt}.json"
        if not path.exists():
            return False
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["seq"] = _next_candidate_sequence(run_dir, expected_day)
        payload["status"] = "accepted"
        if reason:
            payload["status_reason"] = reason
        else:
            payload.pop("status_reason", None)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
        return True

    def _validate_dayplan(
        self,
        expected_day: int,
        dayplan: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> None:
        if not isinstance(dayplan, dict):
            raise Phase3CandidateValidationError("dayplan must be an object")

        actual_day = dayplan.get("day")
        if actual_day != expected_day:
            raise Phase3CandidateValidationError(
                f"dayplan day {actual_day!r} does not match expected day {expected_day}"
            )

        if not isinstance(dayplan.get("date"), str) or not dayplan["date"]:
            raise Phase3CandidateValidationError(
                "dayplan.date must be a non-empty string"
            )

        if not isinstance(dayplan.get("activities"), list):
            raise Phase3CandidateValidationError("dayplan.activities must be a list")

        # 证据校验必须在提交时完成：单 Worker 内失败重试成本是一天；
        # 拖到 Orchestrator 最终 handoff 才发现，会烧掉整轮并行成果。
        for index, activity in enumerate(dayplan["activities"]):
            if not isinstance(activity, dict):
                continue
            if activity.get("visit_info") is None:
                continue
            try:
                validate_visit_info(
                    activity["visit_info"],
                    f"activities[{index}]",
                    source_registry=self.source_registry,
                    session_id=session_id,
                )
            except ToolError as exc:
                raise Phase3CandidateValidationError(
                    str(exc),
                    error_code="INVALID_DAYPLAN_EVIDENCE",
                    suggestion=exc.suggestion,
                ) from exc


def _validate_safe_segment(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT_RE.fullmatch(value):
        raise Phase3CandidateValidationError(
            f"unsafe path segment for {field_name}: {value!r}"
        )


def _candidate_sequence(payload: dict[str, Any]) -> int:
    """Return write order, falling back to legacy attempt-only artifacts."""
    try:
        return int(payload.get("seq", payload.get("attempt", 0)))
    except (TypeError, ValueError):
        return 0


def _next_candidate_sequence(run_dir: Path, expected_day: int) -> int:
    latest = 0
    for path in run_dir.glob(f"day_{expected_day}_attempt_*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        latest = max(latest, _candidate_sequence(payload))
    return latest + 1
