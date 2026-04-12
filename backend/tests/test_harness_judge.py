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


def test_score_clamped_to_max_5():
    response = '{"pace": 10, "geography": 8, "coherence": 5, "personalization": 5, "suggestions": []}'
    score = parse_judge_response(response)
    assert score.pace == 5
    assert score.geography == 5


def test_score_clamped_to_min_1():
    response = '{"pace": -1, "geography": 0, "coherence": 1, "personalization": 1, "suggestions": []}'
    score = parse_judge_response(response)
    assert score.pace == 1
    assert score.geography == 1
    assert score.coherence == 1


def test_parse_failure_returns_default_with_warning(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="harness.judge"):
        score = parse_judge_response("this is not json at all")
    assert score.pace == 3
    assert score.overall == 3.0
    assert any("评估解析失败" in r.message for r in caplog.records)


def test_missing_fields_default_to_3():
    response = '{"pace": 4, "suggestions": ["test"]}'
    score = parse_judge_response(response)
    assert score.pace == 4
    assert score.geography == 3
    assert score.coherence == 3
    assert score.personalization == 3
