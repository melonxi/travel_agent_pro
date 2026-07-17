from pathlib import Path

import pytest

from agent.phase3.candidate_store import (
    Phase3CandidateStore,
    Phase3CandidateValidationError,
)


def _dayplan(day: int = 1) -> dict:
    return {
        "day": day,
        "date": "2026-05-01",
        "notes": "候选计划",
        "activities": [],
    }


def test_submit_candidate_writes_json_artifact(tmp_path: Path):
    store = Phase3CandidateStore(tmp_path)

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
    store = Phase3CandidateStore(tmp_path)

    with pytest.raises(Phase3CandidateValidationError, match="expected day 1"):
        store.submit_candidate(
            session_id="sess_1",
            run_id="run_1",
            worker_id="day_1_attempt_1",
            expected_day=1,
            attempt=1,
            dayplan=_dayplan(2),
        )


def test_load_latest_candidates_keeps_highest_attempt_per_day(tmp_path: Path):
    store = Phase3CandidateStore(tmp_path)
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_1", 1, 1, _dayplan(1))
    latest = _dayplan(1)
    latest["notes"] = "newer"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_2", 1, 2, latest)

    loaded = store.load_latest_candidates("sess_1", "run_1")

    assert len(loaded) == 1
    assert loaded[0]["attempt"] == 2
    assert loaded[0]["dayplan"]["notes"] == "newer"


def test_accepted_only_excludes_rejected_latest_candidate(tmp_path: Path):
    store = Phase3CandidateStore(tmp_path)
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_1", 1, 1, _dayplan(1))
    store.update_candidate_status(
        "sess_1", "run_1", 1, 1, status="rejected", reason="duplicate"
    )

    assert store.load_latest_candidates("sess_1", "run_1", accepted_only=True) == []

    store.submit_candidate("sess_1", "run_1", "day_1_attempt_2", 1, 2, _dayplan(1))
    store.update_candidate_status("sess_1", "run_1", 1, 2, status="accepted")
    accepted = store.load_latest_candidates("sess_1", "run_1", accepted_only=True)
    assert [item["attempt"] for item in accepted] == [2]


def test_accepted_only_does_not_revive_older_accepted_candidate(tmp_path: Path):
    store = Phase3CandidateStore(tmp_path)
    old = _dayplan(1)
    old["notes"] = "old accepted"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_1", 1, 1, old)
    store.update_candidate_status("sess_1", "run_1", 1, 1, status="accepted")

    rejected = _dayplan(1)
    rejected["notes"] = "new rejected"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_4", 1, 4, rejected)
    store.update_candidate_status(
        "sess_1", "run_1", 1, 4, status="rejected", reason="blackboard"
    )

    assert store.load_latest_candidates("sess_1", "run_1", accepted_only=True) == []


def test_latest_candidate_uses_write_sequence_not_attempt_number(tmp_path: Path):
    store = Phase3CandidateStore(tmp_path)
    earlier = _dayplan(1)
    earlier["notes"] = "attempt 5 written first"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_5", 1, 5, earlier)
    store.update_candidate_status("sess_1", "run_1", 1, 5, status="accepted")

    later = _dayplan(1)
    later["notes"] = "attempt 4 written later"
    store.submit_candidate("sess_1", "run_1", "day_1_attempt_4", 1, 4, later)
    store.update_candidate_status("sess_1", "run_1", 1, 4, status="accepted")

    loaded = store.load_latest_candidates("sess_1", "run_1", accepted_only=True)
    assert len(loaded) == 1
    assert loaded[0]["attempt"] == 4
    assert loaded[0]["seq"] == 2
    assert loaded[0]["dayplan"]["notes"] == "attempt 4 written later"


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
    store = Phase3CandidateStore(tmp_path)

    with pytest.raises(Phase3CandidateValidationError, match="unsafe path segment"):
        store.submit_candidate(
            session_id=session_id,
            run_id=run_id,
            worker_id=worker_id,
            expected_day=1,
            attempt=1,
            dayplan=_dayplan(1),
        )
