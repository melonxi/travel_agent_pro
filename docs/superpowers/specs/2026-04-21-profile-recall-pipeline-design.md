# Profile Recall Pipeline 设计

> 记录时间：2026-04-21
> 范围：Profile recall 升级设计
> 策略：先完成全链路设计，再分阶段落地；第一阶段只实现 `Stage 0 硬规则短路 + Stage 1 LLM Recall Gate`

---

## 1. 背景与问题定义

当前 v3 记忆系统已经完成 Profile / WorkingMemory / EpisodeSlice 分层，但 Profile recall 仍然停留在：

- 固定注入 `constraints / rejections / stable_preferences`
- 用户消息命中手写触发词后，再走规则 query 和规则 rank

这套机制可以工作，但已经暴露出明显上限：

1. recall 触发依赖手写触发词，容易漏掉隐式表达和同义换句
2. recall query 主要依赖规则拆词，缺少语义理解和改写能力
3. 规则召回缺少基于当前语境的最终裁决，容易把“相关但不该注入”的条目放进 prompt
4. recall 形态与 extraction 已有的 gate/tool/schema 化流程不对称，工程上难以统一观测和调试

本次设计只覆盖 **Profile recall**。EpisodeSlice 本轮不进入主链路，但接口会预留扩展位。

---

## 2. 目标与非目标

### 2.1 目标

- 降低历史偏好/长期画像场景的漏召回率
- 提高 recall query 的结构化程度和可扩展性
- 保持 recall 结果可解释、可观测、可降级
- 明确 recall / extraction / current-trip facts 三者边界
- 为未来接入 EpisodeSlice 第二路召回预留接口

### 2.2 非目标

- 本轮不改造 extraction 链路
- 本轮不引入向量检索
- 本轮不重做 Profile 存储 schema
- 第一阶段不实现 reranker
- 第一阶段不把 recall gate 输入扩展到 phase、plan、历史消息

---

## 3. 现状回顾

当前 `MemoryManager.generate_context()` 的主路径包含两部分：

1. 固定注入
   - 每轮注入 active 的 `constraints / rejections / stable_preferences`
   - 每轮注入当前 session/trip 的 working memory
2. query-aware symbolic recall
   - 基于 `should_trigger_memory_recall()` 判断是否触发
   - 用 `build_recall_query()` 基于规则抽取 domains / destination / keywords
   - 用 `rank_profile_items()` 和 `rank_episode_slices()` 做规则排序

现有 recall 机制的主要问题：

- `should_trigger_memory_recall()` 本质上仍是关键词触发器
- `build_recall_query()` 仍是规则 query builder，不具备稳定的语义改写和意图归因能力
- `rank_profile_items()` 更像候选筛选器，不是上下文相关性的最终裁决器

与之对照，extraction 侧已经是：

- `memory_extraction_gate`
- `extract_memory_candidates`
- 严格 schema 输出

因此 recall 侧的升级目标不是“完全换一套记忆系统”，而是把 recall 逐步收敛到与 extraction 类似的工程形态。

---

## 4. 目标架构总览

最终目标链路如下：

```text
用户消息
  ↓
Stage 0: 硬规则短路
  ↓
Stage 1: LLM Recall Gate
  ↓
Stage 2: Recall Query Tool
  ↓
Stage 3: 规则召回器
  ↓
Stage 4: LLM Reranker
  ↓
Stage 5: 格式化注入
```

其中：

- `Stage 0` 只处理极明显的 skip / force case
- `Stage 1` 只回答“要不要 recall”
- `Stage 2` 只回答“按什么条件查”
- `Stage 3` 只负责可解释候选召回
- `Stage 4` 只负责最终上下文相关性裁决
- `Stage 5` 只负责格式化注入到 memory block

第一阶段上线范围：

- 实现 `Stage 0 + Stage 1`
- 后续仍复用现有 `build_recall_query()` 和 `rank_profile_items()`

---

## 5. 阶段职责边界

| 阶段 | 核心问题 | 输入 | 输出 | 不负责 |
|------|----------|------|------|--------|
| Stage 0 硬规则短路 | 是否存在极明显短路 case | `user_message` | `skip_recall / force_recall / undecided` | 不做中间地带语义判断 |
| Stage 1 Recall Gate | 要不要 recall | `user_message` | `needs_recall / intent_type / reason / confidence` | 不产出 retrieval plan |
| Stage 2 Recall Query Tool | 怎么查 | `user_message` + `gate_intent_type` | retrieval plan | 不决定最终命中项 |
| Stage 3 规则召回器 | 查出哪些候选 | retrieval plan + profile store | candidates + matched reasons | 不做最终上下文裁决 |
| Stage 4 LLM Reranker | 最终留哪些 | `user_message` + plan + candidates | selected items + reasons | 不扩大全量搜索 |
| Stage 5 格式化注入 | 如何注入 prompt | selected items | memory block | 不再改变候选集合 |

一句话总结：

```text
gate = 要不要查
query = 怎么查
retriever = 查出哪些候选
reranker = 最终留下哪些
```

---

## 6. 统一数据契约

### 6.1 RecallGateResult

```json
{
  "needs_recall": true,
  "intent_type": "profile_preference_recall",
  "reason": "user asks to apply prior long-term preference",
  "confidence": 0.84
}
```

字段约束：

- `needs_recall: boolean`
- `intent_type: enum`
- `reason: string`
- `confidence: number`，范围 `0 ~ 1`

`intent_type` 枚举：

- `current_trip_fact`
- `profile_preference_recall`
- `profile_constraint_recall`
- `past_trip_experience_recall`
- `mixed_or_ambiguous`
- `no_recall_needed`

### 6.2 RecallRetrievalPlan

```json
{
  "source": "profile",
  "buckets": ["stable_preferences", "constraints", "rejections"],
  "domains": ["hotel", "accommodation"],
  "keywords": ["住宿", "酒店", "常规偏好"],
  "aliases": ["住哪里", "酒店偏好", "住宿偏好"],
  "strictness": "soft",
  "top_k": 8,
  "reason": "user wants to reuse long-term accommodation preference"
}
```

字段约束：

- `source: "profile"`
- `buckets: string[]`
- `domains: string[]`
- `keywords: string[]`
- `aliases: string[]`
- `strictness: "strict" | "soft"`
- `top_k: int`
- `reason: string`

第一版不让 query tool 输出 `needs_recall`，避免与 gate 重叠。

### 6.3 RecallCandidate

```json
{
  "item_id": "profile_123",
  "bucket": "stable_preferences",
  "score": 0.91,
  "matched_reason": [
    "domain=hotel",
    "keyword=住宿",
    "stability=explicit_declared"
  ]
}
```

字段约束：

- `item_id: string`
- `bucket: string`
- `score: number`
- `matched_reason: string[]`

第一版建议统一分数语义为“越大越相关”。

### 6.4 RecallRerankResult

```json
{
  "selected_item_ids": ["profile_123", "profile_456"],
  "final_reason": "these items directly match the user's long-term accommodation preference",
  "per_item_reason": {
    "profile_123": "matches accommodation preference and remains applicable",
    "profile_456": "constraint still relevant to lodging choice"
  }
}
```

字段约束：

- `selected_item_ids: string[]`
- `final_reason: string`
- `per_item_reason: object`

第一版不为 reranker 新引入第二套分数体系。

---

## 7. Stage 0：硬规则短路设计

Stage 0 的设计目标只有一个：

- 用极少量高置信规则处理两端 case，避免明显问题还进入 LLM gate

输出枚举：

- `skip_recall`
- `force_recall`
- `undecided`

### 7.1 `skip_recall`

只拦明显 current-trip 权威事实问题，例如：

- `这次预算多少`
- `我们几号出发`
- `当前选了哪个骨架`
- `第三天安排是什么`

规则要求：

- 出现 `这次 / 本次 / 当前`
- 且问题明显指向 `TravelPlanState` 已承载的当前事实

### 7.2 `force_recall`

只拦明显历史/习惯追问，例如：

- `我是不是说过不坐红眼航班`
- `按我的习惯来安排`
- `我之前是不是不住青旅`
- `上次去京都住哪里`

规则要求：

- 命中明显历史指示词或“复用旧画像”的句式

### 7.3 `undecided`

其余所有 case 一律进入 LLM gate。Stage 0 不处理中间地带，避免规则越位。

---

## 8. Stage 1：LLM Recall Gate 设计

### 8.1 输入范围

第一版 gate 只看：

- `user_message`

不看：

- phase
- plan 摘要
- 最近多轮历史消息

这样做的原因：

- 接口最简单
- 成本最低
- 便于独立评估 gate 相对旧触发词规则的收益

### 8.2 判定口径

gate 回答的问题是：

```text
这轮用户消息，是否值得启动 query-aware Profile recall？
```

应判 `needs_recall=true` 的情况：

- 用户在追问自己以前表达过的偏好、约束、拒绝项
- 用户要求按自己过去习惯或长期偏好来安排
- 用户在借用过去旅行经验指导当前决策
- 用户虽然没出现显式历史词，但语义上是在调用长期画像

应判 `needs_recall=false` 的情况：

- 只是当前 trip 事实查询
- 只是当前方案调整
- 只是新的偏好表达，应走 extraction，不是 recall
- 只是泛泛评价，不是在调用过去记忆

关键边界：

```text
recall = 调用已有记忆
extraction = 沉淀新的记忆
```

例如：

- `还是按我常规偏好来` -> 更像 recall
- `我比较怕累` -> 更像 extraction

### 8.3 Prompt 设计原则

- gate 是判定器，不是问答器
- 输出必须严格符合 schema
- few-shot 覆盖：
  - 明确 recall
  - 明确不 recall
  - 模糊句子
- 模糊 case 的默认口径：
  - 像在复用旧画像 -> 偏向 `true`
  - 像在表达新偏好 -> 偏向 `false`

### 8.4 失败降级

- 若 Stage 0 为 `force_recall`，则 gate 失败仍继续 recall
- 若 Stage 0 为 `skip_recall`，则 gate 失败仍跳过 recall
- 若 Stage 0 为 `undecided`，则 gate 失败默认 `needs_recall=false`

第一版默认保守，先保证稳定性。

---

## 9. Stage 2：Recall Query Tool 设计

Stage 2 的职责不是判断是否 recall，而是把用户问题翻译成结构化 retrieval plan。

### 9.1 输入

```json
{
  "user_message": "住宿还是按我常规偏好来",
  "gate_intent_type": "profile_preference_recall"
}
```

### 9.2 输出语义

需要明确：

- 应从哪些 bucket 查
- 应从哪些 domain 查
- 需要哪些 keywords / aliases
- 应偏 `strict` 还是 `soft`
- 规则召回候选上限是多少

### 9.3 `strictness` 口径

- `strict`
  - 用户在问“我是不是明确说过 X”
  - 应偏精确匹配
- `soft`
  - 用户在问“按我习惯 / 常规偏好来”
  - 允许相关扩展和近义匹配

### 9.4 边界

- query tool 不重复判断 `needs_recall`
- query tool 不直接返回最终命中项
- query tool 工具失败时，必须回退到默认 retrieval plan

默认 retrieval plan：

```json
{
  "source": "profile",
  "buckets": ["constraints", "rejections", "stable_preferences"],
  "domains": [],
  "keywords": [],
  "aliases": [],
  "strictness": "soft",
  "top_k": 5,
  "reason": "fallback_default_plan"
}
```

---

## 10. Stage 3：规则召回器设计

规则召回器的目标不是给出最终最准答案，而是：

- 稳定地产出一个小而合理的候选集合
- 保持可解释、可审计、可回放

### 10.1 工作方式

- 先过滤，再打分

### 10.2 硬过滤

- `status != active` 直接丢弃
- bucket 不在 retrieval plan 范围内直接丢弃
- `strict` 模式下，至少满足 domain 或明确 keyword 命中
- `soft` 模式下，可接受 alias 或弱 keyword 命中

### 10.3 打分维度

建议第一版按以下顺序加权：

1. bucket 权重
2. domain 命中
3. keyword 命中
4. alias 命中
5. stability 权重
6. confidence 权重
7. recency 轻微加权

约束：

- recency 只能轻微影响，不能压过长期硬约束

### 10.4 输出约束

- 输出统一的 `RecallCandidate`
- `matched_reason` 尽量保留结构化原因列表
- 候选集大小由 `top_k` 控制

---

## 11. Stage 4：LLM Reranker 设计

Reranker 只处理规则层筛出的小候选集合。

### 11.1 输入

- `user_message`
- `retrieval_plan`
- candidate top K

### 11.2 核心判断

- 哪些候选最直接回应当前问题
- 哪些候选仍适用于当前语境
- 候选间是否存在重复、冲突或强弱层次

### 11.3 输出

- `selected_item_ids`
- `final_reason`
- `per_item_reason`

第一版建议 reranker 最终选择 `2 ~ 5` 条。

### 11.4 失败降级

- reranker 失败时直接回退到规则 top N
- 默认 `N = 3`

---

## 12. Stage 5：格式化注入与上下文拼装

注入阶段需要明确区分两类内容：

1. 长期画像固定注入
2. 本轮 recall 命中的记忆注入

建议在 prompt 中拆成两个 section，避免混在一起：

- `Fixed Profile Memory`
- `Recall Hits For This Turn`

本轮 recall 命中项建议至少保留：

- source
- bucket
- matched reason
- applicability

对于 `preference_hypotheses`：

- 默认不进入固定注入
- 仅在 retrieval plan 显式要求，或 reranker 明确选中时进入 recall block
- 注入时需要显式标注“弱证据 / 待确认”

---

## 13. 失败降级策略

| 阶段 | 失败时行为 |
|------|------------|
| Stage 0 | 视为 `undecided` |
| Stage 1 Gate | 若 Stage 0 已 `force_recall` 则继续；若已 `skip_recall` 则跳过；否则默认 `needs_recall=false` |
| Stage 2 Query Tool | 使用默认 retrieval plan |
| Stage 3 规则召回器 | 本轮 recall 结果为空，不进入 reranker |
| Stage 4 Reranker | 回退到规则 top N |

铁律：

- 任何降级都不能绕过 `status=active` 等硬过滤
- 任何降级都不能让 query tool 越权决定最终命中项

---

## 14. Telemetry 与评估方案

每轮 recall 建议记录以下字段：

- `stage0_decision`
- `stage0_reason`
- `gate_needs_recall`
- `gate_intent_type`
- `gate_confidence`
- `gate_reason`
- `query_plan_summary`
- `candidate_count`
- `selected_item_ids`
- `fallback_used`

前端 `memory_recall` internal task 至少应展示：

- Stage 0 是否命中短路
- gate 是否触发 recall
- query plan 摘要
- 规则候选数量
- reranker 最终选中条目和理由

评估指标建议分三类：

1. 召回效果
   - 历史/习惯类问题命中率
   - 用户“你怎么又忘了”类反馈频率
2. 精准度
   - 误召回率
   - 不相关注入比例
3. 成本与稳定性
   - recall 延迟
   - token 成本
   - fallback 命中率

---

## 15. 分阶段实施计划

### Milestone A：Stage 0 + Stage 1

- 实现硬规则短路
- 实现单层 LLM recall gate
- 保持后续仍使用现有 `build_recall_query()` 和 `rank_profile_items()`

目标：

- 先验证 gate 是否优于当前触发词逻辑

### Milestone B：Stage 2

- 引入 `recall_query` tool
- 用结构化 retrieval plan 替换现有规则 `build_recall_query()`
- 保留现有 `rank_profile_items()` / `rank_episode_slices()` 主体逻辑，通过 adapter 兼容新 plan

目标：

- 提高 query 质量，减少规则拆词上限
- 让 Stage 2 先收敛到目标 contract，但不在本阶段重写 Stage 3

#### Milestone B 细化边界

Milestone B 是“**只替换 query builder，不重写 retriever**”的阶段。目标链路为：

```text
Stage 0 硬规则短路
  ↓
Stage 1 Recall Gate
  ↓
Stage 2 Recall Query Tool
  输出 RecallRetrievalPlan
  ↓
Plan Adapter
  ↓
现有 rank_profile_items / rank_episode_slices
```

本阶段明确：

- 继续复用 Phase A 已落地的 Stage 0 / Stage 1
- `Recall Query Tool` 输入只看：
  - `user_message`
  - `gate_intent_type`
- 不让 query tool 读取 profile 摘要、trip summary 或历史消息
- 不让 query tool 重新输出 `needs_recall`
- 不在本阶段引入 reranker
- 不在本阶段统一 Profile / EpisodeSlice 双 source plan

#### Milestone B 推荐文件边界

- 新增 `backend/memory/recall_query.py`
  - `RecallRetrievalPlan`
  - `build_recall_query_tool()`
  - `build_recall_query_prompt()`
  - `parse_recall_query_tool_arguments()`
  - `fallback_retrieval_plan()`
- 新增 `backend/memory/recall_query_adapter.py`
  - `plan_to_legacy_recall_query()`
  - 只做 plan -> 旧规则召回输入的翻译，不做命中判断
- 修改 `backend/main.py`
  - gate 放行后调用 query tool
  - 处理 timeout / error / invalid payload -> fallback plan
  - 在 `memory_recall` telemetry 中记录 query plan 摘要与 fallback 来源
- 修改 `backend/memory/manager.py`
  - 接收 `retrieval_plan` 或 adapter result
  - 优先走新 plan 路径
  - 无 plan 时兼容旧路径

#### Milestone B 数据契约

Stage 2 输出的 `RecallRetrievalPlan` 维持本设计文档 §6.2 的结构：

```json
{
  "source": "profile",
  "buckets": ["stable_preferences", "constraints", "rejections"],
  "domains": ["hotel", "accommodation"],
  "keywords": ["住宿", "酒店", "常规偏好"],
  "aliases": ["住哪里", "酒店偏好", "住宿偏好"],
  "strictness": "soft",
  "top_k": 8,
  "reason": "user wants to reuse long-term accommodation preference"
}
```

Milestone B 限制：

- `source` 第一版只允许 `profile`
- `strictness` 只允许 `strict | soft`
- `top_k` 在解析层做 clamp，建议区间 `1~10`
- `reason` 必填但限长

adapter 输出建议为临时兼容对象，而不是直接污染旧 `RecallQuery`：

```python
@dataclass
class LegacyRecallQueryAdapterResult:
    domains: list[str]
    keywords: list[str]
    entities: dict[str, str]
    include_profile: bool
    include_slices: bool
    allowed_buckets: list[str]
    strictness: str
    matched_reason: str
```

其中：

- `aliases` 在本阶段并入 `keywords`
- `source=profile` -> `include_profile=true`
- `include_slices=false`
- `buckets` 通过 `allowed_buckets` 传给现有规则召回器
- `strictness` 通过轻量兼容字段传递，不在本阶段重写排序模型

#### Milestone B 决策口径

- query tool 只回答“怎么查”，不回答“要不要查”
- `strict`
  - 适用于“我是不是说过 X”这类核对明确记忆的请求
- `soft`
  - 适用于“按我常规偏好/习惯来”这类借用整体画像的请求
- `preference_hypotheses` 默认少用，仅在 query 很泛且其他 bucket 不足时进入 plan

#### Milestone B 失败降级

- gate=false：不进入 Stage 2
- gate=true + query tool 成功：使用 query tool plan
- gate=true + query tool timeout / error / invalid payload：使用 fallback retrieval plan

fallback retrieval plan 建议为：

```json
{
  "source": "profile",
  "buckets": ["constraints", "rejections", "stable_preferences"],
  "domains": [],
  "keywords": [],
  "aliases": [],
  "strictness": "soft",
  "top_k": 5,
  "reason": "fallback_default_plan"
}
```

约束：

- fallback 只在 gate 已经判定 `needs_recall=true` 时使用
- fallback 默认不带 `preference_hypotheses`
- fallback 不得让 query tool 越权决定最终命中项

#### Milestone B 测试策略

至少覆盖四层测试：

1. query tool schema / parser 单测
   - 合法 payload
   - 非法 enum
   - `top_k` clamp
   - `source != profile` 降级
2. adapter 单测
   - `keywords + aliases` 合并
   - `buckets -> allowed_buckets`
   - `strictness` 透传
3. manager / recall 路由测试
   - gate 放行后优先走 query tool，不再走旧 `build_recall_query()`
   - query tool 失败时 fallback 生效
4. 集成测试
   - query tool 成功路径
   - query tool 失败但请求成功路径

### Milestone C：Stage 3

- 统一规则召回器输出为 `RecallCandidate`
- 重构匹配原因和分数表达

目标：

- 为 reranker 提供稳定、统一、可解释的候选输入

### Milestone D：Stage 4

- 引入 LLM reranker
- 从规则候选中选出最终注入项

目标：

- 降低 prompt 污染，提高本轮相关性

---

## 16. 风险与待决策项

当前仍需明确或持续观察的点：

1. Gate 在 `undecided` 且失败时默认 `false`，是否会过于保守
2. Query Tool 第一版是否只支持 `source=profile`，还是直接预留 `episode_slice`
3. `preference_hypotheses` 何时进入 retrieval plan 和候选集合
4. Gate 是否长期保持只看 `user_message`，还是后续升级为 `message + phase + trip summary`
5. Reranker 的收益是否足够高，值得引入额外一次 LLM 调用

---

## 17. 结论

本设计将 Profile recall 拆成明确的五段式流水线：

- `Stage 0`：用极少量高置信规则做两端短路
- `Stage 1`：用单层 LLM gate 判断是否需要 recall
- `Stage 2`：用结构化 query tool 产出 retrieval plan
- `Stage 3`：用规则召回器产出可解释候选
- `Stage 4`：用 reranker 完成最终上下文裁决
- `Stage 5`：将 recall 命中结果格式化注入 prompt

其中第一阶段只落地 `Stage 0 + Stage 1`，后续先复用现有 recall 逻辑。这样可以在不一次性抬高复杂度的前提下，先验证 recall gate 的真实收益，再逐步推进 query、retriever、reranker 的结构化升级。
