# Phase 5 Worker Convergence Guards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Phase 5 并行 Day Worker 增加“两层收敛机制”：提示词止血和 loop 守卫，降低 30 轮内不收敛概率，并保留明确错误分类。

**Architecture:** 保持现有 orchestrator-worker 架构不变，只在 `worker_prompt.py` 调整收口策略，在 `day_worker.py` 增加运行时守卫、格式修复回合和错误分类，并让 `orchestrator.py` 透传更可诊断的失败信息。测试沿用现有 `backend/tests/test_day_worker.py`、`test_worker_prompt.py`、`test_orchestrator.py` 做最小增量扩展。

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, dataclasses, asyncio, 现有 LLMProvider/ToolEngine 抽象

---

## File Map

- Modify: `backend/agent/worker_prompt.py`
- Modify: `backend/agent/day_worker.py`
- Modify: `backend/agent/orchestrator.py`
- Modify: `backend/tests/test_worker_prompt.py`
- Modify: `backend/tests/test_day_worker.py`
- Modify: `backend/tests/test_orchestrator.py`
- Modify: `PROJECT_OVERVIEW.md`

### Task 1: 收紧 Worker Prompt 语义

**Files:**
- Modify: `backend/agent/worker_prompt.py`
- Test: `backend/tests/test_worker_prompt.py`

- [ ] **Step 1: 先写 prompt 测试，锁定“有限补救 + 保守落地”文案要求**

在 `backend/tests/test_worker_prompt.py` 增加一个断言共享前缀包含以下语义：

```python
def test_build_shared_prefix_contains_convergence_rules():
    plan = _make_plan()
    prefix = build_shared_prefix(plan)
    assert "有限次补救" in prefix
    assert "保守版 DayPlan" in prefix
    assert "不得编造具体营业时间" in prefix
    assert "写入 notes" in prefix
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_worker_prompt.py::test_build_shared_prefix_contains_convergence_rules -v`

Expected: FAIL，提示新文案尚未出现在 `build_shared_prefix()` 结果中。

- [ ] **Step 3: 修改 `worker_prompt.py`，把无限补救改为有限补救**

更新 `_WORKER_ROLE` 的“工具回退策略”，包含以下明确要求：

```python
## 工具回退策略

- 当专项工具返回无效信息时，可以进行有限次补救，但不要围绕同一 POI 或同一问题无限搜索。
- 如果已经具备区域、主题、核心活动和基本时间结构，应优先输出保守版 DayPlan。
- 当工具仍无法补齐细节时，可以基于骨架、区域连续性和常识性节奏完成保守安排。
- 不得编造具体营业时间、具体票价、明确预约要求；无法确认的事实写入 notes。
- 当系统提示进入收口模式时，必须停止继续调工具并直接输出 DayPlan JSON。
```

- [ ] **Step 4: 运行 prompt 相关测试，确认通过**

Run: `pytest backend/tests/test_worker_prompt.py -v`

Expected: PASS，已有测试和新增规则测试都通过。

- [ ] **Step 5: 提交本任务修改**

```bash
git add backend/agent/worker_prompt.py backend/tests/test_worker_prompt.py
git commit -m "fix(phase5): tighten worker convergence prompt"
```

### Task 2: 为 Day Worker 增加错误分类基础

**Files:**
- Modify: `backend/agent/day_worker.py`
- Test: `backend/tests/test_day_worker.py`

- [ ] **Step 1: 先写失败结果测试，锁定错误分类字段**

在 `backend/tests/test_day_worker.py` 增加对 `DayWorkerResult` 的断言，要求支持 `error_code`：

```python
def test_day_worker_result_failure_with_error_code():
    r = DayWorkerResult(
        day=2,
        date="2026-05-02",
        success=False,
        dayplan=None,
        error="JSON invalid",
        error_code="JSON_EMIT_FAILED",
    )
    assert r.success is False
    assert r.error_code == "JSON_EMIT_FAILED"
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_day_worker.py::test_day_worker_result_failure_with_error_code -v`

Expected: FAIL，提示 `DayWorkerResult` 暂不接受 `error_code` 参数。

- [ ] **Step 3: 扩展 `DayWorkerResult` 数据结构**

在 `backend/agent/day_worker.py` 的 `DayWorkerResult` 中增加：

```python
error_code: str | None = None
```

并确保后续失败返回点都能逐步接入该字段。

- [ ] **Step 4: 运行单测确认结构扩展通过**

Run: `pytest backend/tests/test_day_worker.py -v`

Expected: PASS，结构级测试全部通过。

- [ ] **Step 5: 提交本任务修改**

```bash
git add backend/agent/day_worker.py backend/tests/test_day_worker.py
git commit -m "refactor(phase5): add day worker error codes"
```

### Task 3: 为 Day Worker 增加格式修复回合

**Files:**
- Modify: `backend/agent/day_worker.py`
- Test: `backend/tests/test_day_worker.py`

- [ ] **Step 1: 先写异步测试，锁定“无 JSON 不立刻失败”行为**

在 `backend/tests/test_day_worker.py` 增加一个双轮 LLM stub 测试：第一轮无工具且输出非 JSON，第二轮输出合法 JSON，最终应成功。

```python
@pytest.mark.asyncio
async def test_run_day_worker_retries_once_for_json_repair():
    ...
    assert result.success is True
    assert result.dayplan["day"] == 1
    assert llm.calls == 2
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_day_worker.py::test_run_day_worker_retries_once_for_json_repair -v`

Expected: FAIL，当前实现会在第一轮直接返回失败。

- [ ] **Step 3: 在 `run_day_worker()` 中加入一次 JSON 修复回合**

实现规则：

```python
if not tool_calls:
    dayplan = extract_dayplan_json(assistant_text)
    if dayplan is not None:
        return success
    if not emit_repair_attempted:
        emit_repair_attempted = True
        messages.append(Message(role=Role.ASSISTANT, content=assistant_text))
        messages.append(
            Message(
                role=Role.SYSTEM,
                content="请立即只输出一个合法的 DayPlan JSON 代码块，必须包含 day/date/activities。",
            )
        )
        continue
    return DayWorkerResult(..., error_code="JSON_EMIT_FAILED")
```

- [ ] **Step 4: 补一条失败路径测试**

新增测试：连续两轮都未输出合法 JSON，则返回 `error_code == "JSON_EMIT_FAILED"`。

Run: `pytest backend/tests/test_day_worker.py::test_run_day_worker_returns_json_emit_failed_after_repair -v`

Expected: PASS。

- [ ] **Step 5: 运行 Day Worker 测试全集**

Run: `pytest backend/tests/test_day_worker.py -v`

Expected: PASS。

- [ ] **Step 6: 提交本任务修改**

```bash
git add backend/agent/day_worker.py backend/tests/test_day_worker.py
git commit -m "fix(phase5): add json repair round to day worker"
```

### Task 4: 加入重复查询抑制与补救链阈值

**Files:**
- Modify: `backend/agent/day_worker.py`
- Test: `backend/tests/test_day_worker.py`

- [ ] **Step 1: 先写工具循环测试，锁定重复查询触发收口**

新增测试场景：LLM 连续两轮对同一 `web_search.query` 发起相同调用，第三轮应被系统提示切入收口并最终成功输出 JSON。

```python
@pytest.mark.asyncio
async def test_run_day_worker_forces_emit_after_repeated_query_loop():
    ...
    assert result.success is True
    assert any("停止继续调工具" in m.content for m in llm.seen_messages if m.role == Role.SYSTEM)
```

- [ ] **Step 2: 再写同一 POI 补救链阈值测试**

新增测试场景：

1. `get_poi_info("浅草寺")`
2. `web_search("浅草寺 开放时间")`
3. `check_availability("浅草寺", "2026-05-01")`
4. 再次 `web_search("浅草寺 营业时间")`

达到阈值后，worker 应进入 `forced_emit_mode`，而不是继续无限搜索。

- [ ] **Step 3: 运行新增测试，确认当前失败**

Run: `pytest backend/tests/test_day_worker.py -k "repeated_query_loop or recovery_chain" -v`

Expected: FAIL，当前没有任何重复查询或补救链守卫。

- [ ] **Step 4: 在 `day_worker.py` 中实现查询指纹与补救链计数**

增加小型 helper，建议形态：

```python
def _tool_query_fingerprint(call: ToolCall) -> str | None: ...
def _tool_recovery_key(call: ToolCall) -> str | None: ...
```

并在执行工具前更新：

```python
repeated_query_counts[fingerprint] += 1
poi_recovery_counts[recovery_key] += 1
```

当超过阈值时：

```python
forced_emit_mode = True
messages.append(
    Message(
        role=Role.SYSTEM,
        content="你已对同一问题进行了重复补救。请基于现有信息停止继续调工具，并直接输出 DayPlan JSON。无法确认的事实写入 notes。",
    )
)
```

- [ ] **Step 5: 让超阈值失败具备明确错误码**

当 worker 最终因预算耗尽仍未产出结果时，按原因区分：

```python
error_code="REPEATED_QUERY_LOOP"
error_code="RECOVERY_CHAIN_EXHAUSTED"
```

而不是统一落到普通错误字符串。

- [ ] **Step 6: 运行 Day Worker 测试全集**

Run: `pytest backend/tests/test_day_worker.py -v`

Expected: PASS。

- [ ] **Step 7: 提交本任务修改**

```bash
git add backend/agent/day_worker.py backend/tests/test_day_worker.py
git commit -m "fix(phase5): guard repeated worker recovery loops"
```

### Task 5: 加入后半程强制收口模式

**Files:**
- Modify: `backend/agent/day_worker.py`
- Test: `backend/tests/test_day_worker.py`

- [ ] **Step 1: 先写后半程收口测试**

新增测试：在 `max_iterations=5` 情况下，前 3 轮持续调用工具，后续系统应追加收口提示，并在第 4/5 轮推动输出 JSON。

```python
@pytest.mark.asyncio
async def test_run_day_worker_adds_forced_emit_prompt_in_late_iterations():
    ...
    assert result.success is True
```

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_day_worker.py::test_run_day_worker_adds_forced_emit_prompt_in_late_iterations -v`

Expected: FAIL，当前没有 late-iteration 收口逻辑。

- [ ] **Step 3: 在 `run_day_worker()` 中加入后半程收口判定**

建议使用一个小 helper：

```python
def _should_force_emit(iteration: int, max_iterations: int) -> bool:
    return iteration + 1 >= max(3, int(max_iterations * 0.6))
```

到达阈值后，若仍在重复调工具，则追加 system 提示：

```python
"你已进入收口阶段。不要再为细节重复搜索；请基于已知信息立即输出 DayPlan JSON，无法确认的事实写入 notes。"
```

- [ ] **Step 4: 运行 Day Worker 测试全集**

Run: `pytest backend/tests/test_day_worker.py -v`

Expected: PASS。

- [ ] **Step 5: 提交本任务修改**

```bash
git add backend/agent/day_worker.py backend/tests/test_day_worker.py
git commit -m "fix(phase5): force day worker emit in late iterations"
```

### Task 6: 让 Orchestrator 透传更清晰的失败信息

**Files:**
- Modify: `backend/agent/orchestrator.py`
- Test: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: 先写 orchestrator 测试，锁定错误码可见性**

在 `backend/tests/test_orchestrator.py` 增加一个失败 worker stub，返回：

```python
DayWorkerResult(
    day=1,
    date="2026-05-01",
    success=False,
    dayplan=None,
    error="Worker 耗尽迭代",
    error_code="REPEATED_QUERY_LOOP",
)
```

断言 progress chunk 中的 worker 状态包含该错误码，或错误文本中保留错误码前缀。

- [ ] **Step 2: 运行测试，确认当前失败**

Run: `pytest backend/tests/test_orchestrator.py -k error_code -v`

Expected: FAIL，当前 orchestrator 只记录 `error` 文本。

- [ ] **Step 3: 修改 `orchestrator.py` 的状态字段和日志**

在 `worker_statuses` 中增加 `error_code`，在 worker 失败和重试失败分支里填充：

```python
worker_statuses[idx]["error_code"] = result.error_code
```

同时日志改为包含错误码：

```python
logger.warning("Day %d worker failed [%s]: %s", day_task.day, result.error_code, result.error)
```

- [ ] **Step 4: 运行 orchestrator 测试全集**

Run: `pytest backend/tests/test_orchestrator.py -v`

Expected: PASS。

- [ ] **Step 5: 提交本任务修改**

```bash
git add backend/agent/orchestrator.py backend/tests/test_orchestrator.py
git commit -m "chore(phase5): surface worker convergence error codes"
```

### Task 7: 更新项目全景文档

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新 Phase 5 段落**

在 `PROJECT_OVERVIEW.md` 的 Phase 5 描述中补充：

1. Day Worker 具备提示词止血策略
2. Day Worker loop 具备重复查询抑制、补救链阈值、后半程强制收口、JSON 修复回合
3. worker 失败会输出更清晰的错误类别

- [ ] **Step 2: 校验文档与实现一致**

人工检查以下描述一致：

```text
并行模式中的 Day Worker 不再无限补齐信息；当专项工具持续无效时，会切换到保守落地 + 收口模式。
```

- [ ] **Step 3: 提交文档修改**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update phase5 worker convergence overview"
```

### Task 8: 回归验证

**Files:**
- Verify only: `backend/tests/test_worker_prompt.py`
- Verify only: `backend/tests/test_day_worker.py`
- Verify only: `backend/tests/test_orchestrator.py`

- [ ] **Step 1: 运行 prompt/worker/orchestrator 相关测试**

Run: `pytest backend/tests/test_worker_prompt.py backend/tests/test_day_worker.py backend/tests/test_orchestrator.py -v`

Expected: PASS。

- [ ] **Step 2: 检查是否存在因错误码字段扩展导致的断言更新**

若有新的断言需求，只修改相关测试，不扩大到无关模块。

- [ ] **Step 3: 检查变更文件列表是否符合 spec 范围**

Run: `git status --short`

Expected: 仅包含计划内文件或测试快照类文件，无无关改动。

- [ ] **Step 4: 最终提交**

```bash
git add PROJECT_OVERVIEW.md backend/agent/worker_prompt.py backend/agent/day_worker.py backend/agent/orchestrator.py backend/tests/test_worker_prompt.py backend/tests/test_day_worker.py backend/tests/test_orchestrator.py
git commit -m "fix(phase5): add convergence guards for parallel day workers"
```
