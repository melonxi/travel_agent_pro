from pathlib import Path

import pytest

from agent.phase5.candidate_store import (
    Phase5CandidateStore,
    Phase5CandidateValidationError,
)


def _dayplan(day: int = 1) -> dict:
    return {
        "day": day,
        "date": "2026-05-01",
        "notes": "候选计划",
        "activities": [],
    }


def test_submit_candidate_writes_json_artifact(tmp_path: Path):
    store = Phase5CandidateStore(tmp_path)

    result = store.submit_candidate(
        session_id="sess_1",
        run_id="run_1",
        worker_id="day_1_attempt_1",
        expected_day=1,
        attempt=1,
        dayplan=_dayplan(1),
    )

    assert result["submitted"] is True
    path = Path(result["path"])
    assert path.exists()
    loaded = store.load_latest_candidates("sess_1", "run_1")
    assert len(loaded) == 1
    assert loaded[0]["dayplan"]["day"] == 1


def test_submit_candidate_rejects_wrong_day(tmp_path: Path):
    store = Phase5CandidateStore(tmp_path)

    with pytest.raises(Phase5CandidateValidationError, match="expected day 1"):
        store.submit_candidate(
            session_id="sess_1",
            run_id="run_1",
            worker_id="day_1_attempt_1",
            expected_day=1,
            attempt=1,
            dayplan=_dayplan(2),
        )


def test_load_latest_candidates_keeps_highest_attempt_per_day(tmp_path: Path):
    store = Phase5CandidateStore(tmp_path)
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_1", 1, 1, _dayplan(1))
    latest = _dayplan(1)
    latest["notes"] = "newer"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_2", 1, 2, latest)

    loaded = store.load_latest_candidates("sess_1", "run_1")

    assert len(loaded) == 1
    assert loaded[0]["attempt"] == 2
    assert loaded[0]["dayplan"]["notes"] == "newer"


@pytest.mark.parametrize(
    ("session_id", "run_id", "worker_id"),
    [
        ("../evil", "run_1", "day_1_attempt_1"),
        ("sess_1", "run/evil", "day_1_attempt_1"),
        ("sess_1", "run_1", "../worker"),
        ("", "run_1", "day_1_attempt_1"),
        ("sess.1", "run_1", "day_1_attempt_1"),
    ],
)
def test_submit_candidate_rejects_unsafe_path_segments(
    tmp_path: Path,
    session_id: str,
    run_id: str,
    worker_id: str,
):
    store = Phase5CandidateStore(tmp_path)

    with pytest.raises(Phase5CandidateValidationError, match="unsafe path segment"):
        store.submit_candidate(
            session_id=session_id,
            run_id=run_id,
            worker_id=worker_id,
            expected_day=1,
            attempt=1,
            dayplan=_dayplan(1),
        )
