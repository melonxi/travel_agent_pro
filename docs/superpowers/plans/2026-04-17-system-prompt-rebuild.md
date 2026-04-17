# System Prompt Rebuild & Runtime Context Injection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Phase 3 子阶段切换不重建 system message 的 bug，并补齐 Phase 7 daily_plans / skeleton 摘要 / Phase 1 preferences-constraints 三处 runtime context 注入缺口。

**Architecture:** 在 `AgentLoop` 新增 `_rebuild_messages_for_phase3_step_change` 方法并在 loop 主循环 phase3_step 变化点调用；调整 `ContextManager.build_runtime_context` 的条件判断。最小改动，不拆分 builder。

**Tech Stack:** Python 3.12 / FastAPI / pytest / pydantic。

**对应 spec:** `docs/superpowers/specs/2026-04-17-system-prompt-rebuild-design.md`

---

## 工作区与基线

- Worktree：`.worktrees/sysmsg-rebuild`（分支 `system-prompt-rebuild-fix`），已基于 `main`（29148dd）创建。
- 基线测试：`pytest backend/tests/test_context_manager.py backend/tests/test_agent_loop.py backend/tests/test_phase_transition_event.py -q` → **75 passed**（已确认）。
- 运行所有后续测试的前缀（venv 已有）：

```bash
source backend/.venv/bin/activate
cd backend
python -m pytest tests/test_context_manager.py tests/test_agent_loop.py tests/test_phase_transition_event.py -x -q
```

---

## 文件结构

将被修改：
- `backend/context/manager.py` — 调整 `build_runtime_context` 中 daily_plans / skeleton_plans / preferences / constraints 注入条件；新增 skeleton 紧凑摘要生成辅助。
- `backend/agent/loop.py` — 新增 `_rebuild_messages_for_phase3_step_change`；在 phase3_step 变化分支处调用。
- `backend/tests/test_context_manager.py` — 新增 5 条测试。
- `backend/tests/test_agent_loop.py` — 新增 2 条测试。

不创建新文件。

---

## Task 1 — Phase 7 展示 daily_plans 每日活动

**Files:**
- Modify: `backend/context/manager.py:247`
- Test: `backend/tests/test_context_manager.py`

- [ ] **Step 1：写失败测试**

在 `backend/tests/test_context_manager.py` 末尾添加：

```python
def test_runtime_context_phase7_expands_daily_plans(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=7,
        dates=DateRange(start="2026-05-01", end="2026-05-02"),
        daily_plans=[
            DayPlan(
                day=1,
                date="2026-05-01",
                activities=[
                    Activity(
                        name="浅草寺",
                        start_time="09:00",
                        end_time="11:00",
                        location=Location(name="浅草寺"),
                        cost=0,
                    )
                ],
            ),
            DayPlan(
                day=2,
                date="2026-05-02",
                activities=[
                    Activity(
                        name="台场",
                        start_time="10:00",
                        end_time="12:00",
                        location=Location(name="台场"),
                        cost=0,
                    )
                ],
            ),
        ],
    )
    text = ctx_manager.build_runtime_context(plan)
    assert "第1天" in text
    assert "浅草寺" in text
    assert "第2天" in text
    assert "台场" in text
```

- [ ] **Step 2：运行测试确认失败**

```bash
python -m pytest tests/test_context_manager.py::test_runtime_context_phase7_expands_daily_plans -v
```
预期：FAIL（phase==7 时不展开活动名）。

- [ ] **Step 3：修改实现**

在 `backend/context/manager.py` 找到：

```python
            if plan.phase == 5:
```

改为：

```python
            if plan.phase in (5, 7):
```

（位置约第 247 行。同一 `if` 块后面的 "待规划天数" 分支保持原样，对 phase==7 同样触发。）

- [ ] **Step 4：运行测试确认通过**

```bash
python -m pytest tests/test_context_manager.py::test_runtime_context_phase7_expands_daily_plans -v
```
预期：PASS。

---

## Task 2 — Preferences / Constraints 一律注入

**Files:**
- Modify: `backend/context/manager.py:228-241`
- Test: `backend/tests/test_context_manager.py`

- [ ] **Step 1：写失败测试（两条）**

在测试文件末尾追加：

```python
def test_runtime_context_preferences_injected_phase1(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=1,
        preferences=[Preference(key="饮食", value="素食")],
    )
    text = ctx_manager.build_runtime_context(plan)
    assert "用户偏好" in text
    assert "饮食: 素食" in text


def test_runtime_context_constraints_injected_phase1(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=1,
        constraints=[Constraint(type="health", description="低血糖，需规律进食")],
    )
    text = ctx_manager.build_runtime_context(plan)
    assert "用户约束" in text
    assert "低血糖" in text
```

- [ ] **Step 2：运行测试确认失败**

```bash
python -m pytest tests/test_context_manager.py::test_runtime_context_preferences_injected_phase1 tests/test_context_manager.py::test_runtime_context_constraints_injected_phase1 -v
```
预期：两条 FAIL。

- [ ] **Step 3：修改实现**

在 `backend/context/manager.py` 替换 preferences/constraints 块（约 228-241 行）：

原代码：

```python
        # Phase 3 later sub-stages & Phase 5+: inject preferences and constraints
        if plan.preferences and (
            plan.phase >= 5
            or (plan.phase == 3 and plan.phase3_step in ("skeleton", "lock"))
        ):
            pref_strs = [f"{p.key}: {p.value}" for p in plan.preferences if p.key]
            if pref_strs:
                parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")
        if plan.constraints and (
            plan.phase >= 5
            or (plan.phase == 3 and plan.phase3_step in ("skeleton", "lock"))
        ):
            cons_strs = [f"[{c.type}] {c.description}" for c in plan.constraints]
            if cons_strs:
                parts.append(f"- 用户约束：{'; '.join(cons_strs)}")
```

改为：

```python
        # Preferences / constraints 一律注入（任何阶段都有价值，体积小）
        if plan.preferences:
            pref_strs = [f"{p.key}: {p.value}" for p in plan.preferences if p.key]
            if pref_strs:
                parts.append(f"- 用户偏好：{'; '.join(pref_strs)}")
        if plan.constraints:
            cons_strs = [f"[{c.type}] {c.description}" for c in plan.constraints]
            if cons_strs:
                parts.append(f"- 用户约束：{'; '.join(cons_strs)}")
```

- [ ] **Step 4：运行测试确认通过**

```bash
python -m pytest tests/test_context_manager.py -k "preferences_injected or constraints_injected" -v
```
预期：两条 PASS。

---

## Task 3 — Skeleton 子阶段展示紧凑摘要

**Files:**
- Modify: `backend/context/manager.py:189-211`
- Test: `backend/tests/test_context_manager.py`

- [ ] **Step 1：写失败测试（两条）**

```python
def test_runtime_context_skeleton_step_shows_summary(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="skeleton",
        skeleton_plans=[
            {
                "id": "skeleton_01",
                "name": "经典版",
                "tradeoffs": "稳妥，适合首访",
                "days": [
                    {"theme": "老城徒步"},
                    {"theme": "峡谷日游"},
                ],
            },
            {
                "id": "skeleton_02",
                "name": "深度版",
                "tradeoffs": "节奏慢，可深挖",
                "days": [
                    {"theme": "美术馆"},
                    {"theme": "郊外温泉"},
                ],
            },
        ],
    )
    text = ctx_manager.build_runtime_context(plan)
    assert "skeleton_01" in text
    assert "经典版" in text
    assert "稳妥" in text
    assert "老城徒步" in text
    assert "skeleton_02" in text
    assert "美术馆" in text


def test_runtime_context_skeleton_lock_still_shows_selected_full(ctx_manager):
    plan = TravelPlanState(
        session_id="s1",
        phase=3,
        phase3_step="lock",
        selected_skeleton_id="skeleton_01",
        skeleton_plans=[
            {
                "id": "skeleton_01",
                "name": "经典版",
                "tradeoffs": "稳妥",
                "days": [{"theme": "老城徒步"}],
                "extra_field": "detail_value",
            },
            {
                "id": "skeleton_02",
                "name": "深度版",
                "tradeoffs": "慢",
                "days": [{"theme": "美术馆"}],
            },
        ],
    )
    text = ctx_manager.build_runtime_context(plan)
    # lock 展开 selected 完整 dict
    assert "已选骨架方案" in text
    assert "extra_field" in text
    assert "detail_value" in text
    # lock 下不应再给 skeleton_02 展开摘要（避免重复/噪声）
    assert "skeleton_02" not in text or text.count("skeleton_02") <= 1
```

- [ ] **Step 2：运行测试确认失败**

```bash
python -m pytest tests/test_context_manager.py -k "skeleton_step_shows_summary or skeleton_lock_still_shows_selected_full" -v
```
预期：第一条 FAIL（skeleton 步看不到摘要），第二条可能 PASS（不变更语义也能过，但作为回归保护）。

- [ ] **Step 3：修改实现（替换 skeleton_plans 注入块）**

在 `backend/context/manager.py` 找到（约 191-211 行）：

```python
        # Phase 5+: inject selected skeleton full content
        # Phase 3 lock: also inject selected skeleton content
        if plan.skeleton_plans:
            inject_skeleton = (plan.phase >= 5 and plan.selected_skeleton_id) or (
                plan.phase == 3
                and plan.phase3_step == "lock"
                and plan.selected_skeleton_id
            )
            if inject_skeleton:
                selected = self._find_selected_skeleton(plan)
                if selected:
                    parts.append(f"- 已选骨架方案（{plan.selected_skeleton_id}）：")
                    for key, val in selected.items():
                        if key == "id":
                            continue
                        parts.append(f"  - {key}: {val}")
                else:
                    parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
                    parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
            else:
                parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
                if plan.selected_skeleton_id:
                    parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
```

替换为：

```python
        # Phase 5+: inject selected skeleton full content
        # Phase 3 lock: inject selected skeleton full content
        # Phase 3 skeleton: inject compact summary (id / name / tradeoffs / day themes)
        if plan.skeleton_plans:
            inject_full_selected = (
                (plan.phase >= 5 and plan.selected_skeleton_id)
                or (plan.phase == 3 and plan.phase3_step == "lock" and plan.selected_skeleton_id)
            )
            show_summary_list = (
                plan.phase == 3 and plan.phase3_step == "skeleton"
            )
            if inject_full_selected:
                selected = self._find_selected_skeleton(plan)
                if selected:
                    parts.append(f"- 已选骨架方案（{plan.selected_skeleton_id}）：")
                    for key, val in selected.items():
                        if key == "id":
                            continue
                        parts.append(f"  - {key}: {val}")
                else:
                    parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
                    parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
            elif show_summary_list:
                parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
                for sk in plan.skeleton_plans:
                    if not isinstance(sk, dict):
                        continue
                    sid = sk.get("id") or sk.get("name") or "?"
                    name = sk.get("name") or sk.get("title") or ""
                    tradeoffs = sk.get("tradeoffs") or sk.get("tradeoff") or ""
                    header_parts = [f"[id={sid}]"]
                    if name:
                        header_parts.append(f"名称：{name}")
                    if tradeoffs:
                        header_parts.append(f"权衡：{tradeoffs}")
                    parts.append("  - " + " | ".join(header_parts))
                    days = sk.get("days") or []
                    if isinstance(days, list):
                        for idx, day in enumerate(days, start=1):
                            if not isinstance(day, dict):
                                continue
                            theme = day.get("theme") or day.get("title") or f"第{idx}天"
                            parts.append(f"    - D{idx}: {theme}")
            else:
                parts.append(f"- 骨架方案：{len(plan.skeleton_plans)} 套")
                if plan.selected_skeleton_id:
                    parts.append(f"- 已选骨架：{plan.selected_skeleton_id}")
```

- [ ] **Step 4：运行测试确认通过**

```bash
python -m pytest tests/test_context_manager.py -k "skeleton_step_shows_summary or skeleton_lock_still_shows_selected_full" -v
```
预期：两条 PASS。

- [ ] **Step 5：完整跑 test_context_manager.py，确保无回归**

```bash
python -m pytest tests/test_context_manager.py -q
```
预期：所有测试 PASS。

---

## Task 4 — AgentLoop：Phase 3 子阶段切换时重建 system message

**Files:**
- Modify: `backend/agent/loop.py:496-506`（调用点）
- Modify: `backend/agent/loop.py`（新增 `_rebuild_messages_for_phase3_step_change` 方法，置于 `_rebuild_messages_for_phase_change` 之后）
- Test: `backend/tests/test_agent_loop.py`

- [ ] **Step 1：写失败测试**

在 `backend/tests/test_agent_loop.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_phase3_step_change_rebuilds_system_message():
    """子阶段从 brief 推进到 candidate 时，system message 必须被重建。"""
    plan = TravelPlanState(session_id="s1", phase=3, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_system_contents: list[str] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        # 记录每轮看到的 system message 内容
        for m in messages:
            if m.role == Role.SYSTEM:
                observed_system_contents.append(m.content)
                break
        if call_count == 1:
            # 写 dates，触发 phase3_step: brief -> candidate
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_trip_basics",
                    arguments={"dates": {"start": "2026-05-01", "end": "2026-05-05"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="继续")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u-step",
    )

    messages = [Message(role=Role.USER, content="五一去东京玩5天")]
    async for _ in agent.run(messages, phase=3):
        pass

    assert plan.phase3_step == "candidate"
    # 两次推理分别看到 system message，且内容不同（第二次反映新 phase3_step / 新 tools）
    assert len(observed_system_contents) >= 2
    assert observed_system_contents[0] != observed_system_contents[1]
    # 第二次 system 不应混入跨 phase handoff 文案
    assert "已完成 Phase" not in observed_system_contents[1]
    assert "handoff" not in observed_system_contents[1]


@pytest.mark.asyncio
async def test_phase3_step_change_no_handoff_note():
    """phase3_step 变化重建时不得注入跨 phase handoff assistant note。"""
    plan = TravelPlanState(session_id="s1", phase=3, destination="东京")
    engine = ToolEngine()
    register_all_plan_tools(engine, plan)

    observed_messages: list[list[Message]] = []
    call_count = 0

    async def fake_chat(messages, tools=None, stream=True):
        nonlocal call_count
        call_count += 1
        observed_messages.append(list(messages))
        if call_count == 1:
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc1",
                    name="update_trip_basics",
                    arguments={"dates": {"start": "2026-05-01", "end": "2026-05-05"}},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
            return
        yield LLMChunk(type=ChunkType.TEXT_DELTA, content="ok")
        yield LLMChunk(type=ChunkType.DONE)

    llm = MagicMock()
    llm.chat = fake_chat
    agent = AgentLoop(
        llm=llm,
        tool_engine=engine,
        hooks=HookManager(),
        max_retries=3,
        phase_router=PhaseRouter(),
        context_manager=FakeContextManager(),
        plan=plan,
        llm_factory=lambda: MagicMock(),
        memory_mgr=FakeMemoryManager(),
        user_id="u-step2",
    )
    async for _ in agent.run(
        [Message(role=Role.USER, content="定档")], phase=3
    ):
        pass

    # 第二次推理：不得出现 handoff assistant 消息
    second_round = observed_messages[1]
    for m in second_round:
        if m.role == Role.ASSISTANT and m.content:
            assert "handoff" not in m.content
            assert "已完成 Phase" not in m.content
```

- [ ] **Step 2：运行测试确认失败**

```bash
python -m pytest tests/test_agent_loop.py::test_phase3_step_change_rebuilds_system_message tests/test_agent_loop.py::test_phase3_step_change_no_handoff_note -v
```
预期：`test_phase3_step_change_rebuilds_system_message` FAIL（第二次 system 与第一次相同，因为现在只刷新 tools）；第二条可能 PASS，作为后续回归保护。

- [ ] **Step 3：新增 `_rebuild_messages_for_phase3_step_change` 方法**

在 `backend/agent/loop.py` 中 `_rebuild_messages_for_phase_change` 方法之后（约第 588 行之后）插入：

```python
    async def _rebuild_messages_for_phase3_step_change(
        self,
        messages: list[Message],
        original_user_message: Message,
    ) -> list[Message]:
        """Rebuild system message only when phase3_step changes within phase 3.

        Unlike phase change rebuild:
        - 不插入跨 phase handoff note（语义不同，避免误导 LLM）
        - 不插入 backtrack notice（子阶段单向推进）
        - 保留 original_user_message（当前用户意图仍在闭环中）
        """
        if (
            self.phase_router is None
            or self.context_manager is None
            or self.plan is None
            or self.memory_mgr is None
        ):
            raise RuntimeError(
                "Phase3 step rebuild requires router/context/plan/memory"
            )

        phase_prompt = self.phase_router.get_prompt_for_plan(self.plan)
        memory_context, _recalled_ids, *_ = (
            await self.memory_mgr.generate_context(self.user_id, self.plan)
            if self.memory_enabled
            else ("暂无相关用户记忆", [], 0, 0, 0)
        )
        return [
            self.context_manager.build_system_message(
                self.plan,
                phase_prompt,
                memory_context,
                available_tools=self._current_tool_names(self.plan.phase),
            ),
            self._copy_message(original_user_message),
        ]
```

- [ ] **Step 4：改调用点 — `loop.py:496-506`**

找到：

```python
                    phase3_step_after_batch = (
                        getattr(self.plan, "phase3_step", None)
                        if self.plan is not None
                        else None
                    )
                    if phase3_step_after_batch != phase3_step_before_batch:
                        phase_changed_in_prev_iteration = True
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )
```

替换为：

```python
                    phase3_step_after_batch = (
                        getattr(self.plan, "phase3_step", None)
                        if self.plan is not None
                        else None
                    )
                    if phase3_step_after_batch != phase3_step_before_batch:
                        phase_changed_in_prev_iteration = True
                        messages[:] = await self._rebuild_messages_for_phase3_step_change(
                            messages=messages,
                            original_user_message=original_user_message,
                        )
                        tools = self.tool_engine.get_tools_for_phase(
                            current_phase,
                            self.plan,
                        )
```

- [ ] **Step 5：运行新测试确认通过**

```bash
python -m pytest tests/test_agent_loop.py::test_phase3_step_change_rebuilds_system_message tests/test_agent_loop.py::test_phase3_step_change_no_handoff_note -v
```
预期：两条 PASS。

- [ ] **Step 6：跑完整 test_agent_loop.py 确保无回归**

```bash
python -m pytest tests/test_agent_loop.py -q
```
预期：全部 PASS。

---

## Task 5 — 最终回归与提交

- [ ] **Step 1：跑三文件总回归**

```bash
python -m pytest tests/test_context_manager.py tests/test_agent_loop.py tests/test_phase_transition_event.py -q
```
预期：**82 passed**（原 75 + 新 7）。

- [ ] **Step 2：更新 PROJECT_OVERVIEW.md**

在 `PROJECT_OVERVIEW.md` 找到涉及 Phase 3 子阶段流转 / system message 重建的小节（多半在 "AgentLoop" 或 "Context 注入" 相关段落），追加一行：

> 2026-04-17：Phase 3 子阶段变化（brief→candidate→skeleton→lock）会触发 system message 重建；runtime context 在 Phase 7 展开 daily_plans，在 Phase 3 skeleton 子阶段展开骨架紧凑摘要；preferences / constraints 自 Phase 1 起即注入。

若找不到对应小节，则在"关键设计决策"段落末尾追加同样文字。

- [ ] **Step 3：提交**

```bash
git add backend/context/manager.py \
        backend/agent/loop.py \
        backend/tests/test_context_manager.py \
        backend/tests/test_agent_loop.py \
        docs/superpowers/specs/2026-04-17-system-prompt-rebuild-design.md \
        docs/superpowers/plans/2026-04-17-system-prompt-rebuild.md \
        PROJECT_OVERVIEW.md

git commit -m "fix: rebuild system message on phase3 substep change & expand runtime context

- AgentLoop: 新增 _rebuild_messages_for_phase3_step_change，phase3_step 推进（brief→candidate→skeleton→lock）时重建 system message，避免模型看到陈旧 runtime context
- ContextManager: Phase 7 展开 daily_plans 每日活动；Phase 3 skeleton 子阶段展开骨架紧凑摘要（id/name/tradeoffs/每天 theme）；preferences/constraints 一律注入（含 Phase 1）
- tests: 新增 7 条针对性测试覆盖上述行为
"
```

- [ ] **Step 4：git status 确认干净**

```bash
git status
```
预期：`nothing to commit, working tree clean`。

---

## Self-Review 清单

- 覆盖 spec 第 2 节全部 4 条调整 ✅（Task 1 对应 Phase 7、Task 2 对应 preferences/constraints、Task 3 对应 skeleton、Task 4 对应重建时机）
- 无 TBD / "类似于 Task N" / 空泛 "添加合适的错误处理"
- 类型/方法名一致：`_rebuild_messages_for_phase3_step_change`、`build_runtime_context`、`build_system_message`（沿用现有签名）
- 所有 bash 命令含明确预期输出
