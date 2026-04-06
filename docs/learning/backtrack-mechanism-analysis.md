# 回跳机制分析与设计漏洞

> 用于 session handoff，接力 session 可直接基于此文档展开修复工作。

## 背景

`travel_agent_pro` 使用 Phase Agent 架构，将旅行规划分为 6 个阶段（Phase 1/2/3/4/5/7）。
系统支持两种回跳方式：用户在 chat 中输入关键词触发**隐式回跳**，或调用 `/api/backtrack/{session_id}` 触发**显式回跳**。

---

## 洞察 1：回跳触发基于硬编码子串匹配

### 实现位置

`backend/main.py:201` — `_BACKTRACK_PATTERNS`

### 代码

```python
_BACKTRACK_PATTERNS: dict[int, list[str]] = {
    1: ["重新开始", "从头来", "换个需求"],
    2: ["换个目的地", "不想去这里", "不去了", "换地方"],
    3: ["改日期", "换时间", "日期不对"],
    4: ["换住宿", "不住这", "换个区域"],
}

def _detect_backtrack(message: str, plan: TravelPlanState) -> int | None:
    for target_phase, patterns in _BACKTRACK_PATTERNS.items():
        if target_phase >= plan.phase:
            continue
        if any(p in message for p in patterns):  # 子串包含匹配
            return target_phase
    return None
```

### 问题

- **误触发**：`any(p in message ...)` 是纯子串匹配，没有上下文感知。
  - 用户说"我不想去这里吃饭" → 命中"不想去这里" → 意外触发 phase 2 回跳
  - 用户说"日期不对，是 5 月不是 6 月" → 触发 phase 3 回跳（符合预期），但"日期不对"也可能出现在其他语境中
- **漏触发**：关键词覆盖有限，用户说"重来吧""换一个目的地""目的地换掉"均不会触发
- **无优先级**：多个关键词同时命中时，取决于 `_BACKTRACK_PATTERNS` 的字典遍历顺序（Python 3.7+ 保证插入顺序，即 phase 1 优先）

### 潜在改进方向

1. 改为 LLM 意图识别（调用轻量模型判断是否有回跳意图及目标 phase）
2. 引入词边界匹配（正则 `\b` 或分词后匹配）减少误触发
3. 关键词配置化（移入 config 文件），方便运营调整而无需改代码

---

## 洞察 2：Phase 1 回跳存在状态残留漏洞

### 实现位置

- `backend/state/models.py:209` — `_PHASE_DOWNSTREAM`
- `backend/state/models.py:233` — `TravelPlanState.clear_downstream()`

### 核心问题

`_PHASE_DOWNSTREAM` 只定义了 phase 3 和 4 的下游字段，**未覆盖 `destination` 和 `dates`**：

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    3: ["accommodation", "daily_plans"],
    4: ["daily_plans"],
}
```

`clear_downstream` 仅遍历该字典，因此回跳到 phase 1 时：

| 字段 | 预期 | 实际 |
|---|---|---|
| `phase` | 1 | 1 ✓ |
| `accommodation` | None | None ✓ |
| `daily_plans` | [] | [] ✓ |
| `destination` | **None** | **旧值残留** |
| `dates` | **None** | **旧值残留** |

### 失效路径

```
用户说"重新开始"
    → backtrack to phase 1
    → clear_downstream(from_phase=1) 清除 accommodation + daily_plans
    → plan.phase = 1

用户发送下一条消息
    → apply_trip_facts()
    → check_and_apply_transition() → infer_phase()
        destination 有值 → 跳过 phase 1/2 判断
        dates 有值       → 跳过 phase 3 判断
        accommodation=None → return 4
    → plan.phase = 4  ← 用户以为从头来，实际跳到了 phase 4
```

同样地，回跳到 phase 2（"换个目的地"）后，`destination` 字段也不会被清空——
但 phase 2 的语义是"换目的地而非重来"，LLM 在 phase 2 角色下会重新推荐并写入新 `destination`，
所以 phase 2 的行为虽然有残留但实际影响较小。**Phase 1 的"完全重来"语义才是真正失效的场景。**

### 修复方案

**方案 A（最小改动）**：在 `_PHASE_DOWNSTREAM` 补充 phase 1 和 2 的清除字段

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: ["destination", "destination_candidates", "dates", "travelers", "budget",
        "accommodation", "daily_plans", "constraints"],
    2: ["destination", "destination_candidates", "accommodation", "daily_plans"],
    3: ["accommodation", "daily_plans"],
    4: ["daily_plans"],
}
```

注意：`preferences` 即使 phase 1 回跳也**不应清除**，因为它代表用户的基础偏好（喜欢海边、预算范围），
属于跨 session 的用户意图层，不是某次规划的产出物。

**方案 B（彻底重来）**：对 phase 1 回跳直接创建新 session，保留 `preferences` 迁移

```python
if backtrack_target == 1:
    new_plan = TravelPlanState(
        session_id=plan.session_id,
        preferences=plan.preferences,  # 迁移偏好
    )
    session["plan"] = new_plan
```

方案 B 更符合"重新开始"的语义，但需要同步处理消息历史（是否清空 messages）。

---

## 涉及文件清单

| 文件 | 相关内容 |
|---|---|
| `backend/main.py:201` | `_BACKTRACK_PATTERNS` 定义与 `_detect_backtrack()` |
| `backend/main.py:269` | chat 接口中的隐式回跳触发逻辑 |
| `backend/main.py:242` | `/api/backtrack` 显式回跳接口 |
| `backend/state/models.py:209` | `_PHASE_DOWNSTREAM` 字段映射 |
| `backend/state/models.py:233` | `clear_downstream()` 实现 |
| `backend/phase/router.py:55` | `prepare_backtrack()` 执行回跳三步骤 |

---

## 接力 Session 建议任务

1. **复现漏洞 2**：写一个测试，验证 phase 1 回跳后 `destination` 和 `dates` 未被清除，且下一条消息后 phase 会跳至 3+
2. **选择修复方案**（A 或 B）并实施
3. **评估漏洞 1 的误触发率**：可以用真实用户消息样本跑 `_detect_backtrack`，统计误触发比例，再决定是否值得引入 LLM 意图识别

---

## 实施计划（已确认）

> 2026-04-04 确认方案：洞察 1 采用 agent tool call 路线；洞察 2 采用方案 A。

### 问题一：关于"是否应该基于 agent tool call"的回答

**Phase 检测**是隐式副作用：LLM 调用 `update_plan_state` 更新字段 → 后端 `infer_phase()` 规则自动推断新 phase。LLM 从不主动声明 phase 转换。

**回退检测**的需求不同：需要识别自然语言意图（"想换个地方" vs "这里的餐厅不好"），适合通过**显式 tool call 声明**。

结论：**引入 `request_backtrack` 工具**，由 LLM 主动调用，完全替换子串匹配。

---

### Fix 1：修复 `_PHASE_DOWNSTREAM` 状态残留（洞察 2）

**文件**：`backend/state/models.py:209`，采用**方案 A**：

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: ["destination", "destination_candidates", "dates", "travelers", "budget",
        "accommodation", "daily_plans", "constraints"],
    2: ["destination", "destination_candidates", "accommodation", "daily_plans"],
    3: ["accommodation", "daily_plans"],
    4: ["daily_plans"],
}
```

`preferences` 不清除（跨 session 的用户偏好层）。

---

### Fix 2：引入 `request_backtrack` 工具（洞察 1）

#### 新建 `backend/tools/request_backtrack.py`

工厂函数：`make_request_backtrack_tool(plan, state_mgr, phase_router)`

```python
@tool(
    name="request_backtrack",
    description="""回退到更早的规划阶段。
Use when: 用户表达了重新规划、更换目的地/日期/住宿的意图。
Don't use when: 用户只是提问或修改细节，不需要重置阶段状态。""",
    phases=[2, 3, 4, 5, 7],
    parameters={
        "type": "object",
        "properties": {
            "to_phase": {"type": "integer", "enum": [1, 2, 3, 4],
                         "description": "目标阶段：1=完全重来，2=换目的地，3=改日期，4=换住宿"},
            "reason": {"type": "string", "description": "回退原因（简短）"},
        },
        "required": ["to_phase", "reason"],
    },
)
async def request_backtrack(to_phase: int, reason: str) -> dict:
    from_phase = plan.phase
    if to_phase >= from_phase:
        raise ToolError(f"只能回退到更早的阶段（当前 {from_phase}）", error_code="INVALID_PHASE")
    snapshot_path = await state_mgr.save_snapshot(plan)
    phase_router.prepare_backtrack(plan, to_phase, reason, snapshot_path)
    return {"from_phase": from_phase, "to_phase": to_phase, "confirmed": True}
```

#### 修改 `backend/main.py`

1. `_build_agent()` 中注册新工具（第 88-99 行附近）：
   ```python
   tool_engine.register(make_request_backtrack_tool(plan, state_mgr, phase_router))
   ```

2. 删除 `_BACKTRACK_PATTERNS` 和 `_detect_backtrack()`（第 200-214 行）。

3. chat endpoint 移除关键词预检测分支，始终执行 `apply_trip_facts`（第 268-284 行）：
   ```python
   # 删除 backtrack_target = _detect_backtrack(...) 分支
   updated_fields = apply_trip_facts(plan, req.message)
   if updated_fields:
       phase_router.check_and_apply_transition(plan)
   ```

4. `event_stream()` 末尾添加 agent 重建（第 325 行 `save` 之后）：
   ```python
   await state_mgr.save(plan)
   session["agent"] = _build_agent(plan)  # 反映 backtrack/phase 变更
   ```

#### 修改 `backend/phase/prompts.py`

每个 phase 2-7 的 prompt 末尾追加：
```
如果用户想重新规划，调用 request_backtrack：完全重来→1，换目的地→2，改日期→3，换住宿→4（仅 phase 4+）。
```

---

### 测试变更

| 文件 | 操作 |
|------|------|
| `backend/tests/test_state_models.py` | 新增：phase 1 回退后 destination/dates 为 None 的验证 |
| `backend/tests/test_phase_router.py` | 新增：phase 1 回退后 `infer_phase()` 返回 1（非 4+）的验证 |
| `backend/tests/test_error_paths.py` | 删除关键词依赖测试；新增 mock LLM 返回 `request_backtrack` tool call 的集成测试（验证状态变更） |
| `backend/tests/conftest.py` | 新增 `mock_llm_returning_backtrack_tool_call` fixture |

---

### 涉及文件汇总

| 文件 | 操作 |
|------|------|
| `backend/state/models.py:209` | 扩展 `_PHASE_DOWNSTREAM` |
| `backend/tools/request_backtrack.py` | 新建 |
| `backend/main.py:88` | 注册新工具 |
| `backend/main.py:200` | 删除关键词检测 |
| `backend/main.py:268` | 移除预检测分支 |
| `backend/main.py:325` | event_stream 末尾加 agent rebuild |
| `backend/phase/prompts.py` | 各 phase 追加回退指令 |
| `backend/tests/test_state_models.py` | 新增测试 |
| `backend/tests/test_phase_router.py` | 新增测试 |
| `backend/tests/test_error_paths.py` | 更新测试 |
| `backend/tests/conftest.py` | 新增 fixture |

---

## 实施方案（已确认）

### Fix 1：补全 `_PHASE_DOWNSTREAM`（修复洞察 2）

选用**方案 A**（最小改动），在 `backend/state/models.py` 的 `_PHASE_DOWNSTREAM` 中补充 phase 1 和 2：

```python
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: ["destination", "destination_candidates", "dates", "travelers",
        "budget", "accommodation", "daily_plans", "constraints"],
    2: ["destination", "destination_candidates", "accommodation", "daily_plans"],
    3: ["accommodation", "daily_plans"],
    4: ["daily_plans"],
}
```

> `preferences` 在所有回退场景下均**保留**，它代表用户基础偏好而非规划产出。

---

### Fix 2：用 Tool Call 替换硬编码子串匹配（修复洞察 1）

**设计原则**：与 phase 前进对称——前进靠 LLM 调用 `update_plan_state` → `check_and_apply_transition`；回退改为 LLM 调用 `backtrack_to_phase` tool，删除 `_detect_backtrack()`。

#### 2a. 新建 `backend/tools/backtrack.py`

```python
make_backtrack_tool(plan, phase_router, state_mgr)
```

- **参数**：`to_phase: int`（目标阶段 1-4）、`reason: str`（回退原因）
- **可用阶段**：`[2, 3, 4, 5, 7]`（非初始 phase 均可触发）
- **内部逻辑**：验证 `to_phase < plan.phase` → 调用 `state_mgr.save_snapshot(plan)` → 调用 `phase_router.prepare_backtrack()`
- **返回**：`{"backtracks_to": to_phase, "cleared_fields": [...], "reason": reason}`

#### 2b. 修改 `backend/main.py`

1. 在 `_build_agent()` 中注册 `make_backtrack_tool(plan, phase_router, state_mgr)`
2. 删除 `_BACKTRACK_PATTERNS` 常量和 `_detect_backtrack()` 函数
3. 删除 `/api/chat` 中的隐式回退检测代码块（约 L268-279）

#### 2c. 更新系统提示 `backend/context/manager.py`

在 `build_system_message()` 的阶段指引之后追加全局规则：

```
当用户明确表达想重新开始、改变已确认的目的地/日期/住宿时，
使用 backtrack_to_phase 工具回退到合适阶段，而非直接修改已锁定字段。
```

---

### 涉及文件（更新后）

| 文件 | 变更内容 |
|------|---------|
| `backend/state/models.py` | 补充 `_PHASE_DOWNSTREAM` phase 1/2 条目 |
| `backend/tools/backtrack.py` | 新建，实现 `make_backtrack_tool` |
| `backend/main.py` | 注册新 tool，删除 `_BACKTRACK_PATTERNS`、`_detect_backtrack()`、隐式检测块 |
| `backend/context/manager.py` | system prompt 补充全局回退提示 |
| `backend/tests/test_phase_router.py` | 补充 phase 1 回退状态残留回归测试 |
| `backend/tests/test_backtrack_tool.py` | 新建，覆盖成功/无效 phase/快照/历史记录路径 |
