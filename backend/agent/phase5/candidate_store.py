from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class Phase5CandidateValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Phase5CandidateStore:
    root: Path | str

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
        self._validate_dayplan(expected_day, dayplan)

        run_dir = self.run_dir(session_id, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "run_id": run_id,
            "worker_id": worker_id,
            "day": expected_day,
            "attempt": attempt,
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
        self, session_id: str, run_id: str
    ) -> list[dict[str, Any]]:
        run_dir = self.run_dir(session_id, run_id)
        if not run_dir.exists():
            return []

        latest_by_day: dict[int, dict[str, Any]] = {}
        for path in sorted(run_dir.glob("day_*_attempt_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            day = int(payload["day"])
            attempt = int(payload.get("attempt", 0))
            current = latest_by_day.get(day)
            if current is None or attempt > int(current.get("attempt", 0)):
                latest_by_day[day] = payload
        return [latest_by_day[day] for day in sorted(latest_by_day)]

    def _validate_dayplan(self, expected_day: int, dayplan: dict[str, Any]) -> None:
        if not isinstance(dayplan, dict):
            raise Phase5CandidateValidationError("dayplan must be an object")

        actual_day = dayplan.get("day")
        if actual_day != expected_day:
            raise Phase5CandidateValidationError(
                f"dayplan day {actual_day!r} does not match expected day {expected_day}"
            )

        if not isinstance(dayplan.get("date"), str) or not dayplan["date"]:
            raise Phase5CandidateValidationError(
                "dayplan.date must be a non-empty string"
            )

        if not isinstance(dayplan.get("activities"), list):
            raise Phase5CandidateValidationError("dayplan.activities must be a list")


def _validate_safe_segment(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _SAFE_SEGMENT_RE.fullmatch(value):
        raise Phase5CandidateValidationError(
            f"unsafe path segment for {field_name}: {value!r}"
        )
