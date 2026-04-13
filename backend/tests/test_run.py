import time
from run import RunRecord, IterationProgress
from agent.types import Message, Role


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


def test_run_record_continue_fields():
    r = RunRecord(
        run_id="r1",
        session_id="s1",
        status="failed",
        can_continue=True,
        continuation_context={
            "type": "partial_text",
            "partial_assistant_text": "hello",
        },
    )
    assert r.can_continue is True
    assert r.continuation_context["type"] == "partial_text"


def test_run_record_continue_defaults():
    r = RunRecord(run_id="r1", session_id="s1", status="running")
    assert r.can_continue is False
    assert r.continuation_context is None


def test_message_incomplete_default():
    m = Message(role=Role.ASSISTANT, content="hello")
    assert m.incomplete is False
    assert "incomplete" not in m.to_dict()


def test_message_incomplete_true():
    m = Message(role=Role.ASSISTANT, content="partial", incomplete=True)
    assert m.incomplete is True
    d = m.to_dict()
    assert d["incomplete"] is True
