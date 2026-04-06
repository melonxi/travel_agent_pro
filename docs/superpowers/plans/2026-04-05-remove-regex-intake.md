# 移除正则预提取，统一由 LLM 驱动字段写入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删除 chat 入口处的正则预提取与提前 phase 跳转，让字段写入统一经过 `update_plan_state`，并让 `AgentLoop` 在同一轮对话里感知 phase 变化、立刻刷新上下文和工具集。

**Architecture:** `main.py` 只负责会话、SSE 和首次消息入队，不再直接写 plan 字段。`AgentLoop` 在每次 tool call 后比较 `plan.phase` 是否变化；前进时调用 `ContextManager.compress_for_transition()` 生成摘要或短路保留原消息，回退时走硬边界重建，只保留新的 system message、回退说明和原始用户消息。`_build_agent()` 改为注入 `phase_router`、`context_manager`、`plan`、`llm_factory`、`memory_mgr`、`user_id`，其中 `llm_factory` 建议用 `lambda: create_llm_provider(config.llm)`，`user_id` 需要存回 session，保证 backtrack/rebuild 后仍能加载同一份用户画像。

**Tech Stack:** Python 3.11+, FastAPI, pytest, pytest-asyncio

**Spec:** `docs/superpowers/specs/2026-04-05-remove-regex-intake-design.md`

---

## File Structure

```text
backend/agent/
    loop.py                         # MODIFY — phase-aware tool execution, context rebuild, original_user_message anchor
backend/context/
    manager.py                      # MODIFY — add compress_for_transition()
backend/main.py                     # MODIFY — remove apply_trip_facts path, inject new AgentLoop deps, persist session user_id
backend/state/
    intake.py                       # MODIFY — deprecate apply_trip_facts/extract_trip_facts, keep parse helpers
backend/tests/
    test_agent_loop.py              # MODIFY — update constructor helpers, keep baseline behavior tests green
    test_context_manager.py         # MODIFY — add compress_for_transition coverage
    test_api.py                     # MODIFY — replace regex preload assertion with tool-driven expectation
    test_e2e_golden_path.py         # MODIFY — stop depending on apply_trip_facts, drive state through mocked tool calls
    test_telemetry_agent_loop.py    # MODIFY — update AgentLoop constructor setup
    test_phase_integration.py       # MODIFY — add/adjust immediate phase-rebuild coverage if needed
```

---

## Task 1: 为 phase 切换摘要新增 ContextManager 能力

**Files:**
- Modify: `backend/context/manager.py`
- Modify: `backend/tests/test_context_manager.py`

- [ ] **Step 1: 先写 `compress_for_transition()` 的失败测试**

在 `backend/tests/test_context_manager.py` 中补 3 组用例：
- 非 system 消息少于 4 条时，直接拼接原文返回，不调用 LLM。
- 正常 phase 前进时，调用 `llm_factory()` 创建压缩 LLM，并保留用户偏好、约束、关键决策。
- 传入包含 system/user/assistant/tool 的 messages 时，只摘要非 system 内容。

建议断言：

```python
summary = await ctx_manager.compress_for_transition(
    messages=messages,
    from_phase=1,
    to_phase=3,
    llm_factory=fake_factory,
)
assert "预算" in summary
assert factory_called == 1
```

- [ ] **Step 2: 在 `ContextManager` 中实现 `compress_for_transition()`**

实现要点：
- 筛掉 system 消息，只处理 user/assistant/tool。
- `< 4` 条非 system 消息时短路，不走 LLM。
- LLM 路径通过 `llm_factory()` 创建实例，并用一段固定压缩 prompt 要求保留偏好、约束、已确认决策、未完成事项。
- 返回纯字符串摘要，供 `AgentLoop` 直接塞回新的 system message 序列。

- [ ] **Step 3: 跑 context 相关测试**

Run: `cd backend && pytest tests/test_context_manager.py -v`

- [ ] **Step 4: 提交 Task 1**

```bash
git add backend/context/manager.py backend/tests/test_context_manager.py
git commit -m "feat: add phase transition compression to context manager"
```

---

## Task 2: 让 AgentLoop 在每个 tool call 后立即感知 phase 变化

**Files:**
- Modify: `backend/agent/loop.py`
- Modify: `backend/tests/test_agent_loop.py`
- Modify: `backend/tests/test_telemetry_agent_loop.py`

- [ ] **Step 1: 先写 AgentLoop phase-aware 行为测试**

在 `backend/tests/test_agent_loop.py` 中新增覆盖：
- 第一个 tool call 触发 `plan.phase` 变化后，当前批次剩余 tool calls 不再执行。
- 正常前进时，第二次 `llm.chat()` 收到的是新 phase 的 tools 和重建后的 messages。
- backtrack 时不调用压缩 LLM，只保留 system + 回退说明 + 原始用户消息。
- `original_user_message` 会跨多次重建保持不变。

建议用一个假 `phase_router`、假 `context_manager`、假 `memory_mgr` 和可变 `plan.phase` 来断言调用顺序，而不是依赖真实路由逻辑。

- [ ] **Step 2: 扩展 `AgentLoop.__init__` 注入依赖**

构造函数改为接收：

```python
class AgentLoop:
    def __init__(
        self,
        llm,
        tool_engine,
        hooks,
        max_retries,
        phase_router,
        context_manager,
        plan,
        llm_factory,
        memory_mgr,
        user_id,
    ):
```

- [ ] **Step 3: 在 `run()` 中加入逐 tool call phase 检测和上下文重建**

实现要点：
- loop 开始前提取最后一条 user 消息为 `original_user_message`。
- 每执行完一个 tool call 后记录 `phase_before_tool` 与当前 `plan.phase`。
- 发现变化后立刻中断当前批次剩余 tool calls。
- 正常前进时调用 `context_manager.compress_for_transition(...)`。
- backtrack 时构建硬边界消息，不注入旧阶段摘要。
- 每次重建后重新取 `phase_router.get_prompt(plan.phase)`、`memory_mgr.load(user_id)`、`memory_mgr.generate_summary(memory)`，再调用 `context_manager.build_system_message(...)`。
- 同步刷新 `tools = self.tool_engine.get_tools_for_phase(plan.phase)`，然后进入下一轮迭代。

- [ ] **Step 4: 修正直接实例化 `AgentLoop` 的现有测试**

`test_agent_loop.py`、`test_telemetry_agent_loop.py` 目前只传 4 个参数。更新它们的 fixture/局部构造，提供最小 fake 依赖，避免因构造函数变化导致无意义失败。

- [ ] **Step 5: 跑 agent loop 相关测试**

Run: `cd backend && pytest tests/test_agent_loop.py tests/test_telemetry_agent_loop.py -v`

- [ ] **Step 6: 提交 Task 2**

```bash
git add backend/agent/loop.py backend/tests/test_agent_loop.py backend/tests/test_telemetry_agent_loop.py
git commit -m "feat: rebuild agent loop context immediately on phase changes"
```

---

## Task 3: 删除 chat 入口正则写入，改由 AgentLoop 注入新依赖

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/state/intake.py`
- Modify: `backend/tests/test_api.py`

- [ ] **Step 1: 先改 API 级测试，去掉 regex preload 预期**

`backend/tests/test_api.py::test_chat_preloads_explicit_trip_facts_without_tool_call` 需要重写，因为它验证的是将被删除的行为。

改为验证：
- chat 请求不会因为纯文本消息自动写入 `destination/dates/budget`。
- 若 agent 在 run 过程中通过 `update_plan_state` 写入字段，最终 `GET /api/plan/{session_id}` 能看到更新结果。

- [ ] **Step 2: 修改 `_build_agent()` 和 session 结构**

在 `backend/main.py` 中：
- 删除 `apply_trip_facts` 导入和调用。
- `_build_agent(plan, user_id)` 传入 `phase_router`、`context_mgr`、`plan`、`llm_factory`、`memory_mgr`、`user_id`。
- `create_session()` 初始化 session 时增加 `"user_id": "default_user"`。
- `chat()` 收到请求后先把 `session["user_id"] = req.user_id`，然后在需要 rebuild 时使用这个值创建新 agent。
- `backtrack()` rebuild agent 时也要复用 `session["user_id"]`。

- [ ] **Step 3: 清理 `state/intake.py` 的职责**

保留：
- `parse_dates_value`
- `parse_budget_value`

处理方式：
- `apply_trip_facts`、`extract_trip_facts` 标记为 deprecated，或删除其导出路径上的使用注释。
- `_extract_destination`、`_extract_budget_text` 可保留但明确注明“仅遗留，不再用于 chat intake”。

- [ ] **Step 4: 跑 API 基础测试**

Run: `cd backend && pytest tests/test_api.py -v`

- [ ] **Step 5: 提交 Task 3**

```bash
git add backend/main.py backend/state/intake.py backend/tests/test_api.py
git commit -m "refactor: remove regex intake from chat entrypoint"
```

---

## Task 4: 更新端到端与 phase 集成测试，覆盖新行为

**Files:**
- Modify: `backend/tests/test_e2e_golden_path.py`
- Modify: `backend/tests/test_phase_integration.py`

- [ ] **Step 1: 重写 golden path 对 phase 推进的前提**

`backend/tests/test_e2e_golden_path.py` 当前依赖 `apply_trip_facts` 在 chat 入口预填目的地、日期、预算。改为：
- mock LLM 先调用 `update_plan_state(destination=...)`
- 因 phase 变化中断批次并重进 loop
- 下一轮再调用 `update_plan_state(dates=...)`
- budget 在 phase 4 下写入但不触发 phase 变化

验证点：
- plan 字段最终由 tool 写入，而不是 chat 入口写入。
- phase 变化顺序符合当前 `infer_phase()` 真实逻辑。

- [ ] **Step 2: 为“同轮立即生效”补集成覆盖**

在 `backend/tests/test_phase_integration.py` 新增或调整一个用例，模拟同一轮 LLM 产出多个 tool calls，其中第一个 call 改变 phase。断言：
- 后续 call 没有在旧 phase 下执行。
- 新 phase prompt/tool 集已经用于下一轮 LLM 决策。

- [ ] **Step 3: 为 backtrack 硬边界补测试**

增加一个回退用例：
- phase 4 调用 `update_plan_state(field="backtrack", value={"to_phase": 2, ...})`
- 下一轮上下文不包含旧住宿/日期摘要
- `preferences`/`constraints` 仍保留在 plan 中

- [ ] **Step 4: 跑定向回归**

Run: `cd backend && pytest tests/test_e2e_golden_path.py tests/test_phase_integration.py -v`

- [ ] **Step 5: 提交 Task 4**

```bash
git add backend/tests/test_e2e_golden_path.py backend/tests/test_phase_integration.py
git commit -m "test: cover in-loop phase rebuild without regex intake"
```

---

## Task 5: 全量验证与收尾

- [ ] **Step 1: 跑后端全量测试**

Run: `cd backend && pytest`

- [ ] **Step 2: 手动验证 SSE 行为未回归**

最少验证 2 条路径：
- 用户一句话给出目的地+日期+预算时，SSE 仍持续输出，最终 `state_update` 反映 LLM 写入后的 plan。
- 用户在 phase 4 说“换个目的地”，本轮结束后 plan 已回退，不需要下一次 chat 才生效。

- [ ] **Step 3: 对照 spec 做最终核查**

确认以下条目都落地：
- chat 入口不再调用 `apply_trip_facts`
- phase 变化按 tool call 粒度立即生效
- forward transition 有摘要短路逻辑
- backtrack 使用硬上下文边界
- `parse_dates_value` / `parse_budget_value` 仍可供 `update_plan_state` 使用

- [ ] **Step 4: 最终提交**

```bash
git add backend
git commit -m "feat: move intake state extraction fully into llm-driven plan updates"
```
