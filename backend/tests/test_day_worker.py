# backend/tests/test_day_worker.py
import json
import pytest

from agent.day_worker import extract_dayplan_json, DayWorkerResult


def test_extract_dayplan_json_from_code_block():
    text = """我来为你规划第 3 天的行程。

```json
{
  "day": 3,
  "date": "2026-05-03",
  "notes": "浅草-上野文化区",
  "activities": [
    {
      "name": "浅草寺",
      "location": {"name": "浅草寺", "lat": 35.7148, "lng": 139.7967},
      "start_time": "09:00",
      "end_time": "10:30",
      "category": "shrine",
      "cost": 0,
      "transport_from_prev": "地铁",
      "transport_duration_min": 20,
      "notes": ""
    }
  ]
}
```"""
    result = extract_dayplan_json(text)
    assert result is not None
    assert result["day"] == 3
    assert len(result["activities"]) == 1
    assert result["activities"][0]["name"] == "浅草寺"


def test_extract_dayplan_json_bare_json():
    """Worker 可能直接输出 JSON 不带代码块。"""
    data = {
        "day": 1,
        "date": "2026-05-01",
        "notes": "",
        "activities": [],
    }
    text = json.dumps(data, ensure_ascii=False)
    result = extract_dayplan_json(text)
    assert result is not None
    assert result["day"] == 1


def test_extract_dayplan_json_no_json():
    text = "我正在规划行程，请稍等..."
    result = extract_dayplan_json(text)
    assert result is None


def test_day_worker_result_success():
    r = DayWorkerResult(
        day=1,
        date="2026-05-01",
        success=True,
        dayplan={"day": 1, "date": "2026-05-01", "activities": []},
        error=None,
    )
    assert r.success is True
    assert r.dayplan is not None


def test_day_worker_result_failure():
    r = DayWorkerResult(
        day=2,
        date="2026-05-02",
        success=False,
        dayplan=None,
        error="LLM timeout",
    )
    assert r.success is False
    assert "timeout" in r.error
