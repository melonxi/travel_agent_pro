# Spec 1: Harness 验证前移

> **目标**：将质量守护从"阶段转换时事后检查"升级为"每次状态写入后实时拦截"，让 harness 质量层评分从 6.5 冲击 8.0。
>
> **隔离边界**：仅改动 `backend/` 下的 harness、tools、main.py(hooks 区域)。不碰 evals/、frontend/、api/。

---

## 1. 背景与动机

当前 `validate_hard_constraints` 仅在 `before_phase_transition` gate 中调用，意味着：
- Phase 3 lock 锁定了超预算的住宿 → Phase 5 日程编排全部基于错误数据 → Phase 7 门控才发现预算超支
- Phase 5 写入有时间冲突的活动 → 直到阶段转换才报错

TraceSafe 论文指出多步骤工具调用中的静默失败会复合累积。验证前移是 harness 工程中影响力最高的单一改进。

---

## 2. 改动点

### 2.1 增量验证函数 — `backend/harness/validator.py`

新增 `validate_incremental(plan, field, value)` 函数，针对单字段写入做轻量检查，避免每次全量校验：

| 触发字段 | 检查内容 |
|---------|---------|
| `budget` | `total > 0`，且如果已有 `daily_plans`，检查活动总成本不超新预算 |
| `dates` | `total_days >= 1`，且如果已有目的地，调用 `feasibility.check_feasibility` |
| `daily_plans` | 调用现有 `validate_hard_constraints` 的时间冲突检查子逻辑 |
| `selected_transport` | 触发 lock 预算门控（见 2.2） |
| `accommodation` | 触发 lock 预算门控（见 2.2） |

返回值：`list[str]`（错误消息列表），空列表表示通过。

与现有 `validate_hard_constraints` 的关系：增量函数复用其内部子逻辑（如 `_time_to_minutes`），但只检查与当前写入字段相关的约束，不做全量遍历。`validate_hard_constraints` 保留不变，继续在 `before_phase_transition` 中做全量终检。

### 2.2 Phase 3 lock 预算门控 — `backend/harness/validator.py`

新增 `validate_lock_budget(plan)` 函数：

- 计算已锁定项目总价：
  - `selected_transport` 中所有段的 price 之和
  - `accommodation` 的 price_per_night × 天数
- 如果 `plan.budget.total` 存在，检查锁定总价是否超过预算的 80%
- 超过 80% → 返回警告消息（"交通+住宿已占预算的 X%，仅剩 Y 元用于活动和餐饮"）
- 超过 100% → 返回错误消息
- 预算或锁定项不存在 → 跳过，返回空列表

80% 阈值作为模块级常量 `_LOCK_BUDGET_RATIO = 0.8`，便于调整。

### 2.3 工具结果 schema 验证强化 — `backend/harness/guardrail.py`

扩展现有 `_REQUIRED_RESULT_FIELDS` 和 `validate_output`：

**扩展 schema**：

```python
_REQUIRED_RESULT_FIELDS: dict[str, list[str]] = {
    "search_flights": ["price", "departure_time", "arrival_time", "airline"],
    "search_accommodations": ["price", "name", "location"],
    "search_trains": ["price", "departure_time", "arrival_time"],
}
```

**分级处理**：

新增 `_CRITICAL_FIELDS` 集合（`{"price"}`）。当缺失的字段在 `_CRITICAL_FIELDS` 中时，`validate_output` 返回 `level="error"`（当前全部是 `level="warn"`）。非关键字段缺失保持 warn。

改动范围：仅修改 `validate_output` 方法内的条件分支，不改方法签名。

### 2.4 Hook 改造 — `backend/main.py`

现有 `on_validate`（~L390-402）已经在 `update_plan_state` 后调用全量 `validate_hard_constraints`。需要改造为更精准的增量验证 + lock 预算门控：

**当前代码**（~L390-402）：
```python
async def on_validate(**kwargs):
    if kwargs.get("tool_name") == "update_plan_state":
        errors = validate_hard_constraints(plan)  # 全量检查
        if errors:
            ...  # 注入错误消息
```

**改造后**：
```python
async def on_validate(**kwargs):
    if kwargs.get("tool_name") == "update_plan_state":
        tc = kwargs.get("tool_call")
        field = tc.arguments.get("field", "") if tc and tc.arguments else ""
        value = tc.arguments.get("value") if tc and tc.arguments else None

        # 增量检查（替代全量 validate_hard_constraints）
        errors = validate_incremental(plan, field, value)

        # lock 预算门控（selected_transport / accommodation 写入时额外检查）
        if field in ("selected_transport", "accommodation"):
            lock_errors = validate_lock_budget(plan)
            errors.extend(lock_errors)

        if errors:
            session = sessions.get(plan.session_id)
            if session:
                feedback = "[实时约束检查]\n" + "\n".join(f"- {e}" for e in errors)
                session["messages"].append(
                    Message(role=Role.SYSTEM, content=feedback)
                )
```

关键设计决策：
- **增量替代全量**：`on_validate` 中用 `validate_incremental` 替代 `validate_hard_constraints`，只检查与当前写入字段相关的约束，降低无意义的全量遍历。全量 `validate_hard_constraints` 保留在 `on_before_phase_transition` gate 中作为终检兜底。
- **不阻断**：增量验证发现问题时注入 system message 提示 LLM 修正，不阻断当前工具调用。因为中间状态可能临时违反约束（如先写 accommodation 再写 budget 调整），全量终检仍在阶段转换时兜底。
- **lock 预算门控**：`selected_transport` 和 `accommodation` 写入时额外调用 `validate_lock_budget`，超 100% 作为 error 注入，超 80% 作为 warn 注入。

---

## 3. 文件改动清单

| 文件 | 改动类型 | 改动内容 |
|------|---------|---------|
| `backend/harness/validator.py` | 修改 | 新增 `validate_incremental` + `validate_lock_budget`，不改 `validate_hard_constraints` |
| `backend/harness/guardrail.py` | 修改 | 扩展 `_REQUIRED_RESULT_FIELDS`，新增 `_CRITICAL_FIELDS`，`validate_output` 增加分级逻辑 |
| `backend/main.py` | 修改 | `on_validate` hook（~L390-402）替换全量检查为增量检查 + lock 预算门控（约 20 行） |
| `backend/tests/test_incremental_validator.py` | 新建 | 测试 `validate_incremental` 各字段分支 |
| `backend/tests/test_lock_budget_gate.py` | 新建 | 测试 `validate_lock_budget` 的边界条件（80%/100%/无预算/无锁定项） |
| `backend/tests/test_guardrail.py` | 修改 | 追加 `_CRITICAL_FIELDS` 相关测试用例 |

**不碰的文件**：`backend/evals/`、`backend/api/`、`frontend/`、`backend/tools/update_plan_state.py`（验证逻辑在 hook 层，不侵入工具本身）。

---

## 4. 测试策略

### 4.1 test_incremental_validator.py

| 测试场景 | 输入 | 期望 |
|---------|------|------|
| budget 正常写入 | field=budget, value={total: 10000}, 无 daily_plans | 空列表 |
| budget 写入导致超支 | field=budget, value={total: 1000}, 已有 daily_plans 总成本 5000 | 返回超支错误 |
| dates 触发可行性检查 | field=dates, destination=东京, days=1 | 返回天数不足警告 |
| daily_plans 时间冲突 | field=daily_plans, 相邻活动时间重叠 | 返回时间冲突错误 |
| 非监控字段 | field=destination, value=京都 | 空列表（不检查） |

### 4.2 test_lock_budget_gate.py

| 测试场景 | 输入 | 期望 |
|---------|------|------|
| 锁定项占预算 60% | transport 3000 + accommodation 3000, budget 10000 | 空列表 |
| 锁定项占预算 85% | transport 5000 + accommodation 3500, budget 10000 | 警告消息 |
| 锁定项占预算 110% | transport 7000 + accommodation 4000, budget 10000 | 错误消息 |
| 无预算 | budget=None | 空列表 |
| 无锁定项 | transport=None, accommodation=None | 空列表 |
| 仅有交通无住宿 | transport 8000, accommodation=None, budget 10000 | 警告消息 |

### 4.3 test_guardrail.py 追加

| 测试场景 | 输入 | 期望 |
|---------|------|------|
| 航班缺 price | search_flights 结果无 price 字段 | level=error |
| 航班缺 airline | search_flights 结果无 airline 字段 | level=warn |
| 住宿缺 location | search_accommodations 结果无 location 字段 | level=warn |

---

## 5. 验收标准

1. `pytest backend/tests/test_incremental_validator.py backend/tests/test_lock_budget_gate.py` 全部通过
2. `pytest backend/tests/test_guardrail.py` 全部通过（含新增用例）
3. `pytest backend/` 全量回归无新增失败
4. `validate_hard_constraints` 原有行为不变（before_phase_transition 仍调用全量检查）
5. 增量验证仅注入 system message，不阻断工具执行流程
