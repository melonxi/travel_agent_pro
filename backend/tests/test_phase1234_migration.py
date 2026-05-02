from __future__ import annotations

from pathlib import Path

from config import ApiKeysConfig
from phase.router import PhaseRouter
from phase.prompts import PHASE_CONTROL_MODE, PHASE_PROMPTS
from state.models import (
    Accommodation,
    DateRange,
    DayPlan,
    TravelPlanState,
    _PHASE_DOWNSTREAM,
)
from tools.web_search import make_web_search_tool


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_router_uses_contiguous_phase_numbers_1_to_4() -> None:
    router = PhaseRouter()

    assert router.infer_phase(TravelPlanState(session_id="empty")) == 1
    assert (
        router.infer_phase(
            TravelPlanState(session_id="destination", destination="东京")
        )
        == 2
    )
    assert (
        router.infer_phase(
            TravelPlanState(
                session_id="daily",
                destination="东京",
                dates=DateRange("2026-05-01", "2026-05-02"),
                selected_skeleton_id="balanced",
                skeleton_plans=[{"id": "balanced", "days": [{}, {}]}],
                accommodation=Accommodation(area="银座"),
            )
        )
        == 3
    )
    assert (
        router.infer_phase(
            TravelPlanState(
                session_id="delivery",
                destination="东京",
                dates=DateRange("2026-05-01", "2026-05-02"),
                selected_skeleton_id="balanced",
                skeleton_plans=[{"id": "balanced", "days": [{}, {}]}],
                accommodation=Accommodation(area="银座"),
                daily_plans=[
                    DayPlan(day=1, date="2026-05-01"),
                    DayPlan(day=2, date="2026-05-02"),
                ],
            )
        )
        == 4
    )


def test_phase_prompt_and_downstream_maps_only_use_current_phase_numbers() -> None:
    assert set(PHASE_PROMPTS) == {1, 2, 3, 4}
    assert set(PHASE_CONTROL_MODE) == {1, 2, 3, 4}
    assert set(_PHASE_DOWNSTREAM) == {1, 2, 3}


def test_legacy_saved_phase_numbers_migrate_to_1234_protocol() -> None:
    assert TravelPlanState.from_dict({"session_id": "old-1", "phase": 1}).phase == 1
    assert TravelPlanState.from_dict({"session_id": "old-2", "phase": 3}).phase == 2
    assert TravelPlanState.from_dict({"session_id": "old-3", "phase": 5}).phase == 3
    assert TravelPlanState.from_dict({"session_id": "old-4", "phase": 7}).phase == 4


def test_new_saved_phase_numbers_are_not_remapped_when_state_version_is_current() -> None:
    assert (
        TravelPlanState.from_dict({"session_id": "new-2", "phase": 2, "version": 2}).phase
        == 2
    )
    assert (
        TravelPlanState.from_dict({"session_id": "new-3", "phase": 3, "version": 2}).phase
        == 3
    )
    assert (
        TravelPlanState.from_dict({"session_id": "new-4", "phase": 4, "version": 2}).phase
        == 4
    )


def test_phase4_prompt_tools_are_available_to_agent() -> None:
    assert make_web_search_tool(ApiKeysConfig()).phases == [1, 2, 3, 4]


def test_demo_phase_labels_use_current_phase_numbers() -> None:
    demo_spec = (REPO_ROOT / "scripts/demo/demo-full-flow.spec.ts").read_text()

    assert "2: '日期与住宿'" in demo_spec
    assert "4: '出发前查漏'" in demo_spec
    assert "5: '行程组装'" not in demo_spec
    assert "7: '出发前查漏'" not in demo_spec


def test_readme_production_path_uses_current_phase_numbers() -> None:
    readme = (REPO_ROOT / "README.md").read_text()

    assert "| 2 | Framework Planning |" in readme
    assert "| 3 | Daily Itinerary Assembly |" in readme
    assert "| 4 | Pre-Departure Checklist |" in readme
    assert "| 5 | Daily Itinerary Assembly |" not in readme
    assert "| 7 | Pre-Departure Checklist |" not in readme
    assert "Phase 2 has four substeps" in readme


def test_failure_analysis_defaults_to_current_final_phase() -> None:
    capture_script = (
        REPO_ROOT / "scripts/failure-analysis/capture_screenshots.ts"
    ).read_text()

    assert "result.plan_state.phase : 4" in capture_script
    assert "planState.phase : 4" in capture_script
    assert "result.plan_state.phase : 7" not in capture_script
    assert "planState.phase : 7" not in capture_script


def test_current_project_overview_phase3_doc_references_exist() -> None:
    assert (
        REPO_ROOT / "docs/postmortems/2026-04-19-phase3-parallel-guard-refactor.md"
    ).exists()
    assert (REPO_ROOT / "docs/learning/assets/phase3-parallel-orchestration").is_dir()


def test_plan_state_serializes_phase2_step() -> None:
    plan = TravelPlanState(session_id="phase2-step")
    plan.phase2_step = "skeleton"

    data = plan.to_dict()

    assert data["phase2_step"] == "skeleton"


def test_e2e_phase_transition_mock_reads_phase2_step() -> None:
    e2e_spec = (REPO_ROOT / "e2e-test.spec.ts").read_text()

    assert "transitionStep.plan.phase2_step ?? null" in e2e_spec
