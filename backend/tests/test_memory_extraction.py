from memory.extraction import (
    MemoryMerger,
    build_candidate_extraction_prompt,
    build_v3_extraction_gate_prompt,
    build_v3_extraction_gate_tool,
    build_v3_extraction_tool,
    build_v3_profile_extraction_tool,
    build_v3_working_memory_extraction_tool,
    build_extraction_prompt,
    build_v3_extraction_prompt,
    parse_candidate_extraction_response,
    parse_extraction_response,
    parse_v3_extraction_gate_tool_arguments,
    parse_v3_extraction_response,
    v3_profile_extraction_tool_name,
    v3_working_memory_extraction_tool_name,
)
from memory.models import MemoryItem, MemorySource, Rejection, UserMemory
from memory.v3_models import (
    SessionWorkingMemory,
    UserMemoryProfile,
)


def make_memory_item(**overrides):
    base = {
        "id": "item-1",
        "user_id": "u1",
        "type": "preference",
        "domain": "food",
        "key": "spicy",
        "value": "no spicy food",
        "scope": "trip",
        "polarity": "neutral",
        "confidence": 0.8,
        "status": "active",
        "source": MemorySource(kind="message", session_id="s1"),
        "created_at": "2026-04-11T00:00:00",
        "updated_at": "2026-04-11T00:00:00",
    }
    base.update(overrides)
    return MemoryItem(**base)


class TestBuildExtractionPrompt:
    def test_includes_user_messages(self):
        prompt = build_extraction_prompt(
            user_messages=["我不吃辣", "喜欢住民宿"],
            existing_memory=UserMemory(user_id="u1"),
        )
        assert "不吃辣" in prompt
        assert "住民宿" in prompt

    def test_includes_existing_memory(self):
        memory = UserMemory(
            user_id="u1",
            explicit_preferences={"住宿": "民宿"},
        )
        prompt = build_extraction_prompt(
            user_messages=["预算3万"],
            existing_memory=memory,
        )
        assert "民宿" in prompt


class TestBuildCandidateExtractionPrompt:
    def test_includes_user_messages_and_trip_rule(self):
        prompt = build_candidate_extraction_prompt(
            user_messages=["我这次去京都", "预算 3000 元"],
            existing_items=[make_memory_item()],
            plan_facts={"destination": "京都", "dates": "4 月 1 日 - 4 月 5 日"},
        )
        assert "我这次去京都" in prompt
        assert "本次目的地、日期、预算默认不是 global memory" in prompt


class TestParseExtractionResponse:
    def test_valid_json(self):
        response = '{"preferences": {"饮食": "不吃辣"}, "rejections": [{"item": "辣椒", "reason": "过敏", "permanent": true}]}'
        prefs, rejections = parse_extraction_response(response)
        assert prefs == {"饮食": "不吃辣"}
        assert len(rejections) == 1
        assert rejections[0]["item"] == "辣椒"

    def test_json_in_markdown_block(self):
        response = '```json\n{"preferences": {"节奏": "轻松"}, "rejections": []}\n```'
        prefs, rejections = parse_extraction_response(response)
        assert prefs == {"节奏": "轻松"}

    def test_invalid_json_returns_empty(self):
        prefs, rejections = parse_extraction_response("not json at all")
        assert prefs == {}
        assert rejections == []

    def test_empty_extraction(self):
        prefs, rejections = parse_extraction_response(
            '{"preferences": {}, "rejections": []}'
        )
        assert prefs == {}
        assert rejections == []


class TestParseCandidateExtractionResponse:
    def test_valid_candidate_response(self):
        response = """
        [
          {
            "type": "preference",
            "domain": "food",
            "key": "spicy",
            "value": "no spicy food",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": 0.82,
            "risk": "low",
            "evidence": "我不吃辣",
            "reason": "用户明确表达"
          }
        ]
        """
        candidates = parse_candidate_extraction_response(response)
        assert len(candidates) == 1
        assert candidates[0].domain == "food"
        assert candidates[0].key == "spicy"

    def test_fenced_json_candidate_response(self):
        response = """```json
        [
          {
            "type": "preference",
            "domain": "hotel",
            "key": "room",
            "value": "high floor",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": 0.7,
            "risk": "medium",
            "evidence": "想住高楼层",
            "reason": "用户明确表达"
          }
        ]
        ```"""
        candidates = parse_candidate_extraction_response(response)
        assert len(candidates) == 1
        assert candidates[0].domain == "hotel"

    def test_unknown_domain_maps_to_general(self):
        response = """
        [
          {
            "type": "preference",
            "domain": "unknown_domain",
            "key": "something",
            "value": "value",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": 0.5,
            "risk": "low",
            "evidence": "something",
            "reason": "用户明确表达"
          }
        ]
        """
        candidates = parse_candidate_extraction_response(response)
        assert len(candidates) == 1
        assert candidates[0].domain == "general"
        assert candidates[0].attributes["raw_domain"] == "unknown_domain"

    def test_malformed_confidence_candidate_returns_empty(self):
        response = """
        [
          {
            "type": "preference",
            "domain": "food",
            "key": "spicy",
            "value": "no spicy food",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": "high",
            "risk": "low",
            "evidence": "我不吃辣",
            "reason": "用户明确表达"
          }
        ]
        """
        assert parse_candidate_extraction_response(response) == []

    def test_missing_required_fields_candidate_returns_empty(self):
        response = """
        [
          {
            "type": "preference",
            "domain": "food",
            "key": "spicy",
            "value": "no spicy food",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": 0.8,
            "risk": "low"
          }
        ]
        """
        assert parse_candidate_extraction_response(response) == []

    def test_mixed_candidates_keeps_valid_ones(self):
        response = """
        [
          {
            "type": "preference",
            "domain": "food",
            "key": "spicy",
            "value": "no spicy food",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": "high",
            "risk": "low",
            "evidence": "我不吃辣",
            "reason": "用户明确表达"
          },
          {
            "type": "preference",
            "domain": "hotel",
            "key": "room",
            "value": "high floor",
            "scope": "trip",
            "polarity": "neutral",
            "confidence": 0.7,
            "risk": "medium",
            "evidence": "想住高楼层",
            "reason": "用户明确表达"
          }
        ]
        """
        candidates = parse_candidate_extraction_response(response)
        assert len(candidates) == 1
        assert candidates[0].domain == "hotel"
        assert candidates[0].key == "room"

    def test_invalid_json_returns_empty(self):
        assert parse_candidate_extraction_response("not json") == []

    def test_non_list_candidates_returns_empty(self):
        assert parse_candidate_extraction_response('{"domain": "food"}') == []


class TestMemoryMerger:
    def test_merge_new_preferences(self):
        existing = UserMemory(user_id="u1", explicit_preferences={"住宿": "民宿"})
        merger = MemoryMerger()
        merged = merger.merge(
            existing,
            preferences={"饮食": "不吃辣"},
            rejections=[],
        )
        assert merged.explicit_preferences == {"住宿": "民宿", "饮食": "不吃辣"}

    def test_merge_overwrites_same_key(self):
        existing = UserMemory(user_id="u1", explicit_preferences={"住宿": "酒店"})
        merger = MemoryMerger()
        merged = merger.merge(
            existing,
            preferences={"住宿": "民宿"},
            rejections=[],
        )
        assert merged.explicit_preferences["住宿"] == "民宿"

    def test_merge_deduplicates_rejections(self):
        existing = UserMemory(
            user_id="u1",
            rejections=[Rejection(item="辣椒", reason="过敏", permanent=True)],
        )
        merger = MemoryMerger()
        merged = merger.merge(
            existing,
            preferences={},
            rejections=[
                {"item": "辣椒", "reason": "过敏", "permanent": True},
                {"item": "红眼航班", "reason": "不喜欢", "permanent": True},
            ],
        )
        assert len(merged.rejections) == 2
        items = {r.item for r in merged.rejections}
        assert items == {"辣椒", "红眼航班"}


class TestBuildV3ExtractionPrompt:
    def test_prompt_separates_state_and_memory(self):
        profile = UserMemoryProfile.empty("u1")
        working = SessionWorkingMemory.empty("u1", "s1", "trip_1")
        prompt = build_v3_extraction_prompt(
            user_messages=["我不想坐红眼航班", "这次预算3万"],
            profile=profile,
            working_memory=working,
            plan_facts={"destination": "京都", "budget": 30000},
        )

        assert "preference_hypotheses" in prompt
        assert "working_memory" in prompt
        assert "京都" in prompt
        assert "红眼航班" in prompt
        assert "extract_memory_candidates" in prompt
        assert "不要输出 JSON 正文" in prompt
        assert "state_observations" not in prompt
        assert "episode_evidence" not in prompt
        assert "drop" not in prompt
        assert "所有文本字段必须为中文简体字符串" not in prompt


class TestBuildV3ExtractionTool:
    def test_top_level_schema_only_requires_written_outputs(self):
        tool = build_v3_extraction_tool()

        assert tool["name"] == "extract_memory_candidates"
        assert tool["parameters"]["required"] == ["profile_updates", "working_memory"]
        assert "state_observations" not in tool["parameters"]["properties"]
        assert "episode_evidence" not in tool["parameters"]["properties"]
        assert "drop" not in tool["parameters"]["properties"]

    def test_schema_strengthens_core_field_semantics(self):
        tool = build_v3_extraction_tool()
        profile_item = tool["parameters"]["properties"]["profile_updates"]["properties"][
            "constraints"
        ]["items"]

        assert profile_item["properties"]["domain"]["enum"]
        assert profile_item["properties"]["polarity"]["enum"]
        assert profile_item["properties"]["stability"]["enum"]
        assert "scope" not in profile_item["properties"]


class TestSplitMemoryExtractionTools:
    def test_profile_tool_outputs_only_profile_updates(self):
        tool = build_v3_profile_extraction_tool()

        assert tool["name"] == "extract_profile_memory"
        properties = tool["parameters"]["properties"]
        assert list(properties.keys()) == ["profile_updates"]
        assert tool["parameters"]["required"] == ["profile_updates"]
        assert "working_memory" not in properties

    def test_working_memory_tool_outputs_only_working_memory(self):
        tool = build_v3_working_memory_extraction_tool()

        assert tool["name"] == "extract_working_memory"
        properties = tool["parameters"]["properties"]
        assert list(properties.keys()) == ["working_memory"]
        assert tool["parameters"]["required"] == ["working_memory"]
        assert "profile_updates" not in properties

    def test_split_tool_name_helpers(self):
        assert v3_profile_extraction_tool_name() == "extract_profile_memory"
        assert v3_working_memory_extraction_tool_name() == "extract_working_memory"


class TestBuildV3ExtractionGate:
    def test_gate_prompt_focuses_on_judgement_only(self):
        prompt = build_v3_extraction_gate_prompt(
            user_messages=["我不吃辣", "继续规划吧"],
            plan_facts={"destination": "京都", "budget": 30000},
        )

        assert "decide_memory_extraction" in prompt
        assert "不要输出具体 memory item" in prompt
        assert "继续规划吧" in prompt
        assert "京都" in prompt

    def test_gate_tool_requires_routes(self):
        tool = build_v3_extraction_gate_tool()

        assert tool["name"] == "decide_memory_extraction"
        assert tool["parameters"]["required"] == [
            "should_extract",
            "routes",
            "reason",
            "message",
        ]
        routes = tool["parameters"]["properties"]["routes"]
        assert routes["required"] == ["profile", "working_memory"]
        assert routes["properties"]["profile"]["type"] == "boolean"
        assert routes["properties"]["working_memory"]["type"] == "boolean"

    def test_parse_gate_tool_arguments_reads_routes(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": True,
                "routes": {"profile": True, "working_memory": False},
                "reason": "explicit_long_term_constraint",
                "message": "检测到长期旅行约束",
            }
        )

        assert result.should_extract is True
        assert result.routes.profile is True
        assert result.routes.working_memory is False
        assert result.reason == "explicit_long_term_constraint"

    def test_parse_gate_tool_arguments_supports_legacy_boolean(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": True,
                "reason": "explicit_preference_signal",
                "message": "检测到可复用偏好信号",
            }
        )

        assert result.should_extract is True
        assert result.routes.profile is True
        assert result.routes.working_memory is True

    def test_parse_gate_tool_arguments_false_clears_routes(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": False,
                "routes": {"profile": True, "working_memory": True},
                "reason": "trip_state_only",
                "message": "本轮只是当前行程事实",
            }
        )

        assert result.should_extract is False
        assert result.routes.profile is False
        assert result.routes.working_memory is False

    def test_parse_gate_tool_arguments_defaults_safely(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {"should_extract": True, "reason": "explicit_preference_signal"}
        )

        assert result.should_extract is True
        assert result.reason == "explicit_preference_signal"
        assert result.message == ""
        assert result.routes.profile is True
        assert result.routes.working_memory is True


class TestParseV3ExtractionResponse:
    def test_parse_v3_split_extraction_response(self):
        response = (
            '{"profile_updates":{'
            '"constraints":[{'
            '"domain":"flight","key":"avoid_red_eye","value":true,"polarity":"avoid",'
            '"stability":"explicit_declared","confidence":0.95,"status":"active",'
            '"context":{},"applicability":"适用于所有旅行",'
            '"recall_hints":{"keywords":["红眼航班"]},"source_refs":[]'
            '}],'
            '"rejections":[],"stable_preferences":[],"preference_hypotheses":[]'
            '},'
            '"working_memory":[],"episode_evidence":[],'
            '"state_observations":[],"drop":[]'
            "}"
        )
        result = parse_v3_extraction_response(response)
        assert result.profile_updates.constraints[0].key == "avoid_red_eye"
        assert result.profile_updates.constraints[0].recall_hints == {
            "keywords": ["红眼航班"]
        }
        assert result.episode_evidence == []
        assert result.state_observations == []
        assert result.drop == []

    def test_state_observation_does_not_become_profile_item(self):
        response = (
            '{"profile_updates":{'
            '"constraints":[],"rejections":[],"stable_preferences":[],'
            '"preference_hypotheses":[]},'
            '"working_memory":[],"episode_evidence":[],'
            '"state_observations":[{"field":"destination","value":"京都"}],"drop":[]'
            "}"
        )
        result = parse_v3_extraction_response(response)
        assert result.profile_updates.constraints == []
        assert result.state_observations[0]["field"] == "destination"

    def test_missing_optional_v3_fields_defaults_to_empty_lists(self):
        response = (
            '{"profile_updates":{"constraints":[],"rejections":[],'
            '"stable_preferences":[],"preference_hypotheses":[]},'
            '"working_memory":[]}'
        )
        result = parse_v3_extraction_response(response)
        assert result.working_memory == []
        assert result.episode_evidence == []
        assert result.state_observations == []
        assert result.drop == []

    def test_working_memory_parses_session_scope(self):
        response = (
            '{"profile_updates":{"constraints":[],"rejections":[],'
            '"stable_preferences":[],"preference_hypotheses":[]},'
            '"working_memory":[{"id":"wm_1","phase":3,"kind":"temporary_rejection",'
            '"domains":["attraction"],"content":"先别考虑迪士尼",'
            '"reason":"当前候选筛选","status":"active",'
            '"expires":{"on_session_end":true,"on_trip_change":true,'
            '"on_phase_exit":false},"created_at":"2026-04-19T00:00:00"}],'
            '"episode_evidence":[],"state_observations":[],"drop":[]'
            "}"
        )
        result = parse_v3_extraction_response(response)
        assert result.working_memory[0].kind == "temporary_rejection"
        assert result.working_memory[0].expires["on_trip_change"] is True

    def test_invalid_json_returns_empty_v3_result(self):
        result = parse_v3_extraction_response("not json at all")
        assert result.profile_updates.constraints == []
        assert result.profile_updates.rejections == []
        assert result.profile_updates.stable_preferences == []
        assert result.profile_updates.preference_hypotheses == []
        assert result.working_memory == []
        assert result.episode_evidence == []
        assert result.state_observations == []
        assert result.drop == []

    def test_fenced_json_v3_extraction(self):
        response = (
            "```json\n"
            '{"profile_updates":{"constraints":[],"rejections":[],'
            '"stable_preferences":[{"domain":"pace","key":"preferred_pace",'
            '"value":"relaxed","polarity":"prefer","stability":"pattern_observed",'
            '"confidence":0.85,"status":"active","context":{},'
            '"applicability":"适用于所有旅行","recall_hints":{},"source_refs":[]}],'
            '"preference_hypotheses":[]},'
            '"working_memory":[],"episode_evidence":[],'
            '"state_observations":[],"drop":[]}\n'
            "```"
        )
        result = parse_v3_extraction_response(response)
        assert result.profile_updates.stable_preferences[0].value == "relaxed"
