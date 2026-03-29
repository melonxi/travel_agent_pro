# backend/tests/test_harness_judge.py
import json
from unittest.mock import AsyncMock

import pytest

from harness.judge import SoftScore, build_judge_prompt, parse_judge_response


def test_build_judge_prompt():
    plan_data = {"daily_plans": [{"day": 1, "activities": []}]}
    user_prefs = {"travel_style": "relaxed", "avg_pois_per_day": 3}
    prompt = build_judge_prompt(plan_data, user_prefs)
    assert "节奏舒适度" in prompt
    assert "地理效率" in prompt
    assert "relaxed" in prompt


def test_parse_valid_response():
    response = json.dumps(
        {
            "pace": 4,
            "geography": 3,
            "coherence": 5,
            "personalization": 4,
            "suggestions": ["可以考虑调整第二天的节奏"],
        }
    )
    score = parse_judge_response(response)
    assert score.pace == 4
    assert score.geography == 3
    assert score.overall == 4.0  # average
    assert len(score.suggestions) == 1


def test_parse_invalid_response_returns_default():
    score = parse_judge_response("not json at all")
    assert score.pace == 3
    assert score.overall == 3.0
    assert "评估解析失败" in score.suggestions[0]


def test_soft_score_overall():
    score = SoftScore(
        pace=5, geography=4, coherence=3, personalization=2, suggestions=[]
    )
    assert score.overall == 3.5
