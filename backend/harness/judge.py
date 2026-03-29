# backend/harness/judge.py
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SoftScore:
    pace: int = 3
    geography: int = 3
    coherence: int = 3
    personalization: int = 3
    suggestions: list[str] = field(default_factory=list)

    @property
    def overall(self) -> float:
        return (self.pace + self.geography + self.coherence + self.personalization) / 4


def build_judge_prompt(plan_data: dict[str, Any], user_prefs: dict[str, Any]) -> str:
    return f"""评估以下旅行行程的质量，每项 1-5 分。

行程数据：
{json.dumps(plan_data, ensure_ascii=False, indent=2)}

用户偏好：
{json.dumps(user_prefs, ensure_ascii=False, indent=2)}

评分维度：
1. 节奏舒适度（pace）：每天活动量是否均衡？有没有过紧或过松的天？
2. 地理效率（geography）：同一天的景点是否地理集中？有没有不必要的来回跑？
3. 体验连贯性（coherence）：每天的主题感是否清晰？过渡是否自然？
4. 个性化程度（personalization）：是否体现了用户的偏好？

严格输出 JSON：
{{"pace": N, "geography": N, "coherence": N, "personalization": N, "suggestions": ["建议1", "建议2"]}}"""


def parse_judge_response(response: str) -> SoftScore:
    try:
        # Handle cases where LLM wraps JSON in markdown code blocks
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        return SoftScore(
            pace=int(data.get("pace", 3)),
            geography=int(data.get("geography", 3)),
            coherence=int(data.get("coherence", 3)),
            personalization=int(data.get("personalization", 3)),
            suggestions=data.get("suggestions", []),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return SoftScore(suggestions=["评估解析失败，使用默认评分"])
