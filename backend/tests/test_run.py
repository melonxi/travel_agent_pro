import time
from run import RunRecord, IterationProgress


def test_run_record_defaults():
    r = RunRecord(run_id="r1", session_id="s1", status="running")
    assert r.error_code is None
    assert r.finished_at is None
    assert r.started_at <= time.time()


def test_run_record_status_values():
    for status in ("running", "completed", "failed", "cancelled"):
        r = RunRecord(run_id="r1", session_id="s1", status=status)
        assert r.status == status


def test_iteration_progress_values():
    assert IterationProgress.NO_OUTPUT == "no_output"
    assert IterationProgress.PARTIAL_TEXT == "partial_text"
    assert IterationProgress.PARTIAL_TOOL_CALL == "partial_tool_call"
    assert IterationProgress.TOOLS_READ_ONLY == "tools_read_only"
    assert IterationProgress.TOOLS_WITH_WRITES == "tools_with_writes"
