# Profile 记忆召回机制优化设计

> 记录时间：2026-04-21
> 背景：当前 v3 记忆系统已经分出 Profile / Working Memory / EpisodeSlice 三层，但 Profile 的召回仍依赖固定注入 + 轻量规则触发。为了让 Agent 在攻略共建过程中能更准确地调用用户的长期画像，我们重新梳理了当前现状、不足，并确定了一条“硬规则短路 → LLM gate → 召回 query 工具 → 规则召回 → LLM rerank → 注入上下文”的升级路径。
>
> 本文只覆盖 **Profile 的召回链路**。EpisodeSlice 本轮不进入主召回，但会在接口上预留第二路历史案例检索位，以便后续扩展。

---

## 1. 当前记忆召回机制现状

### 1.1 记忆分层现状

当前 v3 记忆系统已落地四个层次，边界为：

```text
TravelPlanState    = 当前这次旅行的权威事实（目的地/日期/预算/骨架/日程等）
Profile            = 跨旅行的长期画像（约束/拒绝/稳定偏好/偏好假设）
WorkingMemory      = 当前 session/trip 的临时推理材料
EpisodeSlice       = 过去完整旅行生成的可召回经验切片
```

其中 Profile 是本次讨论的主角。它由 `MemoryProfileItem` 组成，分 4 个 bucket：

- `constraints`：跨旅行硬约束（例：“不坐红眼航班”）
- `rejections`：明确否决项（例：“不住青旅”）
- `stable_preferences`：多次观察或明确声明的稳定偏好
- `preference_hypotheses`：单次观察得到的偏好假设

每条 `MemoryProfileItem` 至少具备：`domain / key / value / polarity / stability / confidence / status / context / applicability / recall_hints / source_refs / created_at / updated_at`。

### 1.2 召回链路现状

当前 chat 主路径在 `MemoryManager.generate_context()` 中同时做两件事：

1. **固定注入**
   - 从 profile 中拉取 `constraints / rejections / stable_preferences` 的 active 条目，最多 10 条
   - 从当前 session 的 working memory 拉取 active 条目，最多 10 条
   - 每轮都会被注入到 system prompt
2. **按查询触发的 symbolic recall**
   - 只有当用户消息命中“历史/习惯类触发词”（如“上次”“之前”“按我的习惯”“我是不是说过”等）时才启动
   - 启动后走 `build_recall_query()`，基于规则抽取 domain/entities/keywords
   - 再调用 `rank_profile_items()` 做一次规则打分（bucket/domain/keyword/recency）
   - 额外命中 top 5 条 profile 条目注入 prompt
   - 同一路径也会 rank episode slices，但本轮不讨论

### 1.3 提取侧现状（用于对齐召回设计）

每轮用户消息进入 chat 后，会异步：

1. 先跑 memory_extraction_gate，判断这轮是否值得提取
2. 值得则跑 memory_extraction，产出 profile 更新 + working memory
3. 经 policy 分类后写入 `profile.json` / `sessions/{session_id}/trips/{trip_id}/working_memory.json`

extraction 已经是“**gate + 结构化抽取**”的两段式，而 recall 目前仍然是“**固定注入 + 被动规则触发**”，两者并不对称。本次优化的主目标就是把 recall 也做成和 extraction 同样结构清晰的两段式流程。

---

## 2. 当前记忆召回机制的不足

结合 Profile 当前实现与使用场景，召回侧有 4 个可见短板：

### 2.1 触发判断依赖手写触发词，容易漏召回

`should_trigger_memory_recall()` 用一小组中文关键词（“上次/之前/按我的习惯/我是不是说过”等）判断是否启动 query-aware recall。问题：

- 用户表达方式多样：“按我之前去日本的习惯”“我一般比较怕累”“我之前说过不要太赶吧？”——任何触发词漏写都直接导致漏召回
- 同义换句、方言化表达、隐性回忆请求都难以覆盖
- 漏召回的代价远大于多召回（一次“你怎么又忘了”比一次多 100 tokens 严重得多）

### 2.2 召回 query 本身是“规则拆句”，缺少意图理解

`build_recall_query()` 当前靠正则/关键词提取 domain / destination / keywords。它不具备：

- 意图识别（到底是在问“我的习惯”还是“这次方案”）
- 同义改写（“别太赶”≈“慢节奏”）
- 跨 domain 联想（“怕累” → `pace`）
- 严格度判断（精确回忆 vs 泛化参考）

结果是：规则层拿到的 query 质量受限，再往下命中率会被进一步压低。

### 2.3 命中完全由规则打分决定，缺少“当前上下文”的最终裁决

现在排序只看：

- bucket 优先级
- exact domain / keyword match
- recency

但在真实对话中，命中度还取决于：

- 当前正在推进的 phase 和话题
- 用户这句话真正想参考哪一类习惯
- 候选条目之间是否互相冲突
- 哪条条目迁移到本次 trip 场景仍然有效

规则层对这些维度是“盲”的，容易把相关但不适合的条目塞进 prompt，反而污染推理。

### 2.4 recall 与 extraction 不对称，工程形态不统一

Extraction 已经是 `gate → 结构化 job`，走后台 internal task，输出严格 schema。  
Recall 仍然是“固定注入 + 正则触发规则召回”，没有 gate，也没有结构化 query。  
这带来两个后果：

- 形态不统一，难以复用已有的异步/可观测机制
- 无法在 trace 里清晰地看到“这轮为什么召回 / 召回了什么 / 为什么不召回”，调试成本高

---

## 3. 优化路径：两段式 Profile 召回管线

### 3.1 目标链路

```text
用户消息
  ↓
[Stage 0] 硬规则短路
  ├─ 明显 current-trip 问题 → 不进 recall
  └─ 明显历史/习惯类提问 → 强制 recall
  ↓
[Stage 1] LLM Recall Gate
  判断这轮是否需要从 Profile 召回
  ↓  （needs_recall == true）
[Stage 2] Recall Query Tool（强制调用的结构化工具）
  由 LLM 通过工具产出“按哪些字段召回”的 retrieval plan
  ↓
[Stage 3] 规则召回 Profile 候选
  按 plan 在 profile.json 中做字段化匹配 + 可解释打分
  ↓
[Stage 4] LLM Rerank
  对候选小集合做上下文相关性精排
  ↓
[Stage 5] 格式化注入
  带 bucket / matched reason / applicability 一起注入 system prompt
```

每一阶段都应有 telemetry，接入现有 `memory_recall` internal task 卡片。

### 3.2 为什么这条路径是合理的

1. **和 extraction 对称**  
   Gate + 结构化 tool 两段式，和记忆提取的 `gate → extraction` 一致，整个记忆系统的形态收敛，便于统一观测、调试、异常处理。

2. **解决了当前的 4 大短板**
   - 漏召回：由 LLM gate + 硬规则兜底共同决定，覆盖能力远强于手写触发词
   - query 质量：由 LLM 通过工具产出结构化 retrieval plan，比正则规则强
   - 上下文裁决：引入 rerank，让最后一关带上当前对话语境
   - 工程对称：recall 也走结构化输出 + 可追踪任务，形态统一

3. **工具作为召回 query 入口的合理性**  
   在本项目中，工具已经是结构化输入的统一收口。Recall query 以工具形式出现，优点：
   - 可被框架“强制调用”（非主 Agent 自由选择是否使用）
   - 参数 schema 固定，复用现有工具校验/错误反馈链路
   - 以后要扩展“第二路 EpisodeSlice 召回”，只需增加 source 字段或并行第二个工具，不需要重做架构

4. **规则召回 + LLM rerank 的分工是目前最稳形态**
   - 规则层保证可控、可解释、可审计
   - rerank 负责“把最后几条真正该注入的挑出来”，避免把 profile 整体塞入 prompt
   - 规则在前 rerank 在后，即使 rerank 失败也有规则 top-K 兜底，整条链路不会崩

### 3.3 各阶段职责与边界

| 阶段 | 输入 | 输出 | 关键约束 |
|------|------|------|----------|
| Stage 0 硬规则短路 | 用户消息 | `skip_recall` / `force_recall` / `undecided` | 仅识别最明显的两端情况 |
| Stage 1 LLM Recall Gate | 用户消息 + 轻量上下文 | `needs_recall: bool + reason` | 输出 schema 受控；false negative 代价高，应倾向触发 |
| Stage 2 Recall Query Tool | 用户消息 + 轻量上下文 | retrieval plan（source / buckets / domains / keywords / aliases / strictness / top_k / reason） | 工具强制调用；只输出“检索计划”，不决定最终命中 |
| Stage 3 规则召回 | retrieval plan + profile.json | 候选条目 + 每条 matched reason | 完全可解释；对 status/stability/bucket 做硬过滤 |
| Stage 4 LLM Rerank | 用户消息 + retrieval plan + 候选 top K | 最终 2~5 条 + 每条理由 | 只处理规则层筛出的小集合；规则兜底保留 |
| Stage 5 格式化注入 | 最终条目 | 注入 system prompt 的一段 memory block | 必须带 bucket / matched reason / applicability |

### 3.4 关键设计边界

- **Profile recall 不回答当前 trip 事实**  
  “这次预算多少 / 当前选了哪个骨架 / 我们几号出发”必须由 `TravelPlanState` 直接承担，不走 profile recall。Stage 0 负责拦截。

- **硬规则短路只处理两端，不抢 gate 的活**  
  中间地带一律交给 Stage 1 gate，避免规则越位导致漏召回或错召回。

- **工具只输出 retrieval plan，不做最终命中**  
  最终命中由 Stage 3 + Stage 4 共同决定。工具侧失败或返回空时，默认视为“本轮不 recall”，但 gate 强制触发时必须降级为规则默认 plan。

- **preference_hypotheses 默认不进固定注入**  
  仅在 retrieval plan 显式要求、或 rerank 明确选中时才以低权重注入，并在文案上标注“仅观测一次/待确认”。

- **规则打分字段可渐进扩展**  
  第一版以 `bucket / domain / key / stability / confidence / status / updated_at / context` 为主；`recall_hints` 和 `applicability` 在提取侧补齐后再加入打分，但 schema 位先留好。

### 3.5 可观测与失败降级

- 每个 Stage 的输入输出落入现有 `memory_recall` internal task，前端卡片展示：
  - gate 决策和原因
  - retrieval plan 摘要
  - 规则候选数量
  - rerank 选中条目与理由
- 降级策略：
  - gate 失败 → 采用硬规则结果，未命中则跳过 recall
  - tool 失败 → 采用默认 retrieval plan（当前 phase 常见 buckets + domains）
  - rerank 失败 → 采用规则 top K（按预设上限截断）
- 成本控制：
  - gate 和 rerank 使用轻量模型
  - retrieval plan 输出严格限长
  - candidate top K 与最终注入数量均有上限

---

## 4. 继续补全的后续方案

本文只定下路径和各阶段职责，下一步需要分头细化以下内容：

### 4.1 工具契约：`recall_query` tool
- 输入 schema：用户消息、当前 phase、可选当前 destination
- 输出 schema 字段建议：
  - `needs_recall: bool`
  - `source: "profile"`（为 EpisodeSlice 预留枚举）
  - `buckets: [constraints | rejections | stable_preferences | preference_hypotheses]`
  - `domains: [...]`
  - `keywords: [...]`
  - `aliases: [...]`
  - `strictness: "strict" | "soft"`
  - `top_k: int`
  - `reason: str`
- 强制调用策略：在 gate == true 或硬规则 force_recall 时强制走此工具

### 4.2 Stage 1 Gate 的 prompt 和 fallback 规则
- gate prompt 的骨架：角色、判定口径、典型 yes/no 例子、输出 schema
- fallback 规则：当 gate 响应异常或超时时，按“硬规则短路结果 + 默认保守触发”兜底

### 4.3 Stage 3 规则召回打分函数
- 必选字段：bucket / domain / key / stability / confidence / status / updated_at
- 打分维度：exact domain match、key overlap、keyword/alias match、stability 权重、recency 衰减
- 输出：候选列表 + 每条 matched reason，供 rerank 使用

### 4.4 Stage 4 Rerank 的输入输出契约
- 输入：用户消息、retrieval plan、候选 top K（含 matched reason）
- 输出：最终 2~5 条条目 id + 每条 final reason + 可选 strength（strong/weak）
- 兜底：若输出不合法，直接回退到规则 top K 前 N 条

### 4.5 Stage 5 注入格式
- 保留 source / bucket / matched reason / applicability
- 明确区分“长期画像固定注入”与“本轮命中记忆”两个 section

### 4.6 为 EpisodeSlice 预留接口
- 在工具 `source` 字段上预留 `episode_slice`
- 规则召回层以 strategy 方式实现（`ProfileRetriever` / 将来 `EpisodeSliceRetriever`）
- Rerank 输入支持多 source 混排，但默认只启用 profile
- Telemetry 同时按 source 分桶展示

### 4.7 评估与灰度
- 对比当前链路与新链路：
  - 命中 profile 条目数
  - 用户“你怎么又忘了”类反馈频率
  - recall 总延迟 / token 成本
- 灰度策略：
  - 按配置开关切换新旧链路
  - 异常时自动回退旧固定注入 + 正则触发逻辑

---

## 5. 结论

- 当前 Profile 召回是“固定注入 + 手写触发词规则”，存在漏召回、query 质量低、命中缺少上下文裁决、和 extraction 形态不对称四大问题。
- 本次确定的升级路径是 **硬规则短路 → LLM Recall Gate → 召回 Query 工具 → 规则召回 Profile 候选 → LLM Rerank → 格式化注入**。
- 这条路径既解决了现有短板，也和 extraction 的两段式形态对齐，符合本项目“工具作为结构化输入统一收口”的工程风格，并为未来引入 EpisodeSlice 第二路召回预留了接口。
- 本文不包含最终实现细节，下一步按 §4 列出的七项继续细化工具契约、gate prompt、规则打分、rerank 契约、注入格式、预留接口以及评估灰度方案。
