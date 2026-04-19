# Phase 5 Worker Convergence Guards Design

## 背景

Phase 5 并行模式中的 Day Worker 只有只读工具，没有状态写入工具。当前 worker 的收敛条件是：某一轮不再发起 tool call，并且最终文本中包含合法的 DayPlan JSON。

这导致 worker 很容易陷入如下补救链：

`get_poi_info -> 信息不完整 -> web_search -> check_availability -> 仍不完整 -> 继续搜索`

一旦 prompt 强调“信息不足必须继续补齐”，而 loop 又没有重复查询抑制、补救链上限、后半程强制收口机制，worker 就会在 30 轮内持续探索而不产出 JSON。

## 问题定义

当前不收敛问题由两层因素共同造成：

1. Prompt 层鼓励无限补救
2. Loop 层缺乏显式停止条件和收口机制

只改提示词可以缓解，但无法保证收敛；只改 loop 可以强行收口，但如果 prompt 仍鼓励“必须查到底”，模型会持续与守卫对抗，效果不稳定。

因此需要同时做两层修复：

1. 提示词止血：把“无限补齐”改成“有限补救 + 保守落地”
2. Loop 守卫：在 worker 执行器中加入重复查询抑制、补救链阈值、后半程强制收口、JSON 修复回合

## 目标

1. 显著降低 Day Worker 在 30 轮内不收敛的概率
2. 在信息不完整时，优先生成保守可执行的 DayPlan，而不是无限搜索
3. 不放宽到“随意编造事实”，仍然禁止捏造明确营业时间、票价、预约要求等事实字段
4. 保持现有 Phase 5 orchestrator-worker 架构不变

## 非目标

1. 不重构 Phase 5 orchestrator 为新的阶段机系统
2. 不修改串行 Phase 5 主循环逻辑
3. 不改现有工具 schema
4. 不新增用户可配置项到 `config.yaml`

## 方案概述

本设计采用双层收敛方案。

### 第一层：提示词止血

修改 `backend/agent/worker_prompt.py` 中的 Day Worker 角色约束，使其从“只要信息不足就继续补齐”调整为“有限补救后保守落地”。

新规则：

1. `get_poi_info`、`check_availability`、`web_search` 可以用于补充关键信息，但对同一 POI/同一问题的补救次数有限
2. 如果已经具备区域、主题、核心活动和大致时间结构，应优先输出保守版 DayPlan
3. 当工具仍无法补齐细节时，可以基于骨架、区域连续性、常识性节奏安排生成保守版行程
4. 不得编造具体营业时间、具体票价、明确预约要求
5. 无法确认的事实应写入 `notes`，而不是继续无限搜索

提示词止血的目标不是让模型“用既有知识代替事实”，而是明确允许它在事实不完整时停止继续搜索，并产出附带不确定性说明的可执行结果。

### 第二层：Loop 收敛守卫

修改 `backend/agent/day_worker.py`，在现有 loop 外增加轻量收敛控制，而不是重写 orchestrator 或引入新的复杂阶段机。

新增 4 类机制：

1. 重复查询抑制
2. 补救链阈值
3. 后半程强制收口
4. JSON 修复回合

## 详细设计

### A. 提示词调整

修改文件：`backend/agent/worker_prompt.py`

现有 prompt 中的“工具回退策略”会促使模型在专项工具返回无效信息时持续调用 `web_search`。本次改为以下语义：

1. 关键缺失信息可以补查，但单个 POI 或单类问题只允许有限次补救
2. 如果核心行程结构已成立，不要为了缺失的细枝末节持续搜索
3. 缺失事实不得编造，应在 `notes` 中说明，例如：
   - “营业时间需出发前二次确认”
   - “票价未查到，以现场公示为准”
4. 当系统提示已进入收口模式时，必须停止继续调工具并直接输出 DayPlan JSON

同时补充一条明确约束：

> 信息不完整时允许保守落地，但不允许捏造精确事实字段。

### B. Worker 执行状态

修改文件：`backend/agent/day_worker.py`

在 `run_day_worker()` 内维护一个轻量运行时状态对象或局部变量集合，至少包括：

1. `tool_call_count`：累计工具调用次数
2. `repeated_query_counts`：按 query 指纹记录重复次数
3. `poi_recovery_counts`：按 POI 或问题标识记录补救链次数
4. `forced_emit_mode`：是否已进入强制收口模式
5. `emit_repair_attempted`：是否已执行过一次 JSON 修复回合

这些状态仅存在于单个 worker 执行期间，不写入 plan，不暴露到外部配置。

### C. 查询指纹与重复查询抑制

修改文件：`backend/agent/day_worker.py`

为 `get_poi_info`、`check_availability`、`web_search` 的调用参数生成归一化指纹，用于识别近似重复查询。

建议规则：

1. `web_search`：使用归一化后的 `query`
2. `get_poi_info`：使用 `query` 或 `name` 作为核心键
3. `check_availability`：使用 `place_name + date`

当同一指纹超过阈值时，不再继续放任模型重复查询，而是：

1. 将 worker 切换到 `forced_emit_mode`
2. 向消息历史中注入一条明确 system 提示，要求基于已有信息直接输出 JSON

阈值不进入配置文件，先以内置常量形式实现。

### D. 补救链阈值

修改文件：`backend/agent/day_worker.py`

识别同一 POI 上的连续补救链，例如：

1. `get_poi_info(POI A)`
2. `web_search(POI A 开放时间)`
3. `check_availability(POI A, date)`
4. 再次 `web_search(POI A 营业时间)`

当同一 POI 或同一核心问题的补救次数超过阈值后：

1. 不再继续允许该补救链扩张
2. 切入 `forced_emit_mode`
3. 明确提示模型：缺失细节写入 `notes`，不要继续查

这一步是本次设计的核心，直接限制“信息补齐型读工具循环”。

### E. 后半程强制收口

修改文件：`backend/agent/day_worker.py`

在达到总迭代数的后半段时，worker 不再保持完全开放式搜索，而是进入收口优先模式。

行为规则：

1. 前半程允许正常探索和补救
2. 达到预设比例后，如果仍在频繁调工具，则追加一条 system 提示：
   - 已收集足够信息
   - 禁止继续为细节反复搜索
   - 立即输出 DayPlan JSON
3. 如果已经进入 `forced_emit_mode`，后续每轮都维持收口导向，直到模型输出 JSON 或预算耗尽

此机制用于给模型一个明确的模式切换信号：从“继续收集”切换到“立即产出”。

### F. 无 JSON 时的单次修复回合

修改文件：`backend/agent/day_worker.py`

当前逻辑下，只要某轮没有 tool call 且未提取到合法 JSON，就直接失败。这个策略过于脆弱。

新逻辑：

1. 若某轮无 tool call 且提取失败
2. 若尚未执行过格式修复
3. 则追加一条 system/user 级明确提示，要求：
   - 只输出一个 JSON 代码块
   - 必须包含 `day`、`date`、`activities`
   - 不要附加解释性文字
4. 再给模型 1 次机会
5. 若仍失败，再返回 `JSON_EMIT_FAILED`

这一步用于修复“实际上已准备收口，但格式没对齐”的可恢复失败。

### G. 错误分类增强

修改文件：`backend/agent/day_worker.py`、`backend/agent/orchestrator.py`

当前 worker 失败信息主要是自然语言字符串，不利于定位收敛问题。

新增明确错误类别，至少包括：

1. `REPEATED_QUERY_LOOP`
2. `RECOVERY_CHAIN_EXHAUSTED`
3. `JSON_EMIT_FAILED`
4. `ITERATION_BUDGET_EXHAUSTED`
5. `WORKER_TIMEOUT`

orchestrator 不改变调度逻辑，但应在日志和失败汇总中保留这些错误类型，便于后续 trace、测试和回归分析。

## 数据流变化

修改前：

1. LLM 调工具
2. 工具结果写回消息历史
3. 下一轮继续自由决策
4. 直到某轮无 tool call 且刚好输出合法 JSON

修改后：

1. LLM 调工具
2. worker 统计查询指纹、补救链和预算消耗
3. 若检测到重复查询或补救链过长，则切入 `forced_emit_mode`
4. 若迭代进入后半程，则主动追加收口提示
5. 若无 tool call 但 JSON 不合法，则触发一次格式修复回合
6. 若修复仍失败或预算耗尽，则返回带错误类别的失败结果

## 测试策略

新增或扩展 `backend/tests` 中与 worker 相关的测试，覆盖以下场景：

1. 重复 `web_search` query 被识别并触发收口
2. 同一 POI 的补救链超过阈值后进入 `forced_emit_mode`
3. 后半程能注入收口提示并阻止持续探索
4. 无 tool call 但 JSON 不合法时，会触发一次修复回合
5. 修复回合成功时 worker 返回成功
6. 修复回合失败时返回 `JSON_EMIT_FAILED`
7. 错误类别能被 orchestrator 汇总与保留

测试以最小增量为主，不新增端到端大改动。

## 风险与权衡

### 风险 1：过早收口导致结果质量下降

如果阈值过紧，worker 可能在信息不足时过早停止搜索。

缓解方式：

1. 前半程仍允许正常探索
2. 只对明显重复或明显过长的补救链进行切断
3. 缺失事实通过 `notes` 暴露，而不是伪造

### 风险 2：重复查询归一化不准确

若 query 指纹规则过粗，可能把合理的新查询误判为重复。

缓解方式：

1. 首版仅覆盖最常见的三个工具
2. 采用保守归一化策略
3. 测试中覆盖“近似但不相同”的情况

### 风险 3：prompt 与 loop 守卫不一致

如果 prompt 仍鼓励无限补齐，而 loop 在后半程要求停止，模型会反复拉扯。

缓解方式：

1. 同步修改 `worker_prompt.py`
2. 在进入 `forced_emit_mode` 时追加明确 system 提示，覆盖默认探索倾向

## 兼容性与边界

1. 不影响串行 Phase 5 模式
2. 不改变 orchestrator 的外部接口
3. 不改变 Day Worker 的输入输出结构，只增强内部控制逻辑与错误分类
4. 与现有 `web_search` phase 5 可用性改动兼容

## 实施范围

预计涉及文件：

1. `backend/agent/worker_prompt.py`
2. `backend/agent/day_worker.py`
3. `backend/agent/orchestrator.py`
4. `backend/tests/...` 中的 worker / orchestrator 相关测试文件

## 验收标准

满足以下标准即可视为设计落地成功：

1. Day Worker 不再因为同一 POI 的补救链无限扩张而耗尽 30 轮
2. 无 tool call 但 JSON 格式错误时，存在一次自动修复机会
3. 当事实信息不足时，worker 能输出带 `notes` 的保守版 DayPlan，而不是继续无限搜索
4. 失败结果具备明确错误类别，便于后续诊断
5. 现有并行 orchestrator 架构保持不变
