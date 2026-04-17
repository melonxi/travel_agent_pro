# Phase Handoff 重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除阶段切换时的历史摘要与原始用户消息重放，改为“结构化状态 + 职责交接指引”的 phase handoff 机制。

**Architecture:** 保留 `build_system_message()` 作为结构化状态主注入路径，在 `ContextManager` 中新增确定性的 handoff note builder，并将 `AgentLoop._rebuild_messages_for_phase_change()` 改为 forward transition 只使用 `system + assistant handoff note`。`compress_for_transition()` 暂时保留实现，但退出 phase handoff 主路径。整个改动只触及 phase 切换链路和相关测试。

**Tech Stack:** Python 3.12, pytest, dataclasses, async agent loop

---

### Task 1: 新增 Handoff Note Builder

**Files:**
- Modify: `backend/context/manager.py`
- Test: `backend/tests/test_context_manager.py`

- [ ] **Step 1: 写 failing tests，覆盖 Phase 5 handoff note 的基础形状**

在 `backend/tests/test_context_manager.py` 新增以下测试：

```python
def test_build_phase_handoff_note_for_phase5(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=5,
        destination="若尔盖",
        dates=DateRange(start="2026-06-06", end="2026-06-10"),
        trip_brief={"goal": "第一次去草原", "pace": "intensive"},
        skeleton_plans=[{"id": "A", "days": ["D1", "D2"]}],
        selected_skeleton_id="A",
        accommodation=Accommodation(area="松潘+若尔盖", hotel="朵兰达+维也纳"),
        selected_transport={"outbound": "3U6992"},
    )

    note = ctx_manager.build_phase_handoff_note(
        plan=plan,
        from_phase=3,
        to_phase=5,
    )

    assert "[阶段交接]" in note
    assert "当前阶段：Phase 5" in note
    assert "已完成事项：" in note
    assert "目的地" in note
    assert "日期" in note
    assert "旅行画像" in note
    assert "已选骨架" in note
    assert "交通" in note
    assert "住宿" in note
    assert "当前唯一目标：基于已选骨架与住宿，生成覆盖全部出行日期的 daily_plans。" in note
    assert "不要重新锁交通" in note
    assert "request_backtrack(to_phase=3" in note


def test_build_phase_handoff_note_falls_back_when_no_completion_items(ctx_manager):
    plan = TravelPlanState(session_id="s1", phase=3)

    note = ctx_manager.build_phase_handoff_note(
        plan=plan,
        from_phase=1,
        to_phase=3,
    )

    assert "当前阶段：Phase 3" in note
    assert "系统已按当前规划状态切换到新阶段" in note
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
cd backend && pytest tests/test_context_manager.py::test_build_phase_handoff_note_for_phase5 -q
cd backend && pytest tests/test_context_manager.py::test_build_phase_handoff_note_falls_back_when_no_completion_items -q
```

Expected: FAIL，提示 `ContextManager` 没有 `build_phase_handoff_note`。

- [ ] **Step 3: 在 `backend/context/manager.py` 中实现最小版本的 handoff builder**

新增以下方法与辅助方法：

```python
    def build_phase_handoff_note(
        self,
        *,
        plan: TravelPlanState,
        from_phase: int,
        to_phase: int,
    ) -> str:
        phase_name = self._phase_display_name(to_phase)
        completed = self._handoff_completed_items(plan)
        completed_text = (
            f"已完成事项：{'、'.join(completed)}均已确认。"
            if completed
            else "已完成事项：系统已按当前规划状态切换到新阶段。"
        )
        return "\n".join(
            [
                "[阶段交接]",
                f"当前阶段：Phase {to_phase}（{phase_name}）。",
                completed_text,
                self._handoff_goal_line(to_phase),
                self._handoff_guardrail_line(to_phase),
            ]
        )

    def _phase_display_name(self, phase: int) -> str:
        names = {
            1: "目的地收敛",
            3: "行程框架规划",
            5: "逐日行程落地",
            7: "出发前查漏",
        }
        return names.get(phase, "阶段切换")

    def _handoff_completed_items(self, plan: TravelPlanState) -> list[str]:
        items: list[str] = []
        if plan.destination:
            items.append("目的地")
        if plan.dates:
            items.append("日期")
        if plan.trip_brief:
            items.append("旅行画像")
        if plan.selected_skeleton_id:
            items.append("已选骨架")
        if plan.selected_transport:
            items.append("交通")
        if plan.accommodation:
            items.append("住宿")
        if plan.daily_plans:
            items.append("部分逐日行程")
        return items

    def _handoff_goal_line(self, phase: int) -> str:
        mapping = {
            1: "当前唯一目标：帮助用户确认目的地，不进入交通、住宿或逐日行程。",
            3: "当前唯一目标：围绕已确认目的地完成旅行画像、候选筛选、骨架方案与锁定项。",
            5: "当前唯一目标：基于已选骨架与住宿，生成覆盖全部出行日期的 daily_plans。",
            7: "当前唯一目标：基于已确认行程做出发前查漏与准备清单，不重做规划。",
        }
        return mapping.get(phase, "当前唯一目标：按当前阶段职责继续推进。")

    def _handoff_guardrail_line(self, phase: int) -> str:
        mapping = {
            3: "禁止重复：不要回到目的地发散；若用户要求推翻前序决策，使用 `request_backtrack(...)`。",
            5: "禁止重复：不要重新锁交通、不要重新锁住宿、不要重选骨架；若前置状态不足或骨架不可执行，调用 `request_backtrack(to_phase=3, reason=\"...\")`。",
            7: "禁止重复：不要修改 `daily_plans`、不要重新选择交通或住宿；若发现严重问题，使用 `request_backtrack(...)`。",
        }
        return mapping.get(phase, "禁止重复：仅在当前阶段职责内行动。")
```

- [ ] **Step 4: 运行上下文管理测试，确认新增 builder 通过**

Run:

```bash
cd backend && pytest tests/test_context_manager.py::test_build_phase_handoff_note_for_phase5 -q
cd backend && pytest tests/test_context_manager.py::test_build_phase_handoff_note_falls_back_when_no_completion_items -q
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/context/manager.py backend/tests/test_context_manager.py
git commit -m "refactor: add deterministic phase handoff note builder"
```

---

### Task 2: 将 Forward Transition 从 Summary 改为 Handoff Note

**Files:**
- Modify: `backend/agent/loop.py`
- Test: `backend/tests/test_agent_loop.py`

- [ ] **Step 1: 写 failing tests，覆盖 forward transition 不再使用 summary 和 user replay**

在 `backend/tests/test_agent_loop.py` 为 `FakeContextManager` 增加 handoff builder stub：

```python
    def build_phase_handoff_note(self, *, plan, from_phase, to_phase) -> str:
        return f"handoff {from_phase}->{to_phase} phase={plan.phase}"
```

新增测试：

```python
@pytest.mark.asyncio
async def test_rebuild_messages_for_forward_phase_change_uses_handoff_note_not_summary(agent):
    agent.plan.phase = 5
    original = Message(role=Role.USER, content="航班 ok 的，住宿就朵兰达+维也纳")
    messages = [Message(role=Role.USER, content="旧消息")]

    rebuilt = await agent._rebuild_messages_for_phase_change(
        messages=messages,
        from_phase=3,
        to_phase=5,
        original_user_message=original,
        result=ToolResult(tool_call_id="", status="success"),
    )

    assert [m.role for m in rebuilt] == [Role.SYSTEM, Role.ASSISTANT]
    assert "handoff 3->5 phase=5" in rebuilt[1].content
    assert "summary 3->5" not in rebuilt[1].content
    assert all(m.content != "航班 ok 的，住宿就朵兰达+维也纳" for m in rebuilt)
```

保留 backtrack 场景测试，补一个断言：

```python
assert rebuilt[-1].role == Role.USER
```

- [ ] **Step 2: 运行测试确认失败**

Run:

```bash
cd backend && pytest tests/test_agent_loop.py::test_rebuild_messages_for_forward_phase_change_uses_handoff_note_not_summary -q
```

Expected: FAIL，因为当前代码还会调用 `compress_for_transition()` 并重放 user message。

- [ ] **Step 3: 修改 `_rebuild_messages_for_phase_change()` 实现新行为**

将 `backend/agent/loop.py` 中 forward transition 分支从：

```python
        else:
            summary = await self.context_manager.compress_for_transition(...)
            if summary:
                rebuilt.append(Message(role=Role.ASSISTANT, content=...))
            rebuilt.append(self._copy_message(original_user_message))
```

改为：

```python
        else:
            handoff_note = self.context_manager.build_phase_handoff_note(
                plan=self.plan,
                from_phase=from_phase,
                to_phase=to_phase,
            )
            rebuilt.append(
                Message(
                    role=Role.ASSISTANT,
                    content=handoff_note,
                )
            )
```

保留 backtrack 分支：

```python
        if to_phase < from_phase:
            rebuilt.append(self._copy_message(original_user_message))
```

- [ ] **Step 4: 运行 agent loop 相关测试确认通过**

Run:

```bash
cd backend && pytest tests/test_agent_loop.py::test_rebuild_messages_for_forward_phase_change_uses_handoff_note_not_summary -q
cd backend && pytest tests/test_agent_loop.py -q -k "rebuild_messages_for_phase_change or backtrack"
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/agent/loop.py backend/tests/test_agent_loop.py
git commit -m "refactor: replace phase transition summary with handoff note"
```

---

### Task 3: 让 `compress_for_transition()` 退出 Handoff 主路径并明确降级定位

**Files:**
- Modify: `backend/context/manager.py`
- Test: `backend/tests/test_context_manager.py`

- [ ] **Step 1: 补一条回归测试，确保 forward handoff 不再依赖 `compress_for_transition()`**

在 `backend/tests/test_agent_loop.py` 中补一条测试：

```python
@pytest.mark.asyncio
async def test_forward_transition_does_not_call_compress_for_transition(agent):
    called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("compress_for_transition should not be used")

    agent.context_manager.compress_for_transition = fail_if_called
    agent.context_manager.build_phase_handoff_note = lambda **kwargs: "handoff"
    agent.plan.phase = 5

    rebuilt = await agent._rebuild_messages_for_phase_change(
        messages=[Message(role=Role.USER, content="x")],
        from_phase=3,
        to_phase=5,
        original_user_message=Message(role=Role.USER, content="x"),
        result=ToolResult(tool_call_id="", status="success"),
    )

    assert not called
    assert rebuilt[1].content == "handoff"
```

- [ ] **Step 2: 在 `compress_for_transition()` 上加注释，标明它已退出主链路**

在 `backend/context/manager.py` 的 `compress_for_transition()` docstring 开头改成：

```python
    """Produce a deterministic summary of prior-phase context.

    Note: as of 2026-04-17, this function is no longer used by the primary
    phase handoff path. Phase transitions now rely on build_phase_handoff_note()
    plus the normal system runtime context.
    """
```

- [ ] **Step 3: 运行相关测试**

Run:

```bash
cd backend && pytest tests/test_agent_loop.py -q -k "does_not_call_compress_for_transition"
cd backend && pytest tests/test_context_manager.py -q
```

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add backend/context/manager.py backend/tests/test_agent_loop.py backend/tests/test_context_manager.py
git commit -m "docs: retire transition summary from primary handoff path"
```

---

### Task 4: 更新阶段切换事件与集成测试断言

**Files:**
- Modify: `backend/tests/test_phase_transition_event.py`
- Modify: `backend/tests/test_phase_integration.py`

- [ ] **Step 1: 找到依赖旧 summary 文案或旧消息形状的断言并改为新 handoff 断言**

重点将旧断言：

```python
assert "以下是阶段" in rebuilt_message.content
assert summary in rebuilt_message.content
```

改为：

```python
assert "[阶段交接]" in rebuilt_message.content
assert "当前唯一目标：" in rebuilt_message.content
```

如果测试之前假设 forward transition 后存在 user replay，则改为断言没有 replay：

```python
assert all(
    not (m.role == Role.USER and m.content == original_user_message.content)
    for m in rebuilt
)
```

- [ ] **Step 2: 运行相关测试确认行为对齐**

Run:

```bash
cd backend && pytest tests/test_phase_transition_event.py -q
cd backend && pytest tests/test_phase_integration.py -q
```

Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_phase_transition_event.py backend/tests/test_phase_integration.py
git commit -m "test: align phase transition assertions with handoff note flow"
```

---

### Task 5: 更新项目文档与总览

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新项目总览中的阶段转换机制描述**

在 `PROJECT_OVERVIEW.md` 中，把“阶段转换压缩：规则驱动，无额外 LLM 调用；格式为用户/决策/工具成功/助手四类条目”相关描述改为：

```md
2. **阶段转换交接**：前进切换时不再注入历史摘要，而是注入一条确定性的 handoff note，交代当前阶段、已完成事项、当前唯一目标和禁止重复事项；回退切换仍保留 backtrack notice + 原始用户消息。
```

- [ ] **Step 2: 运行无需测试，仅人工检查文案一致性**

检查点：
- 文档不再把 `compress_for_transition()` 描述为主链路
- 文档与 spec 保持一致

- [ ] **Step 3: Commit**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: document deterministic phase handoff flow"
```

---

### Task 6: 执行相关测试回归并整理结果

**Files:**
- No code changes required unless failures expose gaps

- [ ] **Step 1: 运行本次改动的相关测试集**

Run:

```bash
cd backend && pytest tests/test_context_manager.py -q
cd backend && pytest tests/test_agent_loop.py -q
cd backend && pytest tests/test_phase_transition_event.py -q
cd backend && pytest tests/test_phase_integration.py -q
```

Expected: 全部通过；若失败，只修与 handoff 重构直接相关的断言或实现。

- [ ] **Step 2: 记录结果并确认不跑全量测试**

在最终说明中明确写出：

```text
已按项目约定只执行相关测试，未跑全量 pytest，以避免无关耗时。
```

- [ ] **Step 3: Commit**

```bash
git status
```

如果 Step 1 没有新增代码改动，则本 Task 不需要 commit；如果为修测试失败做了相关修正，再单独提交：

```bash
git add <relevant files>
git commit -m "test: verify phase handoff redesign with targeted coverage"
```

---

## Self-Review Checklist

- Spec coverage:
  - 移除 transition summary：Task 2
  - 新增 handoff note：Task 1
  - forward transition 不再 replay user：Task 2
  - `compress_for_transition()` 退出主路径：Task 3
  - 相关测试更新：Task 1-4, 6
  - `PROJECT_OVERVIEW.md` 同步：Task 5
- Placeholder scan: 无 TBD / TODO / “自行实现” 类占位
- Type consistency:
  - 新增方法统一命名为 `build_phase_handoff_note`
  - 辅助方法统一前缀 `_handoff_*`

---

Plan complete and saved to `docs/superpowers/plans/2026-04-17-phase-handoff-redesign.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
