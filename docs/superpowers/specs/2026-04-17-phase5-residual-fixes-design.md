# Phase 5 残留问题修复设计

日期：2026-04-17
关联复盘：`docs/postmortems/2026-04-17-phase5-retest-residual-issues.md`

---

## 1. 问题总览

Phase 5 状态契约主修复后，复测暴露三个独立残留问题：

| # | 问题 | 根因层 | 影响 |
|---|------|--------|------|
| A | `replace_daily_plans({})` 空参数调用 | 工具引擎无预校验 | 用户可见错误 |
| B | validator 时间冲突累积不阻断 | 架构仅诊断不控制 + prompt 未指导 | 行程质量不可用 |
| C | session meta 滞后于 plan 文件 | 增量持久化只做一半 | 三源不一致 |

附带修复：
| # | 问题 | 说明 |
|---|------|------|
| D | trace 缺失 error_code / suggestion | 前端和调试时无法看到结构化错误信息 |

---

## 2. 修复方案 A：工具参数 schema 预校验

### 2.1 选择的方案

在 `ToolEngine.execute()` 中，调用工具函数之前，根据 `tool_def.parameters["required"]` 校验 `call.arguments` 是否包含所有必填参数。缺参直接返回 `INVALID_ARGUMENTS` 的 `ToolResult`，不进入函数调用。

### 2.2 为什么不选其他方案

- **方案 B：完整 JSON Schema 校验**（用 jsonschema 库）：过重，引入新依赖，且工具内部已有详细的运行时校验。只做 required 检查就能解决"空参数"问题。
- **方案 C：给函数参数加默认值**：治标不治本，会把 TypeError 变成更隐蔽的逻辑错误。

### 2.3 实现细节

位置：`backend/tools/engine.py` 的 `execute()` 方法，在 `await tool_def(**call.arguments)` 之前。

```python
# 预校验 required 参数
params_schema = tool_def.parameters or {}
required = params_schema.get("required", [])
if required:
    missing = [p for p in required if p not in call.arguments]
    if missing:
        return ToolResult(
            tool_call_id=call.id,
            status="error",
            error=f"缺少必填参数: {', '.join(missing)}",
            error_code="INVALID_ARGUMENTS",
            suggestion=f"请提供以下参数: {', '.join(missing)}",
        )
```

### 2.4 测试

- `test_execute_missing_required_param_returns_invalid_arguments`：缺参时返回 INVALID_ARGUMENTS
- `test_execute_empty_args_returns_invalid_arguments`：空 dict 时返回 INVALID_ARGUMENTS
- `test_execute_with_all_required_params_passes`：参数齐全时正常执行

---

## 3. 修复方案 B：validator 时间冲突闭环

### 3.1 选择的方案

**写入后即时校验 + 严重冲突阻断**：在 `append_day_plan` / `replace_daily_plans` 工具函数内部，写入 plan 状态后、返回成功结果前，调用 validator 检查刚写入的天的时间冲突。如果发现严重冲突（负间隔或零间隔），将冲突信息附加到工具返回的 `data` 中，并在 `data` 中标记 `has_severe_conflicts: true`。

同时在 Phase 5 prompt 中增加明确指令：当工具返回包含 `has_severe_conflicts: true` 时，必须先用 `replace_daily_plans` 修复冲突天数，再继续追加后续天数。

### 3.2 为什么不选其他方案

- **方案 B：严重冲突时返回 error 拒绝写入**：过于激进，DayPlan 已部分写入 plan 状态，回滚复杂。而且 LLM 可能会陷入"写入→拒绝→重写→拒绝"的死循环。
- **方案 C：在 ToolEngine 层拦截**：破坏了 engine 的"通用执行"职责边界，把业务逻辑放进了基础设施层。

### 3.3 实现细节

#### 3.3.1 工具层修改

位置：`backend/tools/plan_tools/daily_plans.py`

在 `append_day_plan` 和 `replace_daily_plans` 函数中，写入 plan 后调用 `_check_day_conflicts(plan, day_numbers)` 检查刚写入的天数：

```python
def _check_day_conflicts(plan: TravelPlanState, day_numbers: list[int]) -> dict:
    """检查指定天数的时间冲突，返回冲突信息。"""
    from harness.validator import _validate_time_conflicts
    all_errors = _validate_time_conflicts(plan)
    relevant = [e for e in all_errors if any(f"Day {d}:" in e for d in day_numbers)]
    severe = [e for e in relevant if "间隔仅 0min" in e or "间隔仅 -" in e]
    return {
        "conflicts": relevant,
        "has_severe_conflicts": len(severe) > 0,
    }
```

将冲突信息合并到工具返回的 data dict 中。

#### 3.3.2 Prompt 修改

位置：`backend/phase/prompts.py` 的 `PHASE5_PROMPT`

在工具契约段落后增加：

```
## 时间冲突处理

append_day_plan / replace_daily_plans 返回结果中可能包含 `conflicts` 和 `has_severe_conflicts` 字段。

- 如果 `has_severe_conflicts` 为 true：必须立即使用 replace_daily_plans 修复该天的时间安排，确保活动之间留有合理的交通缓冲时间。不要继续追加后续天数。
- 如果有 conflicts 但不严重（正间隔但偏短）：记录在心，可继续追加，在最后统一优化。
```

### 3.4 测试

- `test_append_day_plan_returns_conflicts_on_time_overlap`：写入有时间冲突的活动，返回 data 中包含 conflicts
- `test_append_day_plan_flags_severe_conflict`：负间隔/零间隔标记 has_severe_conflicts=true
- `test_append_day_plan_no_conflicts_when_valid`：正常活动无冲突信息

---

## 4. 修复方案 C：session meta 增量更新

### 4.1 选择的方案

在 plan writer 工具成功且增量保存 plan 文件后，同步更新 `session_store` 的 `phase` 和 `updated_at`。

### 4.2 实现细节

位置：`backend/main.py` 的 `_run_agent_stream()` 中增量持久化段落（约 L1749）。

在 `await state_mgr.save(plan)` 之后增加：

```python
await session_store.update(
    plan.session_id,
    phase=plan.phase,
    title=_generate_title(plan),
)
```

同时将 finally 块中的 `except Exception: pass` 改为记录 warning 日志：

```python
except Exception:
    logger.warning("保底持久化失败 session=%s", plan.session_id, exc_info=True)
```

### 4.3 测试

- `test_incremental_persist_updates_session_meta`：模拟 plan writer 成功后 session_store.update 被调用
- `test_finally_persist_logs_on_failure`：模拟 finally 保存失败时有日志输出

---

## 5. 修复方案 D：trace 暴露完整错误结构

### 5.1 实现细节

位置：`backend/api/trace.py` 的 `build_trace()` 和 `backend/telemetry/stats.py` 的 `ToolCallRecord`。

1. `ToolCallRecord` 增加 `suggestion: str | None = None` 字段
2. `record_tool_call()` 接收 `suggestion` 参数
3. `_record_tool_result_stats()` 传递 `result.suggestion`
4. `build_trace()` 输出 `error_code` 和 `suggestion` 到 tool_call dict

### 5.2 测试

- `test_trace_includes_error_code_and_suggestion`：error 状态的 tool call 在 trace 中包含 error_code 和 suggestion

---

## 6. 不在范围内

- validator 自修复闭环的完整设计（如自动重试机制）——当前只做"通知 + prompt 指导"
- 前端对 error_code / suggestion 的展示改进——前端不在后端修复范围
- `_persist_messages()` 不在 finally 中的问题——影响面更大，需要独立设计
- prompt 文案断言测试的修复——预先存在的问题，与本次无关

---

## 7. 验收标准

1. 空参数调用 `replace_daily_plans({})` 返回 `INVALID_ARGUMENTS`，不再触发 Python TypeError
2. `append_day_plan` 写入有时间冲突的活动时，返回 data 中包含 `conflicts` 和 `has_severe_conflicts`
3. Phase 5 prompt 明确指导模型处理时间冲突
4. plan writer 成功后 session meta 的 phase 和 updated_at 同步更新
5. trace API 输出包含 `error_code` 和 `suggestion` 字段
6. finally 保底保存失败时有 warning 日志
7. 所有新增测试通过，现有测试不退化
