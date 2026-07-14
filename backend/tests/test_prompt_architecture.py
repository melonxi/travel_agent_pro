# backend/tests/test_prompt_architecture.py
"""Tests for prompt skill-card architecture upgrade."""

from phase.prompts import (
    PHASE1_PROMPT,
    PHASE2_BASE_PROMPT,
    PHASE2_STEP_PROMPTS,
    PHASE3_PROMPT,
    PHASE4_PROMPT,
    PHASE_PROMPTS,
    build_phase2_prompt,
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

    def test_phase1_prompt_does_not_include_downstream_red_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(1))
        assert "P1-1" in prompt
        assert "P2-BASE-1" not in prompt
        assert "P3-1" not in prompt
        assert "P4-1" not in prompt

    def test_phase2_candidate_does_not_include_skeleton_generation_red_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(2, "candidate"))
        assert "P2-CAND-1" in prompt
        assert "P2-SKEL-1" not in prompt

    def test_phase2_skeleton_includes_no_search_no_skeleton_flag(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(2, "skeleton"))
        assert "P2-SKEL-1" in prompt
        assert "P2-CAND-1" not in prompt

    def test_phase3_prompt_does_not_include_phase1_destination_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(3))
        assert "P3-1" in prompt
        assert "P1-1" not in prompt

    def test_phase4_prompt_does_not_include_dayplan_assembly_flags(self):
        prompt = PhaseRouter().get_prompt_for_plan(self._make_plan(4))
        assert "P4-1" in prompt
        assert "P3-1" not in prompt

    def test_worker_red_flags_are_isolated_from_main_agent(self):
        worker_rendered = render_red_flags(phase=3, worker=True)
        assert "W3-1" in worker_rendered
        assert "P3-1" not in worker_rendered
        assert "G-EVIDENCE" not in worker_rendered

    def test_active_red_flags_stay_under_budget(self):
        cases = [
            (1, None),
            (3, "brief"),
            (3, "candidate"),
            (3, "skeleton"),
            (3, "lock"),
            (3, None),
            (4, None),
        ]
        for phase, step in cases:
            assert len(build_active_red_flags(phase=phase, phase2_step=step)) <= 12

    def _make_plan(self, phase: int, phase2_step: str = "brief") -> "TravelPlanState":
        plan = TravelPlanState(session_id="test")
        plan.phase = phase
        plan.phase2_step = phase2_step
        return plan


class TestPhase1ExecutionRules:
    def test_phase1_prompt_is_execution_rules_not_identity(self):
        assert "## 操作规则" in PHASE1_PROMPT
        assert "## 角色" not in PHASE1_PROMPT
        assert "目的地收敛顾问" not in PHASE1_PROMPT

    def test_phase1_goal_lives_in_soul_not_phase_rules(self):
        assert "## 目标" not in PHASE1_PROMPT
        assert "把用户的模糊意图收敛" not in PHASE1_PROMPT

    def test_phase1_rules_have_operational_boundaries(self):
        assert "## 边界例外" in PHASE1_PROMPT
        assert "## 工具契约" in PHASE1_PROMPT

    def test_phase1_uses_write_then_close_protocol(self):
        assert "## 写入即收尾" in PHASE1_PROMPT

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
        # xhs 工具下线,UGC 改走 web_search 域内搜索
        assert "UGC 域内搜索" in PHASE1_PROMPT
        assert "web_search" in PHASE1_PROMPT
        assert "xiaohongshu_search_notes" not in PHASE1_PROMPT

    def test_phase1_skips_search_when_destination_confirmed(self):
        assert "不要先做目的地研究" in PHASE1_PROMPT

    def test_phase1_boundary_red_flag(self):
        """Phase 1 Red Flags must warn against boundary violations (Question 7)."""
        assert "预算" in PHASE1_PROMPT
        prompt = PhaseRouter().get_prompt_for_plan(TravelPlanState(session_id="test"))
        assert "P1-2" in prompt


class TestPhase2Split:
    """Phase 2 must be split into base + per-step prompts."""

    def test_base_prompt_exists(self):
        assert len(PHASE2_BASE_PROMPT) > 100

    def test_base_prompt_is_execution_rules_not_identity(self):
        assert "## 角色" not in PHASE2_BASE_PROMPT
        assert "## 状态写入纪律" in PHASE2_BASE_PROMPT

    def test_base_prompt_has_state_write_discipline(self):
        assert (
            "状态写入纪律" in PHASE2_BASE_PROMPT or "状态写入契约" in PHASE2_BASE_PROMPT
        )

    def test_step_prompts_cover_all_steps(self):
        assert set(PHASE2_STEP_PROMPTS.keys()) == {
            "brief",
            "candidate",
            "skeleton",
            "lock",
        }

    def test_each_step_declares_current_substage(self):
        for step, prompt in PHASE2_STEP_PROMPTS.items():
            assert "当前子阶段" in prompt, f"step {step} missing substage header"

    def test_each_step_has_tool_strategy(self):
        for step, prompt in PHASE2_STEP_PROMPTS.items():
            assert "工具" in prompt, f"step {step} missing tool strategy"

    def test_each_step_has_key_output_or_write_contract(self):
        for step, prompt in PHASE2_STEP_PROMPTS.items():
            assert any(
                marker in prompt
                for marker in ("状态写入分工", "字段契约", "写入 `", "用户确认")
            ), (
                f"step {step} missing output/write contract"
            )

    def test_each_step_has_red_flags(self):
        router = PhaseRouter()
        for step in PHASE2_STEP_PROMPTS:
            plan = TravelPlanState(session_id="test")
            plan.phase = 2
            plan.phase2_step = step
            prompt = router.get_prompt_for_plan(plan)
            assert "## 当前阶段 Red Flags（高危失败信号）" in prompt, (
                f"step {step} missing Red Flags"
            )

    def test_brief_has_convergence_pressure(self):
        """Brief must have convergence pressure — the fix for Question 8."""
        assert (
            "轮" in PHASE2_STEP_PROMPTS["brief"]
            or "收敛" in PHASE2_STEP_PROMPTS["brief"]
        )

    def test_skeleton_has_thinking_framework(self):
        """Skeleton must have structured thinking — the fix for Question 4."""
        assert (
            "锚点" in PHASE2_STEP_PROMPTS["skeleton"]
            or "锚定" in PHASE2_STEP_PROMPTS["skeleton"]
        )

    def test_skeleton_marks_candidate_pois_as_single_day_owned(self):
        assert "单天专属候选池" in PHASE2_STEP_PROMPTS["skeleton"]

    def test_skeleton_requires_global_uniqueness_across_locked_and_candidate(self):
        prompt = PHASE2_STEP_PROMPTS["skeleton"]
        assert "locked_pois" in prompt
        assert "candidate_pois" in prompt
        assert "同一套 skeleton 内" in prompt
        assert "只能出现在一天" in prompt
        assert "写入前自查" in prompt
        assert "两天的 `candidate_pois`" in prompt

    def test_lock_mentions_transport_timing(self):
        """Lock must address transport timing — the fix for Question 2."""
        assert "大交通" in PHASE2_STEP_PROMPTS["lock"]

    def test_lock_distinguishes_preference_from_lock_consent(self):
        prompt = PHASE2_STEP_PROMPTS["lock"]
        assert "锁定授权边界" in prompt
        assert "倾向" in prompt
        assert "帮我搜一下" in prompt
        assert "不是锁定授权" in prompt
        assert "交通选A，接着推荐住宿" in prompt
        assert "set_transport_options" in prompt
        assert "只有用户明确说" in prompt
        assert "select_transport" in prompt


class TestBuildPhase2Prompt:
    """build_phase2_prompt() must assemble base + step correctly."""

    def test_default_returns_base_plus_brief(self):
        result = build_phase2_prompt()
        assert PHASE2_BASE_PROMPT in result
        assert PHASE2_STEP_PROMPTS["brief"] in result

    def test_specific_step(self):
        for step in ("brief", "candidate", "skeleton", "lock"):
            result = build_phase2_prompt(step)
            assert PHASE2_BASE_PROMPT in result
            assert PHASE2_STEP_PROMPTS[step] in result

    def test_only_one_step_included(self):
        result = build_phase2_prompt("skeleton")
        assert PHASE2_STEP_PROMPTS["skeleton"] in result
        assert PHASE2_STEP_PROMPTS["brief"] not in result
        assert PHASE2_STEP_PROMPTS["candidate"] not in result
        assert PHASE2_STEP_PROMPTS["lock"] not in result

    def test_phase_prompts_2(self):
        """PHASE_PROMPTS[2] must return the Phase 2 default brief prompt."""
        assert PHASE_PROMPTS[2] == build_phase2_prompt("brief")


from phase.router import PhaseRouter
from state.models import TravelPlanState


class TestPhaseRouterGetPromptForPlan:
    """get_prompt_for_plan() must use build_phase2_prompt for phase 2."""

    def _make_plan(self, phase: int, phase2_step: str = "brief") -> TravelPlanState:
        plan = TravelPlanState(session_id="test")
        plan.phase = phase
        plan.phase2_step = phase2_step
        return plan

    def test_phase1_returns_phase1_prompt(self):
        router = PhaseRouter()
        plan = self._make_plan(1)
        prompt = router.get_prompt_for_plan(plan)
        assert "## 操作规则" in prompt
        assert "目的地收敛顾问" not in prompt

    def test_phase2_brief(self):
        router = PhaseRouter()
        plan = self._make_plan(2, "brief")
        prompt = router.get_prompt_for_plan(plan)
        assert "brief" in prompt.lower() or "旅行画像" in prompt
        assert "锚定不可移动项" not in prompt or "skeleton" in prompt.lower()

    def test_phase2_skeleton(self):
        router = PhaseRouter()
        plan = self._make_plan(2, "skeleton")
        prompt = router.get_prompt_for_plan(plan)
        assert "锚定不可移动项" in prompt or "骨架" in prompt

    def test_phase2_lock(self):
        router = PhaseRouter()
        plan = self._make_plan(2, "lock")
        prompt = router.get_prompt_for_plan(plan)
        assert "大交通" in prompt

    def test_phase3_returns_phase3_prompt(self):
        router = PhaseRouter()
        plan = self._make_plan(3)
        prompt = router.get_prompt_for_plan(plan)
        assert "逐日行程" in prompt or "daily_plans" in prompt

    def test_phase4_returns_phase4_prompt(self):
        router = PhaseRouter()
        plan = self._make_plan(4)
        prompt = router.get_prompt_for_plan(plan)
        assert "查漏" in prompt or "清单" in prompt

    def test_old_get_prompt_still_works(self):
        """Backward compat: get_prompt(phase) still returns a valid prompt."""
        router = PhaseRouter()
        prompt = router.get_prompt(3)
        assert len(prompt) > 100


class TestPhase3ExecutionRules:
    """Phase 3 prompt contains execution rules; identity lives in soul.md."""

    def test_phase3_prompt_is_execution_rules_not_identity(self):
        assert "## 角色" not in PHASE3_PROMPT
        assert "## 硬法则" in PHASE3_PROMPT

    def test_phase3_goal_lives_in_soul_not_phase_rules(self):
        assert "## 目标" not in PHASE3_PROMPT
        assert "逐日行程落地规划师" not in PHASE3_PROMPT

    def test_phase3_has_hard_rules(self):
        assert "## 硬法则" in PHASE3_PROMPT

    def test_phase3_completion_is_state_driven_not_manual_gate(self):
        assert "## 完成 Gate" not in PHASE3_PROMPT
        assert "save_day_plan" in PHASE3_PROMPT

    def test_phase3_has_red_flags(self):
        plan = TravelPlanState(session_id="test")
        plan.phase = 3
        prompt = PhaseRouter().get_prompt_for_plan(plan)
        assert "P3-1" in prompt

    def test_phase3_has_incremental_strategy(self):
        """Phase 3 must use incremental generation — fix for Question 3."""
        assert (
            "增量" in PHASE3_PROMPT
            or "逐天" in PHASE3_PROMPT
            or "按天" in PHASE3_PROMPT
        )

    def test_phase3_has_route_planning_framing(self):
        """Phase 3 must frame as route optimization — fix for Question 5."""
        assert (
            "路径" in PHASE3_PROMPT
            or "动线" in PHASE3_PROMPT
            or "路线" in PHASE3_PROMPT
        )

    def test_phase3_no_batch_all_days_instruction(self):
        """Must NOT instruct to batch all days at once — the old anti-pattern."""
        assert "优先一次性用 list[dict] 提交全部天数" not in PHASE3_PROMPT

    def test_phase3_registered(self):
        assert PHASE_PROMPTS[3] == PHASE3_PROMPT

    def test_phase3_has_input_gate(self):
        assert (
            "输入 Gate" in PHASE3_PROMPT
            or "输入检查" in PHASE3_PROMPT
            or "接手" in PHASE3_PROMPT
        )

    def test_phase3_has_tool_contract(self):
        assert "工具契约" in PHASE3_PROMPT or "工具策略" in PHASE3_PROMPT

    def test_phase3_mentions_optimize_day_route_in_workflow(self):
        assert "optimize_day_route" in PHASE3_PROMPT

    def test_phase3_mentions_calculate_route(self):
        assert "calculate_route" in PHASE3_PROMPT

    def test_phase3_has_json_structure(self):
        assert "DayPlan" in PHASE3_PROMPT or "daily_plans" in PHASE3_PROMPT

    def test_phase3_has_pressure_scenarios(self):
        assert "压力场景" in PHASE3_PROMPT or "场景" in PHASE3_PROMPT


class TestPhase4ExecutionRules:
    """Phase 4 prompt contains execution rules; identity lives in soul.md."""

    def test_phase4_prompt_is_execution_rules_not_identity(self):
        assert "## 角色" not in PHASE4_PROMPT
        assert "## 输入 Gate" in PHASE4_PROMPT

    def test_phase4_goal_lives_in_soul_not_phase_rules(self):
        assert "## 目标" not in PHASE4_PROMPT
        assert "出发前查漏补缺顾问" not in PHASE4_PROMPT

    def test_phase4_has_state_write_rules(self):
        assert "## 状态写入契约" in PHASE4_PROMPT

    def test_phase4_has_input_gate(self):
        assert (
            "输入 Gate" in PHASE4_PROMPT
            or "输入检查" in PHASE4_PROMPT
            or "接手" in PHASE4_PROMPT
        )

    def test_phase4_completion_is_generate_summary_contract(self):
        assert "## 完成 Gate" not in PHASE4_PROMPT
        assert "generate_summary" in PHASE4_PROMPT

    def test_phase4_has_red_flags(self):
        plan = TravelPlanState(session_id="test")
        plan.phase = 4
        prompt = PhaseRouter().get_prompt_for_plan(plan)
        assert "P4-1" in prompt

    def test_phase4_has_tool_contract(self):
        assert "工具契约" in PHASE4_PROMPT or "工具策略" in PHASE4_PROMPT

    def test_phase4_mentions_check_weather(self):
        assert "check_weather" in PHASE4_PROMPT

    def test_phase4_mentions_generate_summary(self):
        assert "generate_summary" in PHASE4_PROMPT

    def test_phase4_mentions_structured_fields(self):
        assert "daily_sections" in PHASE4_PROMPT
        assert "checklist_title" in PHASE4_PROMPT

    def test_phase4_mentions_frozen_deliverables(self):
        assert "冻结" in PHASE4_PROMPT or "先回退" in PHASE4_PROMPT

    def test_phase4_mentions_search_travel_services(self):
        # search_travel_services(flyai)已下线,服务搜索降级 web_search
        assert "search_travel_services" not in PHASE4_PROMPT
        assert "签证办理" in PHASE4_PROMPT
        assert "web_search" in PHASE4_PROMPT

    def test_phase4_registered(self):
        assert PHASE_PROMPTS[4] == PHASE4_PROMPT

    def test_phase4_has_checklist_categories(self):
        assert "证件" in PHASE4_PROMPT
        assert "天气" in PHASE4_PROMPT


class TestActiveRedFlagsInjection:
    """Only active scoped Red Flags must be injected into router prompts."""

    def test_phase1_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 1
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P1-1" in prompt

    def test_phase2_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 2
        plan.phase2_step = "brief"
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P2-BASE-1" in prompt
        assert "P2-BRIEF-1" in prompt

    def test_phase3_all_steps_include_scoped_red_flags(self):
        router = PhaseRouter()
        expected_prefixes = {
            "brief": "P2-BRIEF",
            "candidate": "P2-CAND",
            "skeleton": "P2-SKEL",
            "lock": "P2-LOCK",
        }
        for step in ("brief", "candidate", "skeleton", "lock"):
            plan = TravelPlanState(session_id="test")
            plan.phase = 2
            plan.phase2_step = step
            prompt = router.get_prompt_for_plan(plan)
            assert expected_prefixes[step] in prompt

    def test_phase3_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 3
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P3-1" in prompt

    def test_phase4_includes_active_red_flags(self):
        router = PhaseRouter()
        plan = TravelPlanState(session_id="test")
        plan.phase = 4
        prompt = router.get_prompt_for_plan(plan)
        assert "G-EVIDENCE" in prompt
        assert "P4-1" in prompt

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
        assert _LEGACY_STATE_WRITE_CALL not in PHASE2_BASE_PROMPT

    def test_no_legacy_state_writer_call_in_phase2_steps(self):
        for step_name, step_prompt in PHASE2_STEP_PROMPTS.items():
            assert _LEGACY_STATE_WRITE_CALL not in step_prompt, (
                f"Phase 2 sub-stage '{step_name}' still references {_LEGACY_STATE_WRITE_CALL}"
            )

    def test_no_legacy_state_writer_call_in_phase3(self):
        assert _LEGACY_STATE_WRITE_CALL not in PHASE3_PROMPT
        assert _LEGACY_STATE_WRITE_TOOL not in PHASE3_PROMPT

    def test_no_legacy_state_writer_call_in_phase4(self):
        assert _LEGACY_STATE_WRITE_CALL not in PHASE4_PROMPT

    def test_no_legacy_state_writer_call_in_global_red_flags(self):
        assert _LEGACY_STATE_WRITE_CALL not in render_red_flags(phase=1)

    def test_phase3_skeleton_prompt_mentions_select_skeleton(self):
        skeleton = PHASE2_STEP_PROMPTS["skeleton"]
        assert "select_skeleton" in skeleton

    def test_phase3_brief_prompt_mentions_set_trip_brief(self):
        brief = PHASE2_STEP_PROMPTS["brief"]
        assert "set_trip_brief" in brief

    def test_phase3_mentions_optimize_day_route(self):
        assert "optimize_day_route" in PHASE3_PROMPT

    def test_phase3_mentions_save_day_plan(self):
        assert "save_day_plan" in PHASE3_PROMPT

    def test_phase3_mentions_replace_all_day_plans(self):
        assert "replace_all_day_plans" in PHASE3_PROMPT

    def test_phase3_does_not_mention_legacy_plan_tools(self):
        assert "append_day_plan" not in PHASE3_PROMPT
        assert "replace_daily_plans" not in PHASE3_PROMPT
        assert "assemble_day_plan" not in PHASE3_PROMPT

    def test_phase3_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE3_PROMPT

    def test_phase4_mentions_request_backtrack(self):
        assert "request_backtrack" in PHASE4_PROMPT

    def test_phase1_mentions_update_trip_basics(self):
        assert "update_trip_basics" in PHASE1_PROMPT

    def test_phase1_state_write_mentions_split_constraint_tools(self):
        """Finding 1: Phase 1 prompt should mention add_preferences/add_constraints for explicit constraints/preferences."""
        # The prompt should guide users to write explicit constraints/preferences immediately
        # It currently only mentions update_trip_basics, but should also mention the split tools
        assert "add_preferences" in PHASE1_PROMPT or "add_constraint" in PHASE1_PROMPT

    def test_phase3_describes_split_apis_correctly(self):
        assert 'save_day_plan(mode="create"' in PHASE3_PROMPT
        assert 'save_day_plan(mode="replace_existing"' in PHASE3_PROMPT
        assert "replace_all_day_plans(days=[" in PHASE3_PROMPT
        assert "`optimize_day_route` 不写状态" in PHASE3_PROMPT
