import json

from agent.types import ToolResult
from api.orchestration.session.persistence import (
    deserialize_tool_result,
    serialize_tool_result,
)


def test_tool_error_result_serialization_roundtrips_repair_fields():
    result = ToolResult(
        tool_call_id="tc_1",
        status="error",
        error="POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0]",
        error_code="INVALID_VALUE",
        suggestion="请把 '淄博市博物馆' 只保留在其中一天",
    )

    serialized = serialize_tool_result(result)
    restored = deserialize_tool_result("tc_1", serialized)

    assert json.loads(serialized) == {
        "status": "error",
        "data": None,
        "error": "POI '淄博市博物馆' 重复出现在 plans[0].days[1].candidate_pois[0]",
        "error_code": "INVALID_VALUE",
        "suggestion": "请把 '淄博市博物馆' 只保留在其中一天",
    }
    assert restored == result


def test_deserialize_tool_result_keeps_legacy_data_rows_as_success():
    restored = deserialize_tool_result("tc_1", '{"results": []}')

    assert restored == ToolResult(
        tool_call_id="tc_1",
        status="success",
        data={"results": []},
    )
