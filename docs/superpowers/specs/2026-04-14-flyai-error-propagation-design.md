# FlyAI 工具错误透传设计

## 背景

`search_flights` 已完成一轮修复：

- `FlyAIClient` 能识别 CLI 的文本/MCP 错误
- 工具层不再把 FlyAI 的真实错误静默吞掉
- 当没有其它可用数据源时，前端能看到真实失败原因

但同一套问题仍存在于其它依赖 FlyAI 的工具中，尤其是：

- `search_trains`
- `quick_travel_search`
- `get_poi_info`

这些工具在 FlyAI 返回额度限制、鉴权失败、CLI 异常时，仍可能把真实原因降级成空结果或泛化错误，导致排查困难、提示误导。

## 目标

统一上述工具的 FlyAI 错误透传行为，使其与 `search_flights` 保持一致：

- FlyAI 单源工具：优先向上暴露真实错误原因
- 双源工具：保留成功分支的降级能力；只有在所有数据源都不可用时，才把真实错误汇总到最终 `ToolError`
- 补齐回归测试，覆盖额度限制等真实失败场景

## 非目标

- 不改动 FlyAI CLI 命令参数和搜索逻辑
- 不改动 Google/Amadeus 正常成功路径
- 不做新的重构抽象，例如新增统一错误中间层

## 方案选型

### 方案 A：沿用 `FlyAIClient` 抛异常，工具层统一转为 `ToolError`

做法：

- `FlyAIClient` 保持当前行为，识别文本/MCP 错误后抛出 `RuntimeError`
- 各工具根据自身是单源还是双源，决定是否降级或透传

优点：

- 与 `search_flights` 当前实现一致
- 改动面最小
- 测试边界清晰

缺点：

- 工具层仍需分别处理单源/双源差异

### 方案 B：`FlyAIClient` 返回结构化错误对象

做法：

- 所有调用都不抛异常，返回 `{ok: false, error: ...}` 之类的统一结构

缺点：

- 会影响所有现有调用方
- 需要整体重写现有工具层分支逻辑

### 结论

采用方案 A。

## 详细设计

### 1. `search_trains`

当前状态：FlyAI 单源。

变更：

- `flyai_client.search_train()` 抛出异常时，捕获并转换为带真实原因的 `ToolError`
- `raw_list` 为空时，仍保留现有 `No train results` 语义，因为这表示查询成功但没数据

示例行为：

- FlyAI 返回 `Trial limit reached...` -> `ToolError(... Trial limit reached ...)`
- FlyAI 正常返回空列表 -> `ToolError(No train results ...)`

### 2. `quick_travel_search`

当前状态：FlyAI 单源。

变更：

- `flyai_client.fast_search()` 抛出异常时，转换为带真实原因的 `ToolError`
- 正常返回空列表时维持当前结果结构，不额外改成功路径语义

说明：

- 该工具更偏探索型，不要求把空结果视为异常；但真实失败必须透传

### 3. `get_poi_info`

当前状态：Google Places + FlyAI 双源。

变更：

- 维持并发查询结构
- 如果 Google 成功、FlyAI 失败：记录 warning，继续返回 Google 结果
- 如果 Google 无结果且 FlyAI 失败：最终 `ToolError` 中带上 FlyAI 真实原因
- 如果 Google 未配置：保留现有 `Google Maps API key not configured` 逻辑，但若同时存在 FlyAI 真实错误，也应附加到最终原因列表里

### 4. 错误文案策略

- 单源工具：直接突出真实 FlyAI 错误
- 双源工具：保留 "No ... results from any source" 主语义，再把各源失败原因附加在括号中

目标是让前端一眼能区分：

- 是真的没搜到数据
- 还是 FlyAI/Google/Amadeus 某一路坏了

## 测试设计

新增/更新定向测试：

- `test_search_trains_surfaces_flyai_runtime_error`
- `test_quick_travel_search_surfaces_flyai_runtime_error`
- `test_get_poi_info_surfaces_flyai_error_when_google_empty`
- `test_get_poi_info_keeps_google_results_when_flyai_fails`

测试原则：

- 单源工具：异常透传和空结果语义分开验证
- 双源工具：验证“降级成功”和“最终透传”两种分支

## 风险与兼容性

- 风险较低，主要是错误提示文本变化
- 成功路径不变
- 失败路径会更具体，可能导致少量依赖旧模糊文案的测试需要更新

## 实施步骤

1. 先补失败测试
2. 修改 `search_trains`
3. 修改 `quick_travel_search`
4. 修改 `get_poi_info`
5. 跑定向测试
6. 更新 `PROJECT_OVERVIEW.md`
