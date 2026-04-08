# Phase 5 `daily_plans` 提交链路修复记录

修复时间：2026-04-08
修复范围：`backend/state/models.py`、`backend/tools/generate_summary.py`、`backend/phase/prompts.py`、`backend/tools/update_plan_state.py`

## 背景

在通过 Playwright MCP 做 Phase 1 → Phase 7 全流程回归时，发现 Phase 5 的"逐日行程提交"环节存在若干串联性 bug：LLM 在产出 `daily_plans` 时字段形状有一定随机性，而后端 dataclass 的 `from_dict` 只处理"理想输入"，导致一遇到变体就抛错，进而让 Phase 5 无法进入 Phase 7；随后 Phase 7 的 `generate_summary` 又在另一套宽松输入上继续抛错。本文档汇总这批问题的根因、修复方式以及与之配套的测试。

## 观察到的现象

- Phase 5 中 `update_plan_state(field="daily_plans", value=...)` 频繁抛 `TypeError: string indices must be integers, not 'str'`，或 `KeyError: 'category'`。
- Phase 5 即使前端看起来"写入完成"，Plan 的 `daily_plans` 常常是空的，导致 Phase 5 → 7 自动转阶段迟迟不触发。
- Phase 7 `generate_summary` 间歇性抛 `TypeError: object of type 'int' has no len()`，或在 `budget` 不是 dict 时崩溃。

## 根因

1. `Location.from_dict` 直接 `d.get("lat")`、`d.get("lng")`，但 LLM 经常把 `location` 直接写成字符串（如 `"明治神宫"`）。字符串进来时 `d.get()` 根本不存在，更糟的是当它是 dict 但缺少字段时也会报错。
2. `Activity.from_dict` 用了 `d["category"]` 这种硬取键，LLM 省略 `category` 时直接 `KeyError`；同时它把 `location` 直接丢给 `Location.from_dict` 而不做类型检查。
3. `DayPlan.from_dict` 没做 `isinstance(d, dict)` 校验，非 dict 输入会在下游抛一堆难以定位的错误。
4. `generate_summary` 在字段宽度上假设了太多：
   - `days` 必须是 list；LLM 有时把它当成 `total_days` 直接传整数。
   - `budget` 必须是 dict；LLM 偶尔只传一个金额数字，或直接传 `{"total": 8000}`。
   - `activities` 元素必须是 dict；LLM 偶尔塞字符串进来。
5. Phase 5 prompt 里对 `daily_plans` 的字段形状只描述了"每天应该包含哪些字段"，没有写清"`location` 必须是 dict"、"`category` 必须提供"这类硬约束；`update_plan_state` 的参数描述也只字未提 `daily_plans` 的嵌套结构，导致 LLM 没有学习信号纠正自己。

## 修复

### 1. dataclass 宽容化（`backend/state/models.py`）

- `Location.from_dict`：接受 `str`、`None`、部分字段缺失的 `dict`，并支持 `address` 作为 `name` 的别名；`lat` / `lng` 非数字时降级为 `0.0`。
- `Activity.from_dict`：
  - 非 dict 输入抛 `TypeError`（保持调用点错误可见性）。
  - `location` 通过 `Location.from_dict(d.get("location"))` 统一处理，不再直接下标访问。
  - `category` 使用 `str(d.get("category") or "activity")`，缺省时落到 `"activity"`。
  - `cost`、`transport_duration_min`、`notes` 等字段全部采用 `get(..., default) or default` 的容错写法。
- `DayPlan.from_dict`：
  - 非 dict 抛 `TypeError`。
  - `day` 支持 `int` / 可转 `int` 的字符串。
  - `activities` 空/缺失时安全降级为 `[]`。

这些变更只放松"输入形状"，不改变输出契约——测试里精确值断言仍然成立。

### 2. `generate_summary` 类型宽容（`backend/tools/generate_summary.py`）

重写 `generate_trip_summary`：

- `plan_data` 非 dict 时降级为空 dict。
- `days` 支持以下形态：
  - `list[dict]`（原始契约）
  - `int`（当作 `total_days` 处理）
  - 缺失时允许 `daily_plans` 作为别名
- 当 `days` 是 list 时 `total_days = len(days)`；否则优先使用 `plan_data["total_days"]`，再退化到 `int(raw_days)` 或数字字符串解析。**注意**：实现里要优先判断 `total_days` 是否真的存在（而不是默认填 0），否则会遮蔽 `days: int` 的分支；这正是第一次修复迭代时翻过的跟头。
- `budget` 支持 dict 或纯数字；dict 形态优先加总 `flights/hotels/activities/food`，没有时回退 `budget_raw.get("total")`；纯数字形态直接当总预算。
- 构造每日摘要时跳过非 dict 的 `day`，跳过非 dict 的 `activity`，避免 `a.get(...)` 崩溃。

### 3. Phase 5 Prompt 硬约束（`backend/phase/prompts.py`）

在 Phase 5 prompt 的 `assemble` 小节追加了 `DayPlan` 的**严格 JSON 结构示例**和一份硬约束清单：

- `activities` 必须是 list；每个元素必须是 dict。
- `activity.location` 必须是 `{name, lat, lng}` dict，不能传字符串。
- `start_time`、`end_time` 必须是 `"HH:MM"` 字符串。
- `category` 必须提供，使用 `shrine/museum/food/transport/activity` 这类短词。
- `cost` 必须是数字（没有时填 0，不要写"免费"）。
- `day` 是整数、`date` 是 `"YYYY-MM-DD"`。
- 追加单天用 dict、整体提交多天用 `list[dict]`，不要混用。

这组约束给 LLM 明确的"什么是对的""什么会被丢弃"的信号。配合宽容化的 dataclass，即使个别字段仍然漂，Phase 5 也不会被单条异常打断。

### 4. `update_plan_state` 参数描述补充（`backend/tools/update_plan_state.py`）

在 `value` 参数的描述里追加：

> daily_plans 传单个 dict 追加一天，传 list[dict] 整体替换全部天数；每个 activity 必须是 dict，且 location 必须是 `{"name":..,"lat":..,"lng":..}` dict，start_time/end_time 必须是 "HH:MM" 字符串，category 必须提供，cost 必须是数字。

让 LLM 在 tool schema 层面也能看到同一份硬约束，而不是只能依赖 phase prompt。

## 测试覆盖

所有新增测试都跟已有 fixture / 风格保持一致：

### `backend/tests/test_state_models.py`

- `test_location_from_dict_tolerates_string` / `tolerates_none` / `tolerates_partial_dict_with_address_alias` / `tolerates_non_numeric_lat_lng`
- `test_activity_from_dict_with_string_location_and_missing_category`
- `test_activity_from_dict_raises_on_non_dict`
- `test_day_plan_from_dict_tolerates_loose_activities`
- `test_day_plan_from_dict_raises_on_non_dict`

### `backend/tests/test_generate_summary.py`

- `test_generate_summary_tolerates_days_as_int`
- `test_generate_summary_accepts_daily_plans_alias`
- `test_generate_summary_tolerates_numeric_budget`
- `test_generate_summary_tolerates_total_days_only`
- `test_generate_summary_tolerates_string_activities`
- `test_generate_summary_tolerates_non_dict_plan`

### `backend/tests/test_update_plan_state.py`

- `test_daily_plans_tolerates_string_location_and_missing_category`
- `test_daily_plans_list_replaces_with_loose_shapes`
- `test_daily_plans_rejects_scalar`

### 已有的 Phase 5 → 7 集成 / E2E 回归

- `tests/test_appendix_issues.py::TestA2DailyPlansBlocked::test_phase5_to_7_transition_after_daily_plans`：覆盖 `update_plan_state → DayPlan.from_dict → phase_router` 的 transition 链路。
- `tests/test_e2e_golden_path.py`：模拟 LLM stub 驱动 Phase 1→3→5→7 全流程，包含 `daily_plans` 5 天的整体 list 提交。

### 本次完整回归执行结果

```
tests/test_state_models.py          13 passed
tests/test_generate_summary.py       8 passed
tests/test_update_plan_state.py     18 passed
tests/test_appendix_issues.py        8 passed
tests/test_phase_router.py          21 passed
tests/test_phase_integration.py      6 passed
tests/test_phase34_merge.py         22 passed
tests/test_e2e_golden_path.py        1 passed
tests/test_backtrack_service.py      6 passed
========= 103 passed =========
```

### 真实 UI 烟雾测试

通过 Playwright MCP 驱动前端 `http://localhost:5173`，输入"我想4月中旬去大阪玩3天，预算6000"。观察到：

- `update_plan_state` 成功触发 4 次（destination / dates / budget / travelers 等）。
- `web_search` 成功触发 1 次。
- 助手输出 bubble 正常流式返回。

Phase 1 → 3 的工具调用链路与前端渲染均正常，配合前面的 golden path E2E 覆盖 Phase 5→7 transition，本次修复的完整链路回归闭环。

## 设计取舍

- **为什么不在 `update_plan_state` 里做校验而是让 dataclass 宽容化？** 校验点放在入口确实定位更早，但会把容错逻辑拆成两半：`update_plan_state` 一份、反序列化路径一份。集中在 `from_dict` 里处理，可以顺便兼顾从磁盘 `load` 出的历史脏数据。
- **为什么不直接 reject 非法 shape？** LLM 的输出在边缘情况很难 100% 对齐 schema，一旦 reject 就会让整个阶段卡住、进而触发重试循环，这对用户体验是灾难。我们改成"能解析就解析、硬错才抛"，并用 prompt 硬约束引导 LLM 自我收敛。
- **为什么 Phase 5 prompt 改得比较"硬"？** Phase 5 的产物要直接落到结构化 DAG 里，没有模糊空间；而 Phase 1/3 的产物本身就更偏对话，不需要 schema 级硬约束。两边的宽严梯度是刻意的。

## 相关提交

修复集中在以下一次修改中：

- `backend/state/models.py`：`Location` / `Activity` / `DayPlan` `from_dict` 宽容化。
- `backend/tools/generate_summary.py`：`generate_trip_summary` 类型宽容重写。
- `backend/phase/prompts.py`：Phase 5 prompt 追加严格 JSON 结构示例与硬约束。
- `backend/tools/update_plan_state.py`：`value` 参数描述补充 `daily_plans` 结构提示。
- `backend/tests/test_state_models.py`、`test_generate_summary.py`、`test_update_plan_state.py`：单元回归。
