# Harness Architecture Deep Dive

## 5 层

1. 输入护栏 Guardrail。
2. 硬约束验证器 Validator。
3. 软评分 Judge。
4. 可行性门控 Feasibility Gate。
5. 成本与延迟追踪。

## 输入护栏

覆盖：

- 中文注入检测。
- 消息长度限制。
- 搜索结果字段分级校验。
- 工具结果异常检测。
- 非法预算检测。

## 硬约束验证器

覆盖：

- 时间冲突。
- 预算超支。
- 天数超限。
- 交通/住宿预算占比。
- 所有 plan writer 成功写入后的 incremental validation。

## 软评分

在关键写入后触发：

- `save_day_plan`
- `replace_all_day_plans`
- `generate_summary`

维度：

- pace
- geography
- coherence
- personalization

## 可行性门控

Phase 1 -> 2 时基于目的地查表做规则式判断。

## 成本与延迟

`SessionStats` 记录：

- token 用量。
- 模型成本。
- 工具调用。
- state changes。
- validation errors。
- judge scores。
- memory hits。
- recall telemetry。

## 关键代码

- `backend/harness/guardrail.py`
- `backend/harness/validator.py`
- `backend/harness/judge.py`
- `backend/harness/feasibility.py`
- `backend/telemetry/stats.py`
- `backend/agent/hooks.py`
