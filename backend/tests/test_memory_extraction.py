import pytest

from memory.extraction import (
    MemoryMerger,
    build_extraction_prompt,
    parse_extraction_response,
)
from memory.models import Rejection, UserMemory


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
