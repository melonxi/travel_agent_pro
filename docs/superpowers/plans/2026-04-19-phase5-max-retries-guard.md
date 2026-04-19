# Phase 5 并行路由「最后一轮热切换」边界兜底实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]` / `- [x]`) syntax for tracking.

**Goal:** 在 `AgentLoop.run()` 的 safety-limit yield 之前加一次 `should_use_parallel_phase5` 兜底，使"最后一轮写入工具刚好把 phase 推进到 5"的边界场景也能收回到 Phase 5 并行 orchestrator，消除 `6d17027` 留下的非严格等价行为差异。

**Architecture:** 沿用 `6d17027` 的"守卫在 runner 边界做一次"的原则。在循环顶部守卫之外新增循环末尾兜底守卫，复用同一个 `should_use_parallel_phase5` 谓词；加一条 `max_retries=1` 的动态行为单元用例。

**Tech Stack:** 无新依赖，只改 `backend/agent/loop.py` 和 `backend/tests/test_loop_phase5_routing.py`。

**Status:** 本计划所有任务已执行完毕（追溯补档）。勾选的 checkbox 对应的 commit 为 `21792af`。预备工作 e2e 修复对应 `0899ebb`，不属本计划范围但与本计划同批提交。

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `backend/agent/loop.py` | 修改 | 在 safety-limit yield 前加一段 `should_use_parallel_phase5` 兜底 |
| `backend/tests/test_loop_phase5_routing.py` | 修改 | 新增 `max_retries=1` 动态边界用例（7 个测试变 8 个） |
| `docs/superpowers/specs/2026-04-19-phase5-max-retries-guard-design.md` | 新增 | 本次改动的追溯设计文档 |
| `docs/superpowers/plans/2026-04-19-phase5-max-retries-guard.md` | 新增 | 本文档本身 |

本计划**不**包含以下预备清理（见 commit `0899ebb`，与本计划同批推到分支）：

- `backend/tests/test_e2e_golden_path.py`：对齐新 handoff 文案 + 6 天 `daily_plans` + 禁用 parallel + 屏蔽 `on_soft_judge`。属于 baseline 污染清理。

---

### Task 1: 新增单元测试，确认当前（不含兜底）行为失败

**Files:**
- Modify: `backend/tests/test_loop_phase5_routing.py`

- [x] **Step 1: 在文件顶部补全 imports**

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.hooks import HookManager
from agent.loop import AgentLoop
from agent.types import Message, Role, ToolCall
from config import Phase5ParallelConfig
from llm.types import ChunkType, LLMChunk
from tools.base import tool
from tools.engine import ToolEngine
```

静态谓词测试原本只需要 `AgentLoop` 和 `Phase5ParallelConfig`，新增动态用例需要 loop 的完整依赖。

- [x] **Step 2: 定义最小化测试替身**

在新 class 外面、`TestPhase5Routing` 之后加：

```python
class _PromotingRouter:
    def get_prompt(self, phase): ...
    def get_prompt_for_plan(self, plan): ...
    async def check_and_apply_transition(self, plan, hooks=None):
        if plan.phase == 3:
            plan.phase = 5
            return True
        return False


class _StubContextManager:
    def build_system_message(self, plan, phase_prompt, memory_context="", available_tools=None): ...
    def build_phase_handoff_note(self, *, plan, from_phase, to_phase): ...


class _StubMemoryManager:
    async def generate_context(self, user_id, plan):
        return "", [], 0, 0, 0
```

为什么不复用 `test_agent_loop.py` 里的 Fake 系列？那些替身承载的是 serial 流测试契约，动作更多（compress、handoff 文案具体格式等），对本测试是多余复杂度。本用例只关心：phase 能被 router 改到 5 + context manager 不爆。

- [x] **Step 3: 写 `test_parallel_orchestrator_fires_after_final_iteration_phase_promotion`**

关键点：

- `plan.phase = 3`，但 `dates` / `selected_skeleton_id` / `skeleton_plans` / `accommodation` 预填好，使得一旦 phase → 5，`should_use_parallel_phase5` 的其它条件立即满足。
- 注册一个 `@tool(name="set_accommodation", ...)` 的空壳工具。名字必须在 `PLAN_WRITER_TOOL_NAMES` 里——loop 内部 `tc.name in PLAN_WRITER_TOOL_NAMES` 才会置 `saw_state_update=True`，进而调用 router。扩大这个集合风险更大，改掉测试 tool 名字更安全。
- `max_retries=1` 强制进入边界。
- `agent._run_parallel_phase5_orchestrator = _fake_orchestrator`，`_fake_orchestrator` 把本地 `orchestrator_fired` 置 True 并 yield 一个 DONE。
- 断言：`orchestrator_fired is True` AND `not any(chunk.content contains "达到最大循环次数")`。

- [x] **Step 4: 运行测试确认"如果没有兜底，该测试失败"**

此时 `loop.py` 还未加兜底守卫。运行 `pytest tests/test_loop_phase5_routing.py -v` 应看到 1 个 FAIL：orchestrator 从未被调用（因为 phase=3 的那次 iteration 里 router 在 `continue` 之后，循环直接结束走 safety-limit）。

（实际落地时，本步骤与 Task 2 合并——先写测试再实现，第一次 `pytest` 就是 FAIL，然后加兜底守卫，再 PASS。）

---

### Task 2: 在 `AgentLoop.run()` 末尾加兜底守卫

**Files:**
- Modify: `backend/agent/loop.py`

- [x] **Step 1: 定位插入点**

在 `for iteration in range(self.max_retries):` 循环结束之后、`yield LLMChunk(TEXT_DELTA, "[达到最大循环次数...")` 之前（约 `loop.py:570` 附近）。

- [x] **Step 2: 写守卫**

```python
            # Boundary case: a write tool in the final iteration may have just
            # promoted phase to 5. Give the parallel orchestrator one more shot
            # before the safety-limit fallback so we don't drop the upgrade.
            if self.should_use_parallel_phase5(
                self.plan, self.phase5_parallel_config
            ):
                async for chunk in self._run_parallel_phase5_orchestrator():
                    yield chunk
                return
```

- [x] **Step 3: 重新跑 `pytest tests/test_loop_phase5_routing.py -v`**

新用例应 PASS，原 6 个静态谓词用例保持 PASS（共 7 个）。

- [x] **Step 4: 跑全量 `pytest`**

期望：1183 passed（原 1182 + 本次新增 1 个）。

---

### Task 3: 无需改动 Phase 5 orchestrator / context / config

- [x] **验证：** 兜底守卫调用的是既有的 `_run_parallel_phase5_orchestrator`，orchestrator 内部路径不变；`should_use_parallel_phase5` 也未修改。本任务为空，仅做确认。

---

### Task 4: 提交

- [x] **Step 1: 只 stage 本计划范围内的文件**

```bash
git add backend/agent/loop.py backend/tests/test_loop_phase5_routing.py
```

- [x] **Step 2: commit message（单个逻辑 commit 绑定实现 + 测试）**

```
feat(phase5): route boundary phase→5 promotion to parallel orchestrator

When a plan-writer tool call in the final iteration of AgentLoop promotes
the plan from phase 3 to phase 5, the loop-top guard has already been
passed for that iteration — without this fallback the user falls off the
end of the for-range and gets the "[达到最大循环次数]" safety-limit text
instead of the daily-plan orchestrator output.

Add a single guarded invocation of _run_parallel_phase5_orchestrator right
before the safety-limit emission, guarded by the same should_use_parallel_phase5
predicate used at the loop top. Shares the predicate so behaviour stays
symmetric across cold-start and post-iteration paths.

Covered by a new boundary unit test with max_retries=1 that asserts the
orchestrator fires (and the safety-limit text does not) when the single
iteration flips phase to 5.
```

对应实际 commit：`21792af`。

---

### Task 5: 文档追溯补档

**Files:**
- Add: `docs/superpowers/specs/2026-04-19-phase5-max-retries-guard-design.md`
- Add: `docs/superpowers/plans/2026-04-19-phase5-max-retries-guard.md`

- [x] **Step 1: 补 spec**

格式对齐 `docs/superpowers/specs/2026-04-18-phase5-orchestrator-workers-design.md`：背景 / 目标 / 非目标 / 设计 / 验收条件 / 风险 / 外部参照。

- [x] **Step 2: 补 plan（本文件）**

格式对齐 `docs/superpowers/plans/2026-04-18-phase5-orchestrator-workers.md`：Task / Step + checkbox。追溯补档用 `- [x]` 标记所有已完成步骤，并在顶部说明状态。

- [x] **Step 3: commit 文档**

```
docs(phase5): add spec + plan for max-retries boundary guard
```

文档作为追溯补档，与实现 commit (`21792af`) 分开，方便单独 revert / diff。

---

## 验收 checklist

- [x] `backend/pytest` 全量通过 (1183/1183)
- [x] `test_parallel_orchestrator_fires_after_final_iteration_phase_promotion` 新增且通过
- [x] `loop.py` 兜底守卫与循环顶部守卫使用**同一个** `should_use_parallel_phase5` 调用
- [x] safety-limit yield 仍保留在兜底守卫之后
- [x] postmortem 第 7.1 节的建议完整落地
- [x] spec + plan 文档入库 `docs/superpowers/`

---

## 偏离记录

| 项目 | 偏离 | 原因 |
|------|------|------|
| Brainstorming | 未执行 | 需求在 postmortem 第 7.1 节已经具体到代码片段 |
| Plan 先行 | 事后补档 | 实现顺序已既定，本文档保证流程闭环与后续可检索 |
| 严格 TDD（先红再绿） | 实际交替 | 实现与测试在同一 commit 内落地，没有独立的"红测试 commit"——后续实施类似规模任务时可按 plan 先红再绿，保留 TDD 轨迹 |
