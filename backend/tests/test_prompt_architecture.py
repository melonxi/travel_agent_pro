# backend/tests/test_prompt_architecture.py
"""Tests for prompt skill-card architecture upgrade."""

from phase.prompts import (
    PHASE1_PROMPT,
    PHASE3_BASE_PROMPT,
    PHASE3_STEP_PROMPTS,
    PHASE5_PROMPT,
    PHASE7_PROMPT,
    PHASE_PROMPTS,
    build_phase3_prompt,
)
from phase.red_flags import (
    CORE_RED_FLAGS,
    build_active_red_flags,
    render_red_flags,
)

_LEGACY_STATE_WRITE_CALL = "update_plan_state("
_LEGACY_STATE_WRITE_FIELD_CALL = "update_plan_state(field="
_LEGACY_STATE_WRITE_TOOL = "update_plan_state"


class TestScopedRedFlags:
    def test_core_red_flags_exists_and_nonempty(self):
        assert len(CORE_RED_FLAGS) == 3
        rendered = render_red_flags(phase=1)
        assert "G-EVIDENCE" in rendered
        assert "G-STATE-AUTHORITY" in rendered
        assert "G-CAPABILITY-BOUNDARY" in rendered

    def test_red_flags_header_explains_non_goal_semantics_and_tracking_ids(self):
        rendered = render_red_flags(phase=1)
        assert "不是任务目标" in rendered
        assert "不是执行步骤" in rendered
        assert "正在走偏" in rendered
        assert "编号仅用于系统测试和追踪" in rendered
        assert "不是额外指令" in rendered

    def test_core_red_flags_have_no_phase_specific_terms(self):
        rendered_core = "\n".join(
            f"{flag.id} {flag.trigger} {flag.repair}" for flag in CORE_RED_FLAGS
        )
        forbidden_terms = {
            "destination",
            "dates",
            "selected_skeleton_id",
            "selected_transport",
            "accommodation",
            "daily_plans",
            "travel_plan_markdown",
            "checklist_markdown",
            "generate_summary",
            "save_day_plan",
            "set_shortlist",
            "set_skeleton_plans",
        }
        for term in forbidden_terms:
            assert term not in rendered_core

    def test_phase1_prompt_does_not_include_phase5_or_phase7_red_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(1))
        assert "P1-1" in prompt
        assert "P5-1" not in prompt
        assert "P7-1" not in prompt

    def test_phase3_candidate_does_not_include_skeleton_generation_red_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(3, "candidate"))
        assert "P3-CAND-1" in prompt
        assert "P3-SKEL-1" not in prompt

    def test_phase3_skeleton_includes_no_search_no_skeleton_flag(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(3, "skeleton"))
        assert "P3-SKEL-1" in prompt
        assert "P3-CAND-1" not in prompt

    def test_phase5_prompt_does_not_include_phase1_destination_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(5))
        assert "P5-1" in prompt
        assert "P1-1" not in prompt

    def test_phase7_prompt_does_not_include_dayplan_assembly_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(7))
        assert "P7-1" in prompt
        assert "P5-1" not in prompt

    def test_worker_red_flags_are_isolated_from_main_agent(self):
        worker_rendered = render_red_flags(phase=5, worker=True)
        assert "W5-1" in worker_rendered
        assert "P5-1" not in worker_rendered
        assert "G-EVIDENCE" not in worker_rendered

    def test_active_red_flags_stay_under_budget(self):
        cases = [
            (1, None),
            (3, "brief"),
            (3, "candidate"),
            (3, "skeleton"),
            (3, "lock"),
            (5, None),
            (7, None),
        ]
        for phase, step in cases:
            assert len(build_active_red_flags(phase=phase, phase3_step=step)) <= 12

    def _make_plan(self, phase: int, phase3_step: str = "brief") -> "TravelPlanState":
        plan = TravelPlanState(session_id="test")
        plan.phase = phase
        plan.phase3_step = phase3_step
        return plan


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
        prompt = PhaseRouter().get_prompt_for_plan(TravelPlanState(session_id="test"))
        assert "## 当前阶段 Red Flags（高危失败信号）" in prompt

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
        prompt = PhaseRouter().get_prompt_for_plan(TravelPlanState(session_id="test"))
        assert "P1-2" in prompt


class TestPhase3Split:
    """Phase 3 must be split into base + per-step prompts."""

    def test_base_prompt_exists(self):
        assert len(PHASE3_BASE_PROMPT) > 100

    def test_base_prompt_has_role(self):
        assert "## 角色" in PHASE3_BASE_PROMPT

    def test_base_prompt_has_state_write_discipline(self):
        assert (
            "状态写入纪律" in PHASE3_BASE_PROMPT or "状态写入契约" in PHASE3_BASE_PROMPT
        )

    def test_step_prompts_cover_all_steps(self):
        assert set(PHASE3_STEP_PROMPTS.keys()) == {
            "brief",
            "candidate",
            "skeleton",
            "lock",
        }

    def test_each_step_has_goal(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "目标" in prompt, f"step {step} missing 目标"

    def test_each_step_has_tool_strategy(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "工具" in prompt, f"step {step} missing tool strategy"

    def test_each_step_has_completion_gate(self):
        for step, prompt in PHASE3_STEP_PROMPTS.items():
            assert "完成 Gate" in prompt or "完成标志" in prompt, (
                f"step {step} missing completion gate"
            )

    def test_each_step_has_red_flags(self):
        router = PhaseRouter()
        for step in PHASE3_STEP_PROMPTS:
            plan = TravelPlanState(session_id="test")
            plan.phase = 3
            plan.phase3_step = step
            prompt = router.get_prompt_for_plan(plan)
            assert "## 当前阶段 Red Flags（高危失败信号）" in prompt, (
                f"step {step} missing Red Flags"
            )

    def test_brief_has_convergence_pressure(self):
        """Brief must have convergence pressure — the fix for Question 8."""
        assert (
            "轮" in PHASE3_STEP_PROMPTS["brief"]
            or "收敛" in PHASE3_STEP_PROMPTS["brief"]
        )

    def test_skeleton_has_thinking_framework(self):
        """Skeleton must have structured thinking — the fix for Question 4."""
        assert (
            "锚点" in PHASE3_STEP_PROMPTS["skeleton"]
            or "锚定" in PHASE3_STEP_PROMPTS["skeleton"]
        )

    def test_skeleton_marks_candidate_pois_as_single_day_owned(self):
        assert "单天专属候选池" in PHASE3_STEP_PROMPTS["skeleton"]

    def test_skeleton_requires_global_uniqueness_across_locked_and_candidate(self):
        prompt = PHASE3_STEP_PROMPTS["skeleton"]
        assert "locked_pois" in prompt
        assert "candidate_pois" in prompt
        assert "同一套 skeleton 内" in prompt
        assert "只能出现在一天" in prompt
        assert "上野公園" in prompt
        assert "不要把 `上野公園` 同时写进 Day 1 和 Day 2 的 `candidate_pois`。" in prompt

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
        plan = TravelPlanState(session_id="test")
        plan.phase = 5
        prompt = PhaseRouter().get_prompt_for_plan(plan)
        assert "P5-1" in prompt

    def test_phase5_has_incremental_strategy(self):
        """Phase 5 must use incremental generation — fix for Question 3."""
        assert (
            "增量" in PHASE5_PROMPT
            or "逐天" in PHASE5_PROMPT
            or "按天" in PHASE5_PROMPT
        )

    def test_phase5_has_route_planning_framing(self):
        """Phase 5 must frame as route optimization — fix for Question 5."""
        assert (
            "路径" in PHASE5_PROMPT
            or "动线" in PHASE5_PROMPT
            or "路线" in PHASE5_PROMPT
        )

    def test_phase5_no_batch_all_days_instruction(self):
        """Must NOT instruct to batch all days at once — the old anti-pattern."""
        assert "优先一次性用 list[dict] 提交全部天数" not in PHASE5_PROMPT

    def test_phase5_backward_compat(self):
        assert PHASE_PROMPTS[5] == PHASE5_PROMPT

    def test_phase5_has_input_gate(self):
        assert (
            "输入 Gate" in PHASE5_PROMPT
            or "输入检查" in PHASE5_PROMPT
            or "接手" in PHASE5_PROMPT
        )

    def test_phase5_has_tool_contract(self):
        assert "工具契约" in PHASE5_PROMPT or "工具策略" in PHASE5_PROMPT

    def test_phase5_mentions_optimize_day_route_in_workflow(self):
        assert "optimize_day_route" in PHASE5_PROMPT

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
        assert (
            "输入 Gate" in PHASE7_PROMPT
            or "输入检查" in PHASE7_PROMPT
            or "接手" in PHASE7_PROMPT
        )

    def test_phase7_has_completion_gate(self):
        assert "## 完成 Gate" in PHASE7_PROMPT

    def test_phase7_has_red_flags(self):
        plan = TravelPlanState(session_id="test")
        plan.phase = 7
        prompt = PhaseRouter().get_prompt_for_plan(plan)
        assert "P7-1" in prompt

    def test_phase7_has_tool_contract(self):
        assert "工具契约" in PHASE7_PROMPT or "工具策略" in PHASE7_PROMPT

    def test_phase7_mentions_check_weather(self):
        assert "check_weather" in PHASE7_PROMPT

    def test_phase7_mentions_generate_summary(self):
        assert "generate_summary" in PHASE7_PROMPT

    def test_phase7_mentions_travel_plan_markdown(self):
        assert "travel_plan_markdown" in PHASE7_PROMPT

    def test_phase7_mentions_checklist_markdown(self):
        assert "checklist_markdown" in PHASE7_PROMPT

    def test_phase7_mentions_frozen_deliverables(self):
        assert "冻结" in PHASE7_PROMPT or "先回退" in PHASE7_PROMPT

    def test_phase7_mentions_search_travel_services(self):
        assert "search_travel_services" in PHASE7_PROMPT

    def test_phase7_backward_compat(self):
        assert PHASE_PROMPTS[7] == PHASE7_PROMPT

    def test_phase7_has_checklist_categories(self):
        assert "证件" in PHASE7_PROMPT
        assert "天气" in PHASE7_PROMPT


class TestActiveRedFlagsInjection:
    """Only active scoped Red Flags must be injected into router prompts."""

    def test_phase1_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 1
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P1-1" in prompt

    def test_phase3_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 3
        plan.phase3_step = "brief"
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P3-BASE-1" in prompt
        assert "P3-BRIEF-1" in prompt

    def test_phase3_all_steps_include_scoped_red_flags(self):
        router = PhaseRouter()
        expected_prefixes = {
            "brief": "P3-BRIEF",
            "candidate": "P3-CAND",
            "skeleton": "P3-SKEL",
            "lock": "P3-LOCK",
        }
        for step in ("brief", "candidate", "skeleton", "lock"):
            plan = TravelPlanState(session_id="test")
            plan.phase = 3
            plan.phase3_step = step
            prompt = router.get_prompt_for_plan(plan)
            assert expected_prefixes[step] in prompt

    def test_phase5_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 5
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P5-1" in prompt

    def test_phase7_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 7
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P7-1" in prompt

    def test_red_flags_at_end_of_prompt(self):
        """Active Red Flags should be appended at the end of the phase prompt."""
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 1
        prompt = router.get_prompt_for_plan(plan)
        assert prompt.rstrip().endswith(render_red_flags(phase=1).rstrip())


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
        assert _LEGACY_STATE_WRITE_CALL not in render_red_flags(phase=1)

    def test_phase3_skeleton_prompt_mentions_select_skeleton(self):
        skeleton = PHASE3_STEP_PROMPTS["skeleton"]
        assert "select_skeleton" in skeleton

    def test_phase3_brief_prompt_mentions_set_trip_brief(self):
        brief = PHASE3_STEP_PROMPTS["brief"]
        assert "set_trip_brief" in brief

    def test_phase5_mentions_optimize_day_route(self):
        assert "optimize_day_route" in PHASE5_PROMPT

    def test_phase5_mentions_save_day_plan(self):
        assert "save_day_plan" in PHASE5_PROMPT

    def test_phase5_mentions_replace_all_day_plans(self):
        assert "replace_all_day_plans" in PHASE5_PROMPT

    def test_phase5_does_not_mention_legacy_plan_tools(self):
        assert "append_day_plan" not in PHASE5_PROMPT
        assert "replace_daily_plans" not in PHASE5_PROMPT
        assert "assemble_day_plan" not in PHASE5_PROMPT

    def test_phase5_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE5_PROMPT

    def test_phase7_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE7_PROMPT

    def test_phase1_mentions_update_trip_basics(self):
        assert "update_trip_basics" in PHASE1_PROMPT

    def test_phase1_state_write_mentions_split_constraint_tools(self):
        """Finding 1: Phase 1 prompt should mention add_preferences/add_constraints for explicit constraints/preferences."""
        # The prompt should guide users to write explicit constraints/preferences immediately
        # It currently only mentions update_trip_basics, but should also mention the split tools
        assert "add_preferences" in PHASE1_PROMPT or "add_constraint" in PHASE1_PROMPT

    def test_phase5_describes_split_apis_correctly(self):
        assert 'save_day_plan(mode="create"' in PHASE5_PROMPT
        assert 'save_day_plan(mode="replace_existing"' in PHASE5_PROMPT
        assert "replace_all_day_plans(days=[" in PHASE5_PROMPT
        assert "optimize_day_route 只是路线辅助" in PHASE5_PROMPT
