import pytest

from agent.types import Message, Role, ToolCall, ToolResult
from api.orchestration.session.runtime_view import (
    HistoryMessage,
    build_cache_first_history_for_restore,
    build_runtime_view_for_restore,
)
from state.models import TravelPlanState


class FakePhaseRouter:
    def __init__(self) -> None:
        self.calls = 0

    def get_prompt_for_plan(self, plan):
        self.calls += 1
        return f"PROMPT phase={plan.phase} step={getattr(plan, 'phase2_step', None)}"


class FakeContextManager:
    def __init__(self) -> None:
        self.available_tools = None
        self.memory_context = None

    def build_system_message(self, plan, phase_prompt, memory_context, *, available_tools):
        self.available_tools = list(available_tools)
        self.memory_context = memory_context
        return Message(
            role=Role.SYSTEM,
            content=(
                f"fresh system | {phase_prompt} | memory={memory_context} | "
                f"tools={','.join(available_tools)}"
            ),
        )


class FakeMemoryManager:
    def __init__(self) -> None:
        self.calls = []

    async def generate_context(self, user_id, plan):
        self.calls.append((user_id, plan.phase, getattr(plan, "phase2_step", None)))
        return ("memory for restore", ["mem_1"], 1, 0, 0)


class FakeToolEngine:
    def __init__(self) -> None:
        self.calls = []

    def get_tools_for_phase(self, phase, plan):
        self.calls.append((phase, getattr(plan, "phase2_step", None)))
        return [{"name": f"phase_{phase}_tool"}, {"name": "request_backtrack"}]


def hm(
    role,
    content,
    *,
    phase=None,
    phase2_step=None,
    history_seq=0,
    context_epoch=None,
    rebuild_reason=None,
    tool_calls=None,
    tool_result=None,
):
    return HistoryMessage(
        message=Message(
            role=role,
            content=content,
            tool_calls=tool_calls,
            tool_result=tool_result,
        ),
        phase=phase,
        phase2_step=phase2_step,
        history_seq=history_seq,
        context_epoch=context_epoch,
        rebuild_reason=rebuild_reason,
        run_id=f"run_{history_seq}",
        trip_id="trip_1",
    )


@pytest.mark.asyncio
async def test_restore_cache_first_returns_full_non_system_history():
    plan = TravelPlanState(session_id="sess_1", phase=3, destination="东京")
    history = [
        hm(Role.SYSTEM, "old phase 1 system", phase=1, history_seq=0),
        hm(Role.USER, "我想去东京玩", phase=1, history_seq=1),
        hm(
            Role.ASSISTANT,
            None,
            phase=1,
            history_seq=2,
            tool_calls=[ToolCall(id="tc_old", name="update_trip_basics", arguments={})],
        ),
        hm(
            Role.TOOL,
            None,
            phase=1,
            history_seq=3,
            tool_result=ToolResult(
                tool_call_id="tc_old",
                status="success",
                data={"destination": "东京"},
            ),
        ),
        hm(Role.USER, "按这个骨架继续细化每天路线", phase=3, history_seq=9),
    ]

    phase_router = FakePhaseRouter()
    context_manager = FakeContextManager()
    memory_mgr = FakeMemoryManager()
    tool_engine = FakeToolEngine()

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=phase_router,
        context_manager=context_manager,
        memory_mgr=memory_mgr,
        memory_enabled=True,
        tool_engine=tool_engine,
    )

    assert [message.role for message in runtime] == [
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.USER,
    ]
    assert runtime[0].content == "我想去东京玩"
    assert runtime[-1].content == "按这个骨架继续细化每天路线"
    assert runtime[2].tool_result is not None
    assert "old phase 1 system" not in [message.content for message in runtime]
    assert memory_mgr.calls == []
    assert tool_engine.calls == []
    assert context_manager.available_tools is None


def test_restore_cache_first_caps_long_history_to_recent_messages():
    history = [
        hm(Role.USER, f"message-{index}", history_seq=index)
        for index in range(130)
    ]

    runtime = build_cache_first_history_for_restore(history, max_messages=120)

    assert len(runtime) == 120
    assert runtime[0].content == "message-10"
    assert runtime[-1].content == "message-129"


def test_restore_cache_first_drops_tool_results_cut_off_from_tool_calls():
    history = [
        hm(
            Role.ASSISTANT,
            "读取攻略",
            history_seq=1,
            tool_calls=[
                ToolCall(id="tc1", name="web_search", arguments={}),
                ToolCall(id="tc2", name="web_search", arguments={}),
            ],
        ),
        hm(
            Role.TOOL,
            None,
            history_seq=2,
            tool_result=ToolResult(tool_call_id="tc1", status="success", data={"a": 1}),
        ),
        hm(
            Role.TOOL,
            None,
            history_seq=3,
            tool_result=ToolResult(tool_call_id="tc2", status="success", data={"b": 2}),
        ),
        hm(Role.USER, "继续", history_seq=4),
    ]

    runtime = build_cache_first_history_for_restore(history, max_messages=3)

    assert [message.role for message in runtime] == [Role.USER]
    assert runtime[0].content == "继续"


@pytest.mark.asyncio
async def test_restore_phase3_skeleton_drops_orphan_tool_results():
    plan = TravelPlanState(
        session_id="sess_2",
        phase=2,
        phase2_step="skeleton",
        destination="大阪",
    )
    history = [
        hm(Role.USER, "先确定画像", phase=2, phase2_step="brief", history_seq=1),
        hm(
            Role.TOOL,
            None,
            phase=2,
            phase2_step="brief",
            history_seq=2,
            tool_result=ToolResult(
                tool_call_id="tc_brief",
                status="success",
                data={"trip_brief": "brief old result"},
            ),
        ),
        hm(Role.USER, "给我候选池", phase=2, phase2_step="candidate", history_seq=3),
        hm(
            Role.TOOL,
            None,
            phase=2,
            phase2_step="candidate",
            history_seq=4,
            tool_result=ToolResult(
                tool_call_id="tc_candidate",
                status="success",
                data={"candidate_pool": ["old candidate"]},
            ),
        ),
        hm(Role.USER, "从短名单生成两个骨架", phase=2, phase2_step="skeleton", history_seq=5),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=True,
        tool_engine=FakeToolEngine(),
    )

    assert [message.role for message in runtime] == [Role.USER, Role.USER, Role.USER]
    assert runtime[-1].content == "从短名单生成两个骨架"
    rendered = "\n".join(str(message.content) for message in runtime)
    assert "先确定画像" in rendered
    assert "给我候选池" in rendered


@pytest.mark.asyncio
async def test_restore_after_backtrack_drops_orphan_tool_results():
    plan = TravelPlanState(
        session_id="sess_3",
        phase=2,
        phase2_step="brief",
        destination="京都",
    )
    history = [
        hm(Role.USER, "旧的 Phase 2 画像输入", phase=2, phase2_step="brief", history_seq=1),
        hm(
            Role.TOOL,
            None,
            phase=2,
            phase2_step="brief",
            history_seq=2,
            tool_result=ToolResult(
                tool_call_id="tc_old_p3",
                status="success",
                data={"trip_brief": "old phase3 brief"},
            ),
        ),
        hm(Role.USER, "Phase 3 发现预算不合适，回到框架规划", phase=3, history_seq=10),
        hm(
            Role.TOOL,
            None,
            phase=3,
            history_seq=11,
            tool_result=ToolResult(
                tool_call_id="tc_backtrack",
                status="success",
                data={"backtracked": True, "to_phase": 2, "reason": "预算不合适"},
            ),
        ),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=True,
        tool_engine=FakeToolEngine(),
    )

    assert [message.role for message in runtime] == [Role.USER, Role.USER]
    assert runtime[1].content == "Phase 3 发现预算不合适，回到框架规划"
    rendered = "\n".join(str(message.content) for message in runtime)
    assert "旧的 Phase 2 画像输入" in rendered


@pytest.mark.asyncio
async def test_restore_with_legacy_rows_drops_orphan_tool_results():
    plan = TravelPlanState(session_id="sess_4", phase=3, destination="首尔")
    history = [
        hm(Role.SYSTEM, "legacy system", history_seq=None),
        hm(Role.USER, "第一条旧用户消息", history_seq=None),
        hm(
            Role.TOOL,
            None,
            history_seq=None,
            tool_result=ToolResult(
                tool_call_id="tc_legacy",
                status="success",
                data={"legacy": True},
            ),
        ),
        hm(Role.USER, "最新用户消息", history_seq=None),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=False,
        tool_engine=FakeToolEngine(),
    )

    assert [message.role for message in runtime] == [Role.USER, Role.USER]
    assert runtime[-1].content == "最新用户消息"
    assert all(message.role != Role.SYSTEM for message in runtime)


@pytest.mark.asyncio
async def test_runtime_view_drops_old_epoch_orphan_tool_body():
    plan = TravelPlanState(
        session_id="sess_epoch_backtrack",
        phase=2,
        phase2_step="skeleton",
        destination="成都",
    )
    history = [
        hm(Role.USER, "第一次做框架", phase=2, phase2_step="skeleton", history_seq=9, context_epoch=2),
        hm(
            Role.TOOL,
            None,
            phase=2,
            phase2_step="skeleton",
            history_seq=10,
            context_epoch=2,
            tool_result=ToolResult(
                tool_call_id="tc_old",
                status="success",
                data={"secret_body": "OLD_EPOCH_TOOL_BODY"},
            ),
        ),
        hm(
            Role.SYSTEM,
            "backtrack notice",
            phase=2,
            phase2_step="skeleton",
            history_seq=20,
            context_epoch=4,
            rebuild_reason="backtrack",
        ),
        hm(Role.USER, "重做框架，少走路", phase=2, phase2_step="skeleton", history_seq=21, context_epoch=4),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=False,
        tool_engine=FakeToolEngine(),
    )

    prompt_text = "\n".join(str(message.content) for message in runtime if message.content)
    assert "OLD_EPOCH_TOOL_BODY" not in prompt_text
    assert all(message.tool_result is None for message in runtime)
    assert "重做框架，少走路" in prompt_text
    assert all(message.role != Role.SYSTEM for message in runtime)


@pytest.mark.asyncio
async def test_runtime_view_drops_earlier_phase2_step_orphan_tool_body():
    plan = TravelPlanState(
        session_id="sess_epoch_step",
        phase=2,
        phase2_step="skeleton",
        destination="成都",
    )
    history = [
        hm(
            Role.TOOL,
            None,
            phase=2,
            phase2_step="brief",
            history_seq=5,
            context_epoch=1,
            tool_result=ToolResult(
                tool_call_id="tc_brief",
                status="success",
                data={"brief_tool": "BRIEF_EPOCH_TOOL_BODY"},
            ),
        ),
        hm(
            Role.SYSTEM,
            "skeleton step handoff",
            phase=2,
            phase2_step="skeleton",
            history_seq=9,
            context_epoch=3,
            rebuild_reason="phase2_step_change",
        ),
        hm(Role.USER, "现在定骨架", phase=2, phase2_step="skeleton", history_seq=10, context_epoch=3),
    ]

    runtime = await build_runtime_view_for_restore(
        history_view=history,
        plan=plan,
        user_id="user_1",
        phase_router=FakePhaseRouter(),
        context_manager=FakeContextManager(),
        memory_mgr=FakeMemoryManager(),
        memory_enabled=False,
        tool_engine=FakeToolEngine(),
    )

    prompt_text = "\n".join(str(message.content) for message in runtime if message.content)
    assert "BRIEF_EPOCH_TOOL_BODY" not in prompt_text
    assert all(message.tool_result is None for message in runtime)
    assert "现在定骨架" in prompt_text
