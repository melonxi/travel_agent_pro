# backend/tests/test_prompt_architecture.py
"""Tests for prompt skill-card architecture upgrade."""
import pytest

from phase.prompts import (
    GLOBAL_RED_FLAGS,
    PHASE1_PROMPT,
    PHASE_PROMPTS,
)


class TestGlobalRedFlags:
    def test_global_red_flags_exists_and_nonempty(self):
        assert len(GLOBAL_RED_FLAGS) > 100

    def test_global_red_flags_covers_state_write_discipline(self):
        assert "用户没有明确确认" in GLOBAL_RED_FLAGS

    def test_global_red_flags_covers_tool_hallucination(self):
        assert "当前可用工具列表" in GLOBAL_RED_FLAGS

    def test_global_red_flags_covers_evidence_requirement(self):
        assert "凭记忆" in GLOBAL_RED_FLAGS or "凭常识" in GLOBAL_RED_FLAGS


class TestPhase1SkillCard:
    def test_phase1_has_role_section(self):
        assert "## 角色" in PHASE1_PROMPT

    def test_phase1_has_goal_section(self):
        assert "## 目标" in PHASE1_PROMPT

    def test_phase1_has_hard_rules_section(self):
        assert "## 硬法则" in PHASE1_PROMPT

    def test_phase1_has_completion_gate(self):
        assert "## 完成 Gate" in PHASE1_PROMPT

    def test_phase1_has_red_flags(self):
        assert "## Red Flags" in PHASE1_PROMPT

    def test_phase1_has_response_discipline(self):
        """Phase 1 must constrain output focus — the core fix for Question 1."""
        assert "回复纪律" in PHASE1_PROMPT or "回复原则" in PHASE1_PROMPT

    def test_phase1_has_pressure_scenarios(self):
        assert "## 压力场景" in PHASE1_PROMPT

    def test_phase1_backward_compat_in_phase_prompts(self):
        """PHASE_PROMPTS[1] must still work for backward compatibility."""
        assert PHASE_PROMPTS[1] == PHASE1_PROMPT

    def test_phase1_still_mentions_core_tools(self):
        assert "xiaohongshu_search" in PHASE1_PROMPT
        assert "web_search" in PHASE1_PROMPT

    def test_phase1_skips_search_when_destination_confirmed(self):
        assert "不要先调" in PHASE1_PROMPT

    def test_phase1_boundary_red_flag(self):
        """Phase 1 Red Flags must warn against boundary violations (Question 7)."""
        assert "预算" in PHASE1_PROMPT
        prompt_lower = PHASE1_PROMPT.lower()
        assert "red flag" in prompt_lower or "Red Flags" in PHASE1_PROMPT
