# backend/tests/test_prompt_architecture.py
"""Tests for prompt skill-card architecture upgrade."""
from phase.prompts import (
    GLOBAL_RED_FLAGS,
    PHASE1_PROMPT,
    PHASE3_BASE_PROMPT,
    PHASE3_STEP_PROMPTS,
    PHASE5_PROMPT,
    PHASE7_PROMPT,
    PHASE_PROMPTS,
    build_phase3_prompt,
)

_LEGACY_STATE_WRITE_CALL = "update" "_plan" "_state("
_LEGACY_STATE_WRITE_FIELD_CALL = "update" "_plan" "_state(field="
_LEGACY_STATE_WRITE_TOOL = "update" "_plan" "_state"


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
        assert "xiaohongshu_search_notes" in PHASE1_PROMPT
        assert "web_search" in PHASE1_PROMPT

    def test_phase1_skips_search_when_destination_confirmed(self):
        assert "不要先调" in PHASE1_PROMPT

    def test_phase1_boundary_red_flag(self):
        """Phase 1 Red Flags must warn against boundary violations (Question 7)."""
        assert "预算" in PHASE1_PROMPT
        prompt_lower = PHASE1_PROMPT.lower()
        assert "red flag" in prompt_lower or "Red Flags" in PHASE1_PROMPT


class TestPhase3Split:
    """Phase 3 must be split into base + per-step prompts."""

    def test_base_prompt_exists(self):
        assert len(PHASE3_BASE_PROMPT) > 100

    def test_base_prompt_has_role(self):
        assert "## 角色" in PHASE3_BASE_PROMPT

    def test_base_prompt_has_state_write_discipline(self):
        assert "状态写入纪律" in PHASE3_BASE_PROMPT or "状态写入契约" in PHASE3_BASE_PROMPT

    def test_step_prompts_cover_all_steps(self):
        assert set(PHASE3_STEP_PROMPTS.keys()) == {"brief", "candidate", "skeleton", "lock"}

    def test_each_step_has_goal(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "目标" in prompt, f"step {step} missing 目标"

    def test_each_step_has_tool_strategy(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "工具" in prompt, f"step {step} missing tool strategy"

    def test_each_step_has_completion_gate(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "完成 Gate" in prompt or "完成标志" in prompt, f"step {step} missing completion gate"

    def test_each_step_has_red_flags(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "Red Flags" in prompt or "red flag" in prompt.lower(), f"step {step} missing Red Flags"

    def test_brief_has_convergence_pressure(self):
        """Brief must have convergence pressure — the fix for Question 8."""
        assert "轮" in PHASE3_STEP_PROMPTS["brief"] or "收敛" in PHASE3_STEP_PROMPTS["brief"]

    def test_skeleton_has_thinking_framework(self):
        """Skeleton must have structured thinking — the fix for Question 4."""
        assert "锚点" in PHASE3_STEP_PROMPTS["skeleton"] or "锚定" in PHASE3_STEP_PROMPTS["skeleton"]

    def test_lock_mentions_transport_timing(self):
        """Lock must address transport timing — the fix for Question 2."""
        assert "大交通" in PHASE3_STEP_PROMPTS["lock"]


class TestBuildPhase3Prompt:
    """build_phase3_prompt() must assemble base + step correctly."""

    def test_default_returns_base_plus_brief(self):
        result = build_phase3_prompt()
        assert PHASE3_BASE_PROMPT in result
        assert PHASE3_STEP_PROMPTS["brief"] in result

    def test_specific_step(self):
        for step in ("brief", "candidate", "skeleton", "lock"):
            result = build_phase3_prompt(step)
            assert PHASE3_BASE_PROMPT in result
            assert PHASE3_STEP_PROMPTS[step] in result

    def test_only_one_step_included(self):
        result = build_phase3_prompt("skeleton")
        assert PHASE3_STEP_PROMPTS["skeleton"] in result
        assert PHASE3_STEP_PROMPTS["brief"] not in result
        assert PHASE3_STEP_PROMPTS["candidate"] not in result
        assert PHASE3_STEP_PROMPTS["lock"] not in result

    def test_backward_compat_phase_prompts_3(self):
        """PHASE_PROMPTS[3] must still return a valid prompt (default brief)."""
        assert PHASE_PROMPTS[3] == build_phase3_prompt("brief")


from phase.router import PhaseRouter
from state.models import TravelPlanState


class TestPhaseRouterGetPromptForPlan:
    """get_prompt_for_plan() must use build_phase3_prompt for phase 3."""

    def _make_plan(self, phase: int, phase3_step: str = "brief") -> TravelPlanState:
        plan = TravelPlanState(session_id="test")
        plan.phase = phase
        plan.phase3_step = phase3_step
        return plan

    def test_phase1_returns_phase1_prompt(self):
        router = PhaseRouter()
        plan = self._make_plan(1)
        prompt = router.get_prompt_for_plan(plan)
        assert "目的地收敛顾问" in prompt

    def test_phase3_brief(self):
        router = PhaseRouter()
        plan = self._make_plan(3, "brief")
        prompt = router.get_prompt_for_plan(plan)
        assert "brief" in prompt.lower() or "旅行画像" in prompt
        assert "锚定不可移动项" not in prompt or "skeleton" in prompt.lower()

    def test_phase3_skeleton(self):
        router = PhaseRouter()
        plan = self._make_plan(3, "skeleton")
        prompt = router.get_prompt_for_plan(plan)
        assert "锚定不可移动项" in prompt or "骨架" in prompt

    def test_phase3_lock(self):
        router = PhaseRouter()
        plan = self._make_plan(3, "lock")
        prompt = router.get_prompt_for_plan(plan)
        assert "大交通" in prompt

    def test_phase5_returns_phase5_prompt(self):
        router = PhaseRouter()
        plan = self._make_plan(5)
        prompt = router.get_prompt_for_plan(plan)
        assert "逐日行程" in prompt or "daily_plans" in prompt

    def test_phase7_returns_phase7_prompt(self):
        router = PhaseRouter()
        plan = self._make_plan(7)
        prompt = router.get_prompt_for_plan(plan)
        assert "查漏" in prompt or "清单" in prompt

    def test_old_get_prompt_still_works(self):
        """Backward compat: get_prompt(phase) still returns a valid prompt."""
        router = PhaseRouter()
        prompt = router.get_prompt(3)
        assert len(prompt) > 100


class TestPhase5SkillCard:
    """Phase 5 must be rewritten as a skill-card with incremental generation."""

    def test_phase5_has_role(self):
        assert "## 角色" in PHASE5_PROMPT

    def test_phase5_has_goal(self):
        assert "## 目标" in PHASE5_PROMPT

    def test_phase5_has_hard_rules(self):
        assert "## 硬法则" in PHASE5_PROMPT

    def test_phase5_has_completion_gate(self):
        assert "## 完成 Gate" in PHASE5_PROMPT

    def test_phase5_has_red_flags(self):
        assert "## Red Flags" in PHASE5_PROMPT

    def test_phase5_has_incremental_strategy(self):
        """Phase 5 must use incremental generation — fix for Question 3."""
        assert "增量" in PHASE5_PROMPT or "逐天" in PHASE5_PROMPT or "按天" in PHASE5_PROMPT

    def test_phase5_has_route_planning_framing(self):
        """Phase 5 must frame as route optimization — fix for Question 5."""
        assert "路径" in PHASE5_PROMPT or "动线" in PHASE5_PROMPT or "路线" in PHASE5_PROMPT

    def test_phase5_no_batch_all_days_instruction(self):
        """Must NOT instruct to batch all days at once — the old anti-pattern."""
        assert "优先一次性用 list[dict] 提交全部天数" not in PHASE5_PROMPT

    def test_phase5_backward_compat(self):
        assert PHASE_PROMPTS[5] == PHASE5_PROMPT

    def test_phase5_has_input_gate(self):
        assert "输入 Gate" in PHASE5_PROMPT or "输入检查" in PHASE5_PROMPT or "接手" in PHASE5_PROMPT

    def test_phase5_has_tool_contract(self):
        assert "工具契约" in PHASE5_PROMPT or "工具策略" in PHASE5_PROMPT

    def test_phase5_mentions_assemble_day_plan(self):
        assert "assemble_day_plan" in PHASE5_PROMPT

    def test_phase5_mentions_calculate_route(self):
        assert "calculate_route" in PHASE5_PROMPT

    def test_phase5_has_json_structure(self):
        assert "DayPlan" in PHASE5_PROMPT or "daily_plans" in PHASE5_PROMPT

    def test_phase5_has_pressure_scenarios(self):
        assert "压力场景" in PHASE5_PROMPT or "场景" in PHASE5_PROMPT


class TestPhase7SkillCard:
    """Phase 7 must be rewritten with full skill-card structure."""

    def test_phase7_has_role(self):
        assert "## 角色" in PHASE7_PROMPT

    def test_phase7_has_goal(self):
        assert "## 目标" in PHASE7_PROMPT

    def test_phase7_has_hard_rules(self):
        assert "## 硬法则" in PHASE7_PROMPT

    def test_phase7_has_input_gate(self):
        assert "输入 Gate" in PHASE7_PROMPT or "输入检查" in PHASE7_PROMPT or "接手" in PHASE7_PROMPT

    def test_phase7_has_completion_gate(self):
        assert "## 完成 Gate" in PHASE7_PROMPT

    def test_phase7_has_red_flags(self):
        assert "## Red Flags" in PHASE7_PROMPT

    def test_phase7_has_tool_contract(self):
        assert "工具契约" in PHASE7_PROMPT or "工具策略" in PHASE7_PROMPT

    def test_phase7_mentions_check_weather(self):
        assert "check_weather" in PHASE7_PROMPT

    def test_phase7_mentions_generate_summary(self):
        assert "generate_summary" in PHASE7_PROMPT

    def test_phase7_mentions_search_travel_services(self):
        assert "search_travel_services" in PHASE7_PROMPT

    def test_phase7_backward_compat(self):
        assert PHASE_PROMPTS[7] == PHASE7_PROMPT

    def test_phase7_has_checklist_categories(self):
        assert "证件" in PHASE7_PROMPT
        assert "天气" in PHASE7_PROMPT


class TestGlobalRedFlagsInjection:
    """GLOBAL_RED_FLAGS must be injected into all phase prompts."""

    def test_phase1_includes_global_red_flags(self):
        from phase.router import PhaseRouter
        from state.models import TravelPlanState
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 1
        prompt = router.get_prompt_for_plan(plan)
        assert GLOBAL_RED_FLAGS in prompt

    def test_phase3_includes_global_red_flags(self):
        from phase.router import PhaseRouter
        from state.models import TravelPlanState
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 3
        plan.phase3_step = "brief"
        prompt = router.get_prompt_for_plan(plan)
        assert GLOBAL_RED_FLAGS in prompt

    def test_phase3_all_steps_include_global_red_flags(self):
        for step in ("brief", "candidate", "skeleton", "lock"):
            result = build_phase3_prompt(step)
            assert GLOBAL_RED_FLAGS in result, f"step {step} missing GLOBAL_RED_FLAGS"

    def test_phase5_includes_global_red_flags(self):
        from phase.router import PhaseRouter
        from state.models import TravelPlanState
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 5
        prompt = router.get_prompt_for_plan(plan)
        assert GLOBAL_RED_FLAGS in prompt

    def test_phase7_includes_global_red_flags(self):
        from phase.router import PhaseRouter
        from state.models import TravelPlanState
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 7
        prompt = router.get_prompt_for_plan(plan)
        assert GLOBAL_RED_FLAGS in prompt

    def test_red_flags_at_end_of_prompt(self):
        """GLOBAL_RED_FLAGS should be appended at the end."""
        from phase.router import PhaseRouter
        from state.models import TravelPlanState
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 1
        prompt = router.get_prompt_for_plan(plan)
        assert prompt.rstrip().endswith(GLOBAL_RED_FLAGS.rstrip())


class TestLegacyStateWriterRemovedInPrompts:
    """After Step 3, prompts must not reference the removed omnibus state writer."""

    def test_no_legacy_state_writer_call_in_phase1(self):
        assert _LEGACY_STATE_WRITE_CALL not in PHASE1_PROMPT
        assert _LEGACY_STATE_WRITE_FIELD_CALL not in PHASE1_PROMPT

    def test_no_legacy_state_writer_call_in_phase3_base(self):
        assert _LEGACY_STATE_WRITE_CALL not in PHASE3_BASE_PROMPT

    def test_no_legacy_state_writer_call_in_phase3_steps(self):
        for step_name, step_prompt in PHASE3_STEP_PROMPTS.items():
            assert _LEGACY_STATE_WRITE_CALL not in step_prompt, (
                f"Phase 3 sub-stage '{step_name}' still references {_LEGACY_STATE_WRITE_CALL}"
            )

    def test_no_legacy_state_writer_call_in_phase5(self):
        assert _LEGACY_STATE_WRITE_CALL not in PHASE5_PROMPT
        assert _LEGACY_STATE_WRITE_TOOL not in PHASE5_PROMPT

    def test_no_legacy_state_writer_call_in_phase7(self):
        assert _LEGACY_STATE_WRITE_CALL not in PHASE7_PROMPT

    def test_no_legacy_state_writer_call_in_global_red_flags(self):
        assert _LEGACY_STATE_WRITE_CALL not in GLOBAL_RED_FLAGS

    def test_phase3_skeleton_prompt_mentions_select_skeleton(self):
        skeleton = PHASE3_STEP_PROMPTS["skeleton"]
        assert "select_skeleton" in skeleton

    def test_phase3_brief_prompt_mentions_set_trip_brief(self):
        brief = PHASE3_STEP_PROMPTS["brief"]
        assert "set_trip_brief" in brief

    def test_phase5_mentions_append_day_plan(self):
        assert "append_day_plan" in PHASE5_PROMPT

    def test_phase5_mentions_replace_daily_plans(self):
        assert "replace_daily_plans" in PHASE5_PROMPT

    def test_phase5_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE5_PROMPT

    def test_phase7_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE7_PROMPT

    def test_phase1_mentions_update_trip_basics(self):
        assert "update_trip_basics" in PHASE1_PROMPT

    def test_global_red_flags_mentions_request_backtrack(self):
        assert "request_backtrack" in GLOBAL_RED_FLAGS

    def test_phase1_state_write_mentions_split_constraint_tools(self):
        """Finding 1: Phase 1 prompt should mention add_preferences/add_constraints for explicit constraints/preferences."""
        # The prompt should guide users to write explicit constraints/preferences immediately
        # It currently only mentions update_trip_basics, but should also mention the split tools
        assert "add_preferences" in PHASE1_PROMPT or "add_constraint" in PHASE1_PROMPT

    def test_phase5_describes_split_apis_correctly(self):
        """Finding 2: Phase 5 prompt must describe the real split APIs (append_day_plan vs replace_daily_plans)."""
        # The prompt should describe append_day_plan for single day and replace_daily_plans for batch
        # It should NOT describe the old dict/list payload model
        assert "append_day_plan(day=" in PHASE5_PROMPT or "append_day_plan(...)" in PHASE5_PROMPT
        assert "replace_daily_plans(days=[" in PHASE5_PROMPT or "replace_daily_plans(...)" in PHASE5_PROMPT
        # Should not have the old model where single dict vs list[dict] determines behavior
        assert "传单个 dict 表示追加单天；传 list[dict] 表示批量写入" not in PHASE5_PROMPT
