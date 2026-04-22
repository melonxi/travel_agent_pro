from memory.extraction import (
    build_v3_extraction_gate_prompt,
    build_v3_extraction_gate_tool,
    build_v3_extraction_tool,
    build_v3_profile_extraction_prompt,
    build_v3_profile_extraction_tool,
    build_v3_working_memory_extraction_prompt,
    build_v3_working_memory_extraction_tool,
    build_v3_extraction_prompt,
    parse_v3_extraction_gate_tool_arguments,
    parse_v3_extraction_response,
    parse_v3_profile_extraction_tool_arguments,
    parse_v3_working_memory_extraction_tool_arguments,
    v3_profile_extraction_tool_name,
    v3_working_memory_extraction_tool_name,
)
from memory.v3_models import (
    SessionWorkingMemory,
    UserMemoryProfile,
)


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
    def test_profile_item_schema_requires_recall_ready_fields(self):
        tool = build_v3_profile_extraction_tool()
        profile_item = tool["parameters"]["properties"]["profile_updates"]["properties"][
            "constraints"
        ]["items"]

        assert "applicability" in profile_item["properties"]
        assert "recall_hints" in profile_item["properties"]
        assert "source_refs" in profile_item["properties"]
        assert {"applicability", "recall_hints", "source_refs"}.issubset(
            set(profile_item["required"])
        )

        recall_hints = profile_item["properties"]["recall_hints"]
        assert recall_hints["type"] == "object"
        assert set(recall_hints["required"]) == {"domains", "keywords", "aliases"}
        assert recall_hints["properties"]["domains"]["type"] == "array"
        assert recall_hints["properties"]["keywords"]["type"] == "array"
        assert recall_hints["properties"]["aliases"]["type"] == "array"

        source_refs = profile_item["properties"]["source_refs"]
        assert source_refs["type"] == "array"
        source_ref_item = source_refs["items"]
        assert source_ref_item["type"] == "object"
        assert set(source_ref_item["required"]) == {"kind", "session_id", "quote"}

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

    def test_profile_prompt_excludes_working_memory_target(self):
        prompt = build_v3_profile_extraction_prompt(
            user_messages=["以后我都不坐红眼航班"],
            profile=UserMemoryProfile.empty("u1"),
            plan_facts={"destination": "京都"},
        )

        assert "extract_profile_memory" in prompt
        assert "profile_updates" in prompt
        assert "working_memory" not in prompt
        assert "本次目的地、日期、预算" in prompt

    def test_profile_prompt_requires_recall_ready_metadata(self):
        prompt = build_v3_profile_extraction_prompt(
            user_messages=["以后我都不坐红眼航班"],
            profile=UserMemoryProfile.empty("u1"),
            plan_facts={"destination": "京都"},
        )

        assert "applicability" in prompt
        assert "recall_hints" in prompt
        assert "domains" in prompt
        assert "keywords" in prompt
        assert "aliases" in prompt
        assert "source_refs" in prompt
        assert "当前轮" in prompt
        assert "quote" in prompt
        assert "敏感信息" in prompt

    def test_working_prompt_excludes_profile_updates_target(self):
        prompt = build_v3_working_memory_extraction_prompt(
            user_messages=["这轮先别考虑迪士尼"],
            working_memory=SessionWorkingMemory.empty("u1", "s1", "trip_1"),
            plan_facts={"destination": "东京"},
        )

        assert "extract_working_memory" in prompt
        assert "working_memory" in prompt
        assert "profile_updates" not in prompt
        assert "长期偏好" in prompt

    def test_parse_profile_tool_arguments(self):
        result = parse_v3_profile_extraction_tool_arguments(
            {
                "profile_updates": {
                    "constraints": [
                        {
                            "domain": "flight",
                            "key": "avoid_red_eye",
                            "value": True,
                            "polarity": "avoid",
                            "stability": "explicit_declared",
                            "confidence": 0.95,
                            "reason": "明确表达",
                            "evidence": "以后不坐红眼航班",
                        }
                    ],
                    "rejections": [],
                    "stable_preferences": [],
                    "preference_hypotheses": [],
                }
            }
        )

        assert result.profile_updates.constraints[0].key == "avoid_red_eye"
        assert result.working_memory == []

    def test_parse_working_memory_tool_arguments(self):
        result = parse_v3_working_memory_extraction_tool_arguments(
            {
                "working_memory": [
                    {
                        "phase": 3,
                        "kind": "temporary_rejection",
                        "domains": ["attraction"],
                        "content": "这轮先别考虑迪士尼",
                        "reason": "当前候选筛选需要避让",
                        "status": "active",
                        "expires": {
                            "on_session_end": True,
                            "on_trip_change": True,
                            "on_phase_exit": False,
                        },
                    }
                ]
            }
        )

        assert result.profile_updates.constraints == []
        assert result.working_memory[0].kind == "temporary_rejection"


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

    def test_parse_extraction_gate_requires_routes_object(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": True,
                "reason": "legacy_bool_payload",
                "message": "旧格式",
            }
        )

        assert result.should_extract is False
        assert result.routes.profile is False
        assert result.routes.working_memory is False
        assert result.reason == "invalid_route_payload"

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

        assert result.should_extract is False
        assert result.reason == "invalid_route_payload"
        assert result.message == ""
        assert result.routes.profile is False
        assert result.routes.working_memory is False


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
