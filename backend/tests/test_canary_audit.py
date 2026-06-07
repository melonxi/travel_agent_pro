from __future__ import annotations

import json

from evals.canary_audit import audit_events


def _row(
    sequence: int,
    tool_name: str | None,
    *,
    status: str = "success",
    error_code: str | None = None,
    event_type: str = "tool_call",
) -> dict:
    """Build a trace_events row in the shape returned by trace_store.load_events
    (SELECT *): top-level tool_name/status, payload as a JSON string."""
    payload: dict = {"tool_name": tool_name}
    if error_code:
        payload["error_code"] = error_code
    return {
        "event_id": f"evt-{sequence}",
        "run_id": "run-1",
        "sequence": sequence,
        "event_type": event_type,
        "phase": 2,
        "phase2_step": "skeleton",
        "tool_name": tool_name,
        "status": status,
        "duration_ms": 1.0,
        "payload_json": json.dumps(payload),
    }


def test_flags_forbidden_tool_in_trace():
    events = [
        _row(1, "set_trip_brief"),
        _row(2, "set_candidate_pool"),
    ]
    audit = audit_events(events, forbidden_prefixes=("set_candidate_pool",))
    assert not audit.ok
    assert "set_candidate_pool" in audit.forbidden_hits
    assert any("set_candidate_pool" in v for v in audit.violations)


def test_no_violation_when_forbidden_absent():
    events = [_row(1, "set_trip_brief"), _row(2, "update_trip_basics")]
    audit = audit_events(events, forbidden_prefixes=("set_candidate_pool", "web_search"))
    assert audit.ok
    assert audit.violations == []
    assert audit.forbidden_hits == {}


def test_forbidden_prefix_matches_family():
    events = [
        _row(1, "xiaohongshu_search_notes"),
        _row(2, "xiaohongshu_read_note"),
        _row(3, "get_poi_info"),
    ]
    audit = audit_events(events, forbidden_prefixes=("xiaohongshu_",))
    assert audit.forbidden_hits["xiaohongshu_"] == [
        "xiaohongshu_read_note",
        "xiaohongshu_search_notes",
    ]


def test_counts_all_tool_calls_ignoring_non_tool_events():
    # The Phase-3 coverage point: every tool_call row counts, llm_call rows do not.
    events = [
        _row(1, None, event_type="llm_call"),
        _row(2, "get_poi_info"),
        _row(3, "calculate_route"),
        _row(4, None, event_type="llm_call"),
        _row(5, "web_search"),
    ]
    audit = audit_events(events)
    assert audit.tool_count == 3
    assert audit.tool_calls == ["get_poi_info", "calculate_route", "web_search"]


def test_over_budget_warns_but_is_not_a_hard_violation():
    events = [_row(i, "web_search") for i in range(1, 5)]
    audit = audit_events(events, max_tool_calls=2)
    assert audit.over_budget == 2
    assert any("budget" in w for w in audit.warnings)
    assert audit.ok  # budget is soft, not a forbidden-tool violation


def test_error_stats_and_high_error_rate_from_payload_json():
    events = [_row(1, "calculate_route")] + [
        _row(i, "calculate_route", status="error", error_code="NO_ROUTE")
        for i in range(2, 11)
    ]
    audit = audit_events(events, error_rate_warn=0.5, error_rate_min_calls=3)
    stat = next(s for s in audit.error_stats if s.tool == "calculate_route")
    assert stat.total == 10
    assert stat.error == 9
    assert stat.error_codes == ("NO_ROUTE",)
    assert any("calculate_route" in w and "high_error_rate" in w for w in audit.warnings)


def test_empty_events():
    audit = audit_events([], forbidden_prefixes=("web_search",))
    assert audit.tool_count == 0
    assert audit.ok
    assert audit.error_stats == []
