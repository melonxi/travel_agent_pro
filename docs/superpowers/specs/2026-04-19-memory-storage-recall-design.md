# Memory Storage v3 分层重构设计

> **状态**：草案，待评审
> **日期**：2026-04-19
> **范围**：重构记忆存储与读取边界，使其适配 query-aware symbolic recall，不引入 embedding、向量库或 RAG。

---

## 1. 背景

当前记忆系统已经具备结构化 `MemoryItem`、后台候选提取、pending 确认、global/trip 双 scope、`TripEpisode` 归档和三路固定召回能力。

但 `docs/mind/2026-04-19-memory-system-upgrade-insight.md` 提出的核心问题仍然成立：如果 `MemoryItem(scope="trip")` 保存本次旅行事实、偏好、约束和阶段决策，它会与 `TravelPlanState` 抢夺同一份当前旅行真相，导致重复注入、权威来源不清、生命周期治理复杂和语义漂移。

同时，当前读取机制主要按 `scope`、`trip_id`、`phase domain` 固定召回，无法很好处理用户显式历史查询，例如：

- “我上次去京都住哪里？”
- “我之前是不是说过不坐红眼航班？”
- “按我以前喜欢的节奏来。”
- “上次带爸妈出游有什么坑？”

这些请求不需要向量语义检索，而需要一个可解释、可调试、字段化的 symbolic recall 存储基础。

---

## 2. 设计目标

本设计选择非兼容优先的 v3 分层重构，不再让一个通用 `MemoryItem` 承担所有记忆职责。

目标：

1. 消除 `MemoryItem(scope="trip")` 作为第二套 `TravelPlanState` 的结构性风险。
2. 把长期画像、当前工作记忆、历史归档、历史切片和行为证据拆成一等存储实体。
3. 让存储实体直接服务 query-aware symbolic recall，而不是依赖全文搜索或 embedding。
4. 保证 prompt 召回单位短、小、可追溯，并带适用边界。
5. 让写入端先分流，再落库，避免把当前旅行状态写进长期 memory。
6. 通过显式迁移脚本从 v2 迁到 v3，运行时不再双读旧格式。

非目标：

- 不引入 embedding、vector DB、RAG。
- 不实现跨用户共享画像。
- 不把 `TripEpisode` 全文注入 prompt。
- 不保留 `MemoryItem(scope="trip")` 作为新写入或新读取路径。
- 不在第一版实现复杂倒排索引；小数据量下用 JSON/JSONL 加内存过滤即可。

---

## 3. 总体架构

v3 后，当前旅行状态和记忆系统的边界固定为：

```text
TravelPlanState = 当前旅行事实和权威状态
profile.json = 跨旅行长期画像
working_memory.json = 当前 session/trip 的短期推理材料
episodes.jsonl = 完整历史旅行归档
episode_slices.jsonl = 历史证据召回单元
events.jsonl = 行为证据日志
```

读取路径：

```text
ContextAssembler
  ├─ TravelPlanStateReader
  ├─ ProfileRetriever
  ├─ WorkingMemoryRetriever
  └─ QueryAwareRecall
       ├─ ProfileSymbolicRetriever
       └─ EpisodeSliceRetriever
```

写入路径：

```text
ExtractionResult
  ├─ profile_updates          -> profile.json
  ├─ working_memory           -> sessions/{session_id}/working_memory.json
  ├─ episode_evidence         -> Phase 7 episode/slice 生成素材
  ├─ state_observations       -> 不写 memory，只用于诊断或 plan writer 校验
  └─ drop                     -> 不落库
```

---

## 4. 存储目录

v3 主格式：

```text
data/users/{user_id}/memory/
  profile.json
  events.jsonl
  episodes.jsonl
  episode_slices.jsonl
  sessions/{session_id}/working_memory.json
```

旧格式：

```text
data/users/{user_id}/memory.json
data/users/{user_id}/memory_events.jsonl
data/users/{user_id}/trip_episodes.jsonl
```

旧格式不再作为运行时主路径。迁移完成后移动到：

```text
data/users/{user_id}/legacy_memory_v2/
```

如果运行时发现用户没有 v3 文件，系统按“无记忆用户”处理，而不是自动降级读取 v2。迁移必须由显式脚本完成，避免旧 `trip` 记忆继续污染新读取链路。

---

## 5. 数据模型

### 5.1 `UserMemoryProfile`

`profile.json` 只保存跨旅行长期画像，不保存当前旅行事实，不保存当前 session 临时信号。

```json
{
  "schema_version": 3,
  "user_id": "default_user",
  "constraints": [],
  "rejections": [],
  "stable_preferences": [],
  "preference_hypotheses": []
}
```

四个桶的语义：

| 桶 | 含义 | 固定召回策略 |
|---|---|---|
| `constraints` | 跨旅行硬约束，例如“不坐红眼航班” | 高优先级固定召回 |
| `rejections` | 明确否决，例如“绝不住青旅” | 高优先级固定召回 |
| `stable_preferences` | 多次确认或用户明确声明的长期偏好 | 可固定召回 |
| `preference_hypotheses` | 单次观察形成的偏好假设 | 不固定召回，只在 query 命中时低权重召回 |

统一 profile item 结构：

```json
{
  "id": "constraint_flight_red_eye",
  "domain": "flight",
  "key": "avoid_red_eye",
  "value": true,
  "polarity": "avoid",
  "stability": "explicit_declared",
  "confidence": 0.95,
  "status": "active",
  "context": {},
  "applicability": "适用于所有旅行，除非用户明确临时允许。",
  "recall_hints": {
    "domains": ["flight", "transport"],
    "keywords": ["红眼航班", "夜航", "凌晨航班"],
    "aliases": ["red eye"],
    "priority": "high"
  },
  "source_refs": [
    {
      "kind": "message",
      "session_id": "s1",
      "quote": "以后我都不坐红眼航班"
    }
  ],
  "created_at": "2026-04-19T00:00:00",
  "updated_at": "2026-04-19T00:00:00"
}
```

`confidence` 和 `stability` 必须分开：

```text
confidence = 抽取得准不准
stability = 是否能跨旅行稳定复用
```

允许的 `stability`：

```text
explicit_declared
single_observation
repeated_confirmed
user_confirmed
```

### 5.2 `SessionWorkingMemory`

`working_memory.json` 保存当前 session/trip 内的短期推理材料。

```json
{
  "schema_version": 1,
  "user_id": "default_user",
  "session_id": "s1",
  "trip_id": "trip_123",
  "items": [
    {
      "id": "wm_001",
      "phase": 3,
      "kind": "temporary_rejection",
      "domains": ["attraction"],
      "content": "用户说先别考虑迪士尼，原因是不想太商业化。",
      "reason": "当前候选筛选阶段需要避免重复推荐。",
      "status": "active",
      "expires": {
        "on_session_end": true,
        "on_trip_change": true,
        "on_phase_exit": false
      },
      "created_at": "2026-04-19T00:00:00"
    }
  ]
}
```

适合进入 working memory 的内容：

- 临时否决但尚未形成正式约束。
- 当前方案的犹豫原因。
- 阶段切换 handoff 中对后续推理有用的提醒。
- 当前 session 内不能重复推荐或需要短期避让的信息。

不允许进入 working memory 的内容：

- 当前目的地、日期、预算、同行人等 `TravelPlanState` 权威字段。
- 跨旅行长期偏好。
- 敏感 PII、支付、会员信息。

### 5.3 `TripEpisode`

`episodes.jsonl` 保存完整历史归档，不直接进入 prompt。

```json
{
  "id": "ep_kyoto_2026",
  "user_id": "default_user",
  "session_id": "s1",
  "trip_id": "trip_123",
  "destination": "京都",
  "dates": "2026-05-01 to 2026-05-05",
  "travelers": {"adults": 2},
  "budget": {"amount": 20000, "currency": "CNY"},
  "selected_skeleton": {},
  "final_plan_summary": "...",
  "accepted_items": [],
  "rejected_items": [],
  "lessons": [],
  "created_at": "2026-04-19T00:00:00"
}
```

### 5.4 `EpisodeSlice`

`episode_slices.jsonl` 是历史召回的唯一 prompt 单元。

```json
{
  "id": "slice_ep_kyoto_2026_hotel_001",
  "user_id": "default_user",
  "source_episode_id": "ep_kyoto_2026",
  "source_trip_id": "trip_123",
  "slice_type": "accommodation_decision",
  "domains": ["hotel", "accommodation"],
  "entities": {
    "destination": "京都",
    "travelers": ["couple"],
    "season": "spring"
  },
  "keywords": ["住宿", "酒店", "民宿", "町屋", "住哪里"],
  "content": "上次京都选择町屋，原因是靠近地铁，适合情侣慢节奏体验。",
  "applicability": "仅供住宿偏好参考；当前同行人、预算或无障碍要求变化时不能直接套用。",
  "created_at": "2026-04-19T00:00:00"
}
```

第一版支持的 slice 类型：

| 类型 | 来源 | 适用问题 |
|---|---|---|
| `accommodation_decision` | selected hotel / accepted hotel event / final summary | 上次住哪里、为什么这样住 |
| `transport_decision` | selected transport / accepted transport event | 上次怎么去、交通偏好 |
| `pace_lesson` | lessons / rejected items / final summary | 上次累不累、按以前节奏来 |
| `rejected_option` | rejected items / memory events | 之前拒绝过什么 |
| `accepted_pattern` | selected skeleton / accepted items | 用户接受过什么规划模式 |
| `pitfall` | lessons | 上次踩了什么坑 |
| `budget_signal` | budget / lessons / final summary | 上次预算分配经验 |

第一版 slice 生成采用确定性规则，不额外调用 LLM：

- 从 `TripEpisode.destination`、`travelers`、`budget` 提取实体。
- 从 `accepted_items`、`rejected_items`、`lessons`、`selected_skeleton`、`final_plan_summary` 生成短内容。
- 使用固定 domain/keyword 映射表补齐 `domains` 和 `keywords`。
- 每个 episode 最多生成 8 条 slice，避免历史噪音膨胀。

### 5.5 `MemoryEvent`

`events.jsonl` 是行为证据日志，不直接召回。

```json
{
  "id": "event_001",
  "user_id": "default_user",
  "session_id": "s1",
  "event_type": "accept",
  "object_type": "hotel",
  "object_payload": {},
  "reason_text": "交通方便",
  "created_at": "2026-04-19T00:00:00"
}
```

事件在 Phase 7 归档时作为 episode 和 slice 生成素材。读取层不直接把原始 event 注入 prompt。

---

## 6. 读取机制

### 6.1 固定召回

固定召回只从 `profile.json` 和当前 `working_memory.json` 取少量高价值内容。

长期画像固定召回：

- `constraints`
- `rejections`
- `stable_preferences`

默认不固定召回：

- `preference_hypotheses`
- `episodes.jsonl`
- `episode_slices.jsonl`
- `events.jsonl`

当前 session 工作记忆召回：

- 只读当前 `session_id`。
- `trip_id` 不匹配时不召回。
- `status != active` 不召回。
- 如果 `expires.on_phase_exit = true` 且当前 phase 已变化，不召回。

### 6.2 Query-aware Symbolic Recall

新增一条按本轮用户消息触发的检索链路。

Layer 1：规则触发。

触发词：

```text
上次
之前
以前
我是不是说过
按我的习惯
我通常
还记得吗
有没有记录
```

不触发的当前旅行问题：

```text
这次预算多少？
我们几号出发？
当前选了哪个骨架？
这次有哪些约束？
```

Layer 2：规则 query 解析。

第一版不调用 LLM，只解析：

- domains：住宿/酒店/民宿 -> `hotel/accommodation`
- domains：航班/火车/交通 -> `flight/train/transport`
- domains：节奏/累/慢/松 -> `pace`
- entities：目的地名、上次/以前/之前等时间指向
- include_profile：长期偏好类问题为 true
- include_slices：上次/历史旅行类问题为 true

Layer 3：本地 symbolic retrieval。

检索来源：

- `profile.json`：查长期偏好、约束、否决。
- `episode_slices.jsonl`：查历史旅行切片。
- `working_memory.json`：查当前 session 短期信号。

匹配字段：

- `domain`
- `key`
- `value`
- `recall_hints.domains`
- `recall_hints.keywords`
- `recall_hints.aliases`
- `entities.destination`
- `keywords`
- `content`
- `applicability`

排序：

1. constraint / rejection 优先。
2. exact destination match 优先。
3. domain match 优先。
4. keyword match 数量多优先。
5. 最近创建或更新优先。
6. `preference_hypotheses` 低于 stable items。

### 6.3 Prompt 格式

系统 prompt 中不再有“本次旅行记忆”区块。

推荐结构：

```text
## 长期用户画像
- [flight] avoid_red_eye: true
  来源：长期约束
  适用边界：适用于所有旅行，除非用户明确临时允许

## 当前会话工作记忆
- 用户说先别考虑迪士尼，原因是不想太商业化
  适用边界：仅当前 session/trip

## 本轮请求命中的历史记忆
- 命中原因：用户询问“上次京都住宿”
  来源：episode slice ep_kyoto_2026
  内容：上次京都选择町屋，原因是靠近地铁，适合情侣慢节奏体验
  适用边界：当前同行人、预算或无障碍要求变化时不能直接套用
```

所有历史记忆必须带：

- 来源。
- 命中原因。
- 内容。
- 适用边界。

历史切片只是证据，不是当前旅行硬约束。`TravelPlanState` 明确字段始终优先。

---

## 7. 写入机制

### 7.1 Extraction 输出

新提取器输出分流结构，不再输出单一 `MemoryCandidate[]`。

```json
{
  "profile_updates": {
    "constraints": [],
    "rejections": [],
    "stable_preferences": [],
    "preference_hypotheses": []
  },
  "working_memory": [],
  "episode_evidence": [],
  "state_observations": [],
  "drop": []
}
```

落库规则：

| 输出 | 落库 |
|---|---|
| `profile_updates.constraints` | `profile.json.constraints` |
| `profile_updates.rejections` | `profile.json.rejections` |
| `profile_updates.stable_preferences` | `profile.json.stable_preferences` |
| `profile_updates.preference_hypotheses` | `profile.json.preference_hypotheses` |
| `working_memory` | `sessions/{session_id}/working_memory.json` |
| `episode_evidence` | 当前 session 暂存，Phase 7 生成 episode/slice 时消费 |
| `state_observations` | 不写 memory，用于诊断或 plan writer 校验 |
| `drop` | 不落库 |

### 7.2 提取边界

禁止写入 memory 的当前旅行状态：

- 本次目的地。
- 本次日期。
- 本次预算。
- 本次同行人。
- 本次 trip brief。
- 本次候选池。
- 本次骨架选择。
- 本次每日行程。
- 已由 plan writer 写入的 preferences / constraints。

这些信息必须由 `TravelPlanState` 承担。

允许写入 profile 的信息：

- 明确长期硬约束，例如“以后都不坐红眼航班”。
- 明确长期强否决，例如“我绝不住青旅”。
- 用户确认过或多次出现的稳定偏好。
- 单次观察形成的偏好假设，但只能进入 `preference_hypotheses`，并带 `context/applicability/stability=single_observation`。

允许写入 working memory 的信息：

- 当前 session 需要记住的临时否决。
- 当前阶段需要避免重复推荐的候选。
- 用户短期犹豫原因。

### 7.3 Policy 和合并

Profile item id 生成规则：

```text
{bucket}:{domain}:{key}:{normalized_value?}
```

其中：

- constraints / stable_preferences 按 `domain + key` upsert。
- rejections 按 `domain + key + normalized_value` upsert，避免多个排除项互相覆盖。
- preference_hypotheses 按 `domain + key + context hash` upsert。

合并规则：

1. 同 id 同 value：更新 `confidence=max(old,new)` 和 `updated_at`。
2. 同 id 标量冲突：旧项保留为 `obsolete`，新项进入 `pending_conflict`。
3. 列表类 value：并集，不静默删除旧值。
4. `preference_hypotheses` 不自动提升到 `stable_preferences`，必须满足重复证据或用户确认。
5. 用户 confirm/reject API 优先级最高。

PII 策略：

- payment / membership 直接 drop。
- 证件号、身份证、护照号、手机号、邮箱、银行卡号直接 drop。
- 健康/过敏只保存类型和规划影响，不保存诊断细节。
- 家庭成员只保存关系和数量，不保存姓名。

---

## 8. API 和前端

后端 API 重做为 v3 资源，不再返回混合 `items` 列表作为主接口。

```text
GET /api/memory/{user_id}/profile
GET /api/memory/{user_id}/episodes
GET /api/memory/{user_id}/episode-slices
GET /api/memory/{user_id}/sessions/{session_id}/working-memory
POST /api/memory/{user_id}/profile/confirm
POST /api/memory/{user_id}/profile/reject
DELETE /api/memory/{user_id}/profile/{bucket}/{item_id}
```

Memory Center 前端按四类展示：

```text
长期画像
偏好假设
历史旅行
当前会话工作记忆
```

现有 v2 API 可在迁移期保留为 deprecated，但新功能和新 UI 不依赖它。

SSE `memory_recall` 扩展：

```json
{
  "type": "memory_recall",
  "sources": {
    "profile_fixed": 2,
    "working_memory": 1,
    "query_profile": 1,
    "episode_slice": 1
  },
  "profile_ids": ["..."],
  "working_memory_ids": ["..."],
  "slice_ids": ["..."],
  "matched_reasons": ["用户询问上次京都住宿"]
}
```

---

## 9. 迁移策略

提供一次性脚本：

```text
scripts/migrate_memory_v2_to_v3.py
```

迁移规则：

| v2 来源 | v3 目标 |
|---|---|
| `type=constraint, scope=global` | `profile.constraints` |
| `type=rejection, scope=global` | `profile.rejections` |
| `type=preference, scope=global` | `profile.stable_preferences`，标记 `source_schema=v2` |
| `scope=trip` | 不进入 profile；写入 `legacy_ignored.jsonl` 供审计 |
| `memory_events.jsonl` | `memory/events.jsonl` |
| `trip_episodes.jsonl` | `memory/episodes.jsonl` |
| 历史 episode | 批量生成 `memory/episode_slices.jsonl` |

迁移后旧文件移动到：

```text
data/users/{user_id}/legacy_memory_v2/
```

迁移脚本必须具备：

- dry-run 模式。
- 迁移前备份。
- 重复运行幂等。
- 输出 ignored trip memory 数量。
- 输出生成的 profile item / episode / slice 数量。

---

## 10. 测试计划

模型与存储：

- `UserMemoryProfile` 序列化/反序列化。
- `SessionWorkingMemory` 序列化/反序列化。
- `TripEpisode` v3 路径读写。
- `EpisodeSlice` append/list/去重。
- `MemoryEvent` v3 路径追加。

迁移：

- v2 global preference -> v3 stable preference。
- v2 rejection -> v3 rejection。
- v2 trip memory -> ignored，不进入 prompt。
- legacy episode -> v3 episode + slices。
- dry-run 不写文件。
- 重复迁移不重复生成数据。

读取：

- 固定召回只读取 constraints/rejections/stable_preferences。
- preference_hypotheses 不进入固定画像。
- working memory 只在 session/trip 匹配时进入 prompt。
- 当前旅行问题不触发 query recall。
- “上次京都住哪里”触发 episode slice recall。
- “我是不是说过不坐红眼航班”触发 profile query recall。
- formatter 不注入完整 episode。
- prompt 不再出现“本次旅行记忆”区块。

写入：

- 提取器把当前旅行状态放入 `state_observations` 或 `drop`，不写 profile。
- 明确长期硬约束进入 `profile.constraints`。
- 单次观察进入 `preference_hypotheses`。
- 临时否决进入 working memory。
- payment/membership/PII 被 drop。

API/SSE：

- 新 profile API 返回分桶结构。
- episode slices API 返回切片。
- working memory API 按 session 返回。
- `memory_recall` SSE 包含 source counts 和 matched reasons。

---

## 11. 实施顺序

1. 新增 v3 模型与 store。
2. 编写 v2 -> v3 迁移脚本和测试。
3. 实现 episode slice 生成器。
4. 实现固定 profile / working memory 召回和 formatter。
5. 实现 query-aware symbolic recall。
6. 替换 `MemoryManager.generate_context()` 为 v3 context assembly。
7. 改造 extraction 输出 schema、policy 和落库分流。
8. 改造 API 和 SSE。
9. 更新 Memory Center 前端类型与展示。
10. 删除或废弃 v2 运行时读取路径。

每一步都必须保持后端测试可运行；迁移完成前不删除旧文件，迁移完成后运行时不再读取旧格式。

---

## 12. 风险与约束

主要风险：

- 改动面大，涉及 backend memory、main.py、context、API、前端 Memory Center 和测试。
- v2 旧数据若不迁移会被运行时视为无记忆。
- query-aware recall 第一版规则可能漏召回，但不能过度召回。
- working memory 生命周期需要严格测试，避免跨 trip 泄漏。
- episode slice 规则生成可能太粗，需要通过测试样例约束内容长度和适用边界。

控制措施：

- 先实现迁移脚本和 v3 store 测试，再接入运行时。
- query trigger 第一版保守，只处理显式历史指向。
- prompt formatter 强制输出来源、命中原因和适用边界。
- 保留 v2 文件备份，支持手动回滚数据。
- 不引入 LLM 生成 slice，先用确定性规则降低不稳定性。

---

## 13. 成功标准

功能成功：

- 当前旅行事实只从 `TravelPlanState` 进入 prompt。
- prompt 中不再出现 broad trip memory 区块。
- 用户显式询问历史旅行时，系统能召回相关 episode slice。
- 用户询问长期偏好时，系统能从 profile 中命中相关约束/偏好。
- `TripEpisode` 全文不会进入 prompt。
- 临时 session 信号不会进入长期 profile。

工程成功：

- v3 store 和 migration 测试覆盖核心路径。
- 现有 memory policy / formatter / manager 测试按 v3 更新后通过。
- `memory_recall` SSE 能区分 profile、working memory、episode slice 来源。
- `PROJECT_OVERVIEW.md` 在实现完成时更新为当前架构。

最终原则：

> 存储结构必须服务读取语义：当前事实读 state，长期画像读 profile，历史经验读 slice，临时推理读 working memory，行为证据读 events。
