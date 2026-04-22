# 用户画像记忆提取与召回对齐设计

## 目标

把当前 `profile` 记忆提取链路补强到真正服务于现有 recall 算法，而不改变 recall 主体机制。重点解决三类问题：

1. profile extraction 没有强制产出 recall 需要的元数据
2. profile key / domain 缺乏统一规范，容易语义碎裂
3. hypothesis 到 stable 的稳定性升级过度依赖单轮 LLM 判断

## 非目标

- 不引入 embedding 检索
- 不重写 recall gate / retrieval plan / symbolic recall 主流程
- 不新增 working memory recall
- 不重做 episode slice 提取与召回

## 当前问题

### 1. recall 所需元数据不是 extraction 的一等输出

当前 recall 排序会使用 `applicability`、`recall_hints`、`source_refs` 等字段；但 profile extraction schema 并未要求 LLM 稳定输出这些字段。结果是 profile item 即使成功落库，也经常只能靠 `domain/key/value` 被命中，召回面偏窄、可解释性偏弱。

### 2. profile key 不稳定，语义易碎裂

当前只在 prompt 中要求 `key` 使用 snake_case，没有中心化的 key 归一规则。等价偏好可能被提成不同 key，导致后续 recall 无法把它们稳定视为同一画像。

### 3. stable / hypothesis 的边界没有系统证据累积

当前 bucket 排序已经假设 `stable_preferences` 比 `preference_hypotheses` 更可信，但落库时没有显式的跨轮证据累积和升级逻辑。这样会让 bucket 的可靠性过度依赖单轮 LLM 主观判断。

## 方案概览

本次改造保持“LLM 提取 + 规则 recall”的总路线不变，在 extraction 和保存层增加三道能力：

1. **Schema 对齐**：把 recall 真正会消费的字段正式纳入 profile extraction contract
2. **规范化对齐**：在落库前增加 profile normalization，把近义 key / domain / hints 收拢到统一表达
3. **稳定性对齐**：在保存 profile 时结合已有条目执行 hypothesis/stable 的跨轮升级

## 设计细节

### A. Profile Extraction Schema 升级

扩展 `MemoryProfileItem` 的提取 contract，使 profile extractor 稳定产出以下字段：

- `applicability`
  - 简短描述该画像适用于什么范围
  - 例如：`适用于大多数旅行。`
- `recall_hints`
  - 至少允许 `domains`、`keywords`、`aliases`
  - 用于 recall 时的同义表达和领域补充
- `source_refs`
  - 引用来源，至少保留当前轮消息级证据
  - 最低要求支持 `kind`、`session_id`、`quote`

这些字段进入 profile extraction tool schema，并在 prompt 中被明确要求：

- `applicability` 要简洁、去命令化
- `recall_hints.keywords` 提供贴近用户原话的短词
- `recall_hints.aliases` 提供常见同义说法
- `source_refs` 只保留最小必要来源，不引入敏感信息

### B. Profile Normalization 层

在 profile item 落库前增加一个纯 Python 规范化步骤，目标是减少碎片化。

规范化内容：

- `domain` 归一
  - 例如 `住宿` 统一归到 `hotel`
- `key` 归一
  - 建立中心化 canonical key 映射
  - 例如把 `dislike_spicy_food`、`no_spicy` 收拢到 `avoid_spicy`
- `recall_hints` 归一
  - 去重、排序、清洗空值
  - 把 canonical key 的默认 alias/hint 与 LLM 输出做并集
- `applicability` 兜底
  - LLM 未提供时，根据 bucket 提供保守默认文案

该层只做确定性变换，不依赖模型。

### C. 跨轮稳定性升级

在保存 profile updates 时，基于“规范化后的条目 + 已有 profile”执行升级判断。

核心规则：

- 相同 canonical identity 的条目再次被观察到时，累积证据
- `preference_hypotheses` 若重复命中且证据方向一致，则可升级到 `stable_preferences`
- 显式长期声明（如“以后都不…”）仍可直接进入稳定 bucket
- 发生明显冲突时，不自动升级，保留 pending 或维持 hypothesis

实现方式不引入新存储表，优先复用现有字段：

- `context` 中保存轻量聚合信息，如 `observation_count`、`last_evidence`
- `source_refs` 追加新的来源引用
- `confidence` 可做保守提升，但不无限累加

### D. 兼容性策略

此次改造不要求一次性迁移旧 profile 数据。

兼容原则：

- 旧条目缺失 `applicability` / `recall_hints` / `source_refs` 时仍可读取
- 新写入条目按增强 schema 生成
- recall 继续兼容空字段，但优先消费增强后的元数据

## 数据流变化

改造后的 profile 路径：

`decide_memory_extraction`
→ `extract_profile_memory`
→ `parse_v3_profile_extraction_tool_arguments`
→ `normalize_profile_item`
→ `merge_with_existing_profile_evidence`
→ `policy.classify_v3_profile_item`
→ `v3_store.upsert_profile_item`

其中 recall 路径不变，只是会读到更完整、更加规范的 profile item。

## 文件改动范围

主要改动文件：

- `backend/memory/extraction.py`
  - profile schema / prompt / parser
- `backend/memory/policy.py`
  - profile sanitize / 可能的稳定性分类协助
- `backend/main.py`
  - profile 保存流程接入 normalize + upgrade
- `backend/memory/v3_models.py`
  - 如需要，为聚合信息补充轻量字段约定
- `backend/tests/test_memory_extraction.py`
- `backend/tests/test_memory_policy.py`
- `backend/tests/test_memory_integration.py`

建议新增文件：

- `backend/memory/profile_normalization.py`
  - canonical key/domain/hints 归一逻辑
- `backend/tests/test_profile_normalization.py`

## 风险与取舍

### 1. 规则过强导致误归一

如果 canonical key 映射做得太激进，会把近似但不同的偏好合并。解决方式是先只覆盖高价值、高确定性的旅行画像领域，例如：

- food
- hotel
- flight
- train
- pace

### 2. 升级逻辑过松导致过早稳定化

如果 hypothesis 太容易升级成 stable，会放大错误记忆。解决方式是保守升级：

- 只对同 canonical identity 的重复观察升级
- bucket 升级时要求证据方向一致
- 对冲突信号维持 hypothesis 或 pending

### 3. LLM 输出字段增加后，tool payload 更重

profile extraction 输出会更长，但仍在可控范围内；而 recall 质量提升的收益大于这一点额外 token 成本。

## 测试策略

需要覆盖四层测试：

### 1. Schema / Prompt 测试

- profile extraction tool 必须要求新增字段
- profile extraction prompt 必须明确要求产出 `applicability` / `recall_hints` / `source_refs`

### 2. Normalization 单测

- 同义 key 会归一到 canonical key
- recall hints 会去重并合并默认 alias
- applicability 缺失时会保守补全

### 3. 保存与升级单测

- hypothesis 再次出现会升级为 stable
- 单次显式长期声明可直接稳定
- 冲突证据不会误升级

### 4. 集成测试

- API 聊天后写入的 profile item 含增强字段
- recall 能使用增强字段完成命中
- 旧条目仍兼容读取

## 成功标准

完成后应满足：

1. 新写入的 profile item 默认具备 recall 可用元数据
2. 高频用户画像领域的 key 不再明显碎裂
3. hypothesis / stable 的 bucket 可靠性提升，不再主要靠单轮 LLM 主观判断
4. 不改 recall 主流程的前提下，profile recall 的命中率和解释性提升
