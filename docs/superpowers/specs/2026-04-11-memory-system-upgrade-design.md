# Memory System Upgrade 设计文档

> **状态**：草案，待评审
> **日期**：2026-04-11
> **范围**：将当前轻量用户画像记忆升级为可治理、可检索、可评估的旅行垂类记忆系统

---

## 1. 背景

当前 Travel Agent Pro 的记忆系统已经具备 MVP 能力：

- `UserMemory` 以 `data/users/{user_id}/memory.json` 持久化用户偏好、历史旅行、排除项。
- `Memory Extraction` 在 Phase 1 -> Phase 3 转换后异步调用 LLM，从用户消息中抽取长期偏好。
- `MemoryManager.generate_summary()` 将记忆拼成 `## 用户画像`，每轮注入 system prompt。
- 当前行程偏好通过 `update_plan_state(field="preferences")` 写入 `TravelPlanState`，在后续阶段注入当前规划状态。

这套机制适合演示"跨会话记住用户偏好"，但还不是完整的 Agent Memory 系统。它缺少结构化生命周期、作用域、来源、置信度、冲突处理、用户确认、按需检索、历史 episode 和行为反馈学习。随着会话历史、工具结果和用户画像增长，"全量摘要注入"会带来上下文污染、错误记忆放大和难以调试的问题。

本设计将当前系统升级为旅行垂类 memory layer。第一阶段保持可落地，不引入向量库、知识图谱或外部 connector，优先解决当前项目最关键的治理、注入和评估问题。

---

## 2. 外部实践映射

### Agent Memory 公认分类

当前主流 Agent Memory 实践通常区分三类：

| 类型 | 含义 | 旅行 Agent 示例 |
|------|------|----------------|
| Semantic Memory | 用户事实、偏好、稳定画像 | 不吃辣、喜欢轻松节奏、偏好海边酒店 |
| Episodic Memory | 过去交互、决策、结果和反馈 | 上次东京亲子游选择了轻松路线，拒绝迪士尼排队 |
| Procedural Memory | Agent 做事规则、沟通和规划策略 | 这个用户喜欢先看表格对比，再看详细解释 |

当前项目只实现了 semantic memory 的一小部分，并且主要是自由 key-value。`implicit_preferences` 和 `trip_history` 虽然在模型中存在，但没有完整写入流程；episodic 和 procedural memory 还没有进入系统。

### 通用 Agent 实践

Manus 这类通用 Agent 公开资料没有详细披露内部 memory 算法，但它的产品形态体现了几个重要方向：

- **持久主线程**：Agent 不只是单次 session，而有可持续延展的主任务线程。
- **Project Context**：项目级 instruction、文件和知识库自动进入新任务上下文。
- **独立子任务上下文**：复杂任务拆成多个 fresh context 的子任务，主 agent 汇总结构化结果。
- **外部数据边界**：connector 数据、项目知识、用户个人记忆需要分离治理。

对本项目的启发是：旅行系统应区分用户全局记忆、本次旅行项目记忆、会话短期上下文和外部授权数据，不能都塞进一个 `memory.json` 摘要。

### 旅行 Agent 实践

旅行垂类 Agent 论文和行业实践更强调：

- **短期记忆 + 长期记忆**：本次旅行约束、候选、决策属于 trip memory；长期偏好和历史画像属于 user memory。
- **行为反馈学习**：like/pass、接受/拒绝、编辑/重排会比自然语言更稳定地暴露隐式偏好。
- **记忆参与排序和规划**：偏好不应只进 prompt，还应影响目的地、酒店、餐厅、交通和每日节奏的评分。
- **实时事实不能来自记忆**：价格、库存、营业时间、签证政策必须来自工具，记忆只提供用户倾向和历史决策。

---

## 3. 设计目标

### 目标

1. **结构化治理**：所有长期记忆都有类型、作用域、来源、置信度、时间戳和状态。
2. **旅行垂类 schema**：将自由偏好 key 收敛到稳定的 travel profile 字段，减少同义字段散落。
3. **分层记忆**：区分 global memory、trip memory、episode memory 和未来 procedural memory。
4. **安全提取**：每轮后台生成 memory candidates，按风险级别自动保存或进入待确认。
5. **精准注入**：从全量用户画像改为核心常驻 + 本次旅行 + 阶段相关记忆。
6. **行为学习**：记录用户对候选、骨架、酒店、餐厅、日程的接受、拒绝、编辑和重排行为。
7. **可评估**：建立 memory extraction、merge、injection 和 planning compliance 的测试闭环。

### 非目标

第一阶段不实现：

- 向量数据库或 embedding 检索。
- temporal knowledge graph。
- 外部 connector，如日历、邮箱、订单、会员体系。
- 完整前端 memory 管理页。
- 自动改写 procedural prompt。
- 多 agent workspace 和子任务 memory 隔离。

这些能力保留为 Future Work，避免第一阶段范围过大。

---

## 4. 目标架构

```
User Message / Tool Result / Plan Event
    │
    ├─ TravelPlanState
    │     └─ 当前 session 的确定性状态
    │
    ├─ MemoryCandidateExtractor
    │     └─ 后台从对话和事件中生成候选记忆
    │
    ├─ MemoryPolicy
    │     ├─ 判断 scope: global / trip / destination / temporary
    │     ├─ 判断 risk: low / medium / high
    │     └─ 判断 action: auto_save / pending / ignore
    │
    ├─ MemoryStore
    │     ├─ MemoryItem
    │     ├─ MemoryEvent
    │     └─ TripEpisode
    │
    ├─ MemoryRetriever
    │     └─ 根据 phase、tool、destination、domain 选择相关记忆
    │
    └─ ContextManager
          ├─ 核心用户画像
          ├─ 本次旅行记忆
          └─ 当前阶段相关历史
```

### 边界原则

- `TravelPlanState` 仍然是本次规划的权威状态。
- `MemoryStore` 只保存跨轮、跨 session 或需要后续检索的记忆。
- 记忆不能覆盖工具事实。事实性信息仍必须来自工具。
- 记忆注入是 context assembly 的一部分，不应该散落在业务工具内部。
- 记忆可影响工具参数和排序，但必须通过明确接口传入。

---

## 5. 数据模型

### 5.1 `MemoryItem`

替代当前自由 dict 的核心结构。

```python
@dataclass
class MemoryItem:
    id: str
    user_id: str
    type: str          # preference | constraint | rejection | profile | procedure
    domain: str        # food | hotel | flight | train | pace | budget | family | accessibility | planning_style | destination | documents | general
    key: str
    value: Any
    scope: str         # global | trip | destination | temporary
    polarity: str      # like | dislike | must | avoid | neutral
    confidence: float
    status: str        # active | pending | pending_conflict | obsolete | rejected
    source: MemorySource
    created_at: str
    updated_at: str
    expires_at: str | None = None
    destination: str | None = None
    session_id: str | None = None
    trip_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
```

`attributes` 用作 schema 扩展位：当 LLM 抽取出"合理但不在固定 domain/key 表内"的信息时，原始字段写入 `attributes`，由后续 policy 或人工决定是否升级为正式 schema 字段。禁止 LLM 直接为 `domain` 或 `key` 造词。

`id` 生成规则按类型区分：

- preference / constraint / profile / procedure：`sha1(f"{user_id}:{type}:{domain}:{key}:{scope}:{trip_id or ''}")[:16]`。同一用户/类型/域/键/作用域天然 upsert。
- rejection：`sha1(f"{user_id}:{type}:{domain}:{key}:{normalized_value}:{scope}:{trip_id or ''}")[:16]`。同一域下的不同排除项必须是不同 item，避免多个 `avoid` 值互相覆盖。

多值字段（如 `cuisine_likes`）的合并语义见 §8 Merge 规则。

`expires_at` 语义：仅 trip / temporary scope 在以下时机自动设置——trip scope item 在 episode 归档时由 store 写入 `expires_at = now`；temporary scope item 在创建时写入 `now + 24h`。读取路径采用懒清理：`list_items` 时如果 `expires_at < now`，store 把 `status` 改为 `obsolete` 并在下一次 `upsert` 持久化，不引入后台扫描任务。global scope item 永远不设置 `expires_at`。

### 5.2 `MemorySource`

```python
@dataclass
class MemorySource:
    kind: str          # message | tool_call | plan_event | user_confirmation | migration
    session_id: str
    message_id: str | None = None
    tool_call_id: str | None = None
    quote: str | None = None
```

`quote` 只保存短引用或摘要，避免把完整对话复制进 memory。

### 5.3 Travel Profile Domains

第一阶段固定以下 domain/key，降低自由字段漂移。

| Domain | Key 示例 | 用途 |
|--------|----------|------|
| `pace` | `preferred_pace`, `daily_activity_count`, `start_time`, `end_time` | 控制每日行程密度 |
| `food` | `dietary_restrictions`, `cuisine_likes`, `cuisine_avoids` | 餐厅搜索和排除 |
| `hotel` | `hotel_style`, `location_preference`, `star_level`, `room_preference` | 住宿搜索和排序 |
| `flight` | `seat_preference`, `avoid_red_eye`, `airline_preference` | 航班筛选 |
| `train` | `seat_preference`, `transfer_tolerance` | 火车筛选 |
| `budget` | `budget_style`, `splurge_categories`, `save_categories` | 预算分配 |
| `family` | `usual_travelers`, `child_friendly`, `elderly_friendly` | 亲子/老人规划 |
| `accessibility` | `walking_tolerance`, `mobility_needs` | 路线和节奏约束 |
| `planning_style` | `detail_level`, `comparison_style`, `decision_style` | 回复和方案呈现 |
| `destination` | `liked_destinations`, `avoided_destinations` | 目的地推荐 |
| `documents` | `passport_validity`, `visa_reminder` | 只保存证件有效期提示，不保存证件号 |

未列出的 domain 一律落入 `general`，原始信息写入 `MemoryItem.attributes`，并由 `MemoryPolicy` 标记 `needs_review=true`，留待后续人工或规则升级到正式 domain。新增正式 domain 必须走 schema 升级流程，不允许 LLM 在运行时造新 domain，以防止字段漂移回到旧自由 dict 状态。

### 5.4 `MemoryEvent`

用于记录行为反馈，不立即等同于长期偏好。

```python
@dataclass
class MemoryEvent:
    id: str
    user_id: str
    session_id: str
    event_type: str    # accept | reject | like | dislike | edit | reorder | skip
    object_type: str   # destination | attraction | restaurant | hotel | flight | train | transport | skeleton | daily_plan
    object_payload: dict
    reason_text: str | None
    created_at: str
```

### 5.5 `TripEpisode`

在行程归档或用户明确结束规划时生成。

```python
@dataclass
class TripEpisode:
    id: str
    user_id: str
    session_id: str
    destination: str | None
    dates: str | None
    travelers: dict | None
    budget: dict | None
    selected_skeleton: dict | None
    final_plan_summary: str
    accepted_items: list[dict]
    rejected_items: list[dict]
    lessons: list[str]
    satisfaction: int | None
    created_at: str
```

---

## 6. 存储策略

第一阶段优先复用现有数据目录，避免引入新基础设施。

### 推荐落地方案

```
data/users/{user_id}/
    memory.json              # 新结构，保存 MemoryItem 列表和版本号
    memory_events.jsonl      # 行为事件追加日志
    trip_episodes.jsonl      # 历史行程 episode
```

`memory.json` 增加 `schema_version`：

```json
{
  "schema_version": 2,
  "user_id": "default_user",
  "items": [],
  "legacy": {
    "explicit_preferences": {},
    "implicit_preferences": {},
    "trip_history": [],
    "rejections": []
  }
}
```

### 为什么第一阶段仍用文件

- 当前 `MemoryManager` 已是文件持久化，改动面小。
- session history 已经使用 SQLite，但 memory 还不需要复杂查询。
- JSON/JSONL 方便人工检查 memory 行为。
- 后续迁入 SQLite 或向量库时，可以从 `MemoryStore` 接口下切换实现。

### Store 接口

新增 `MemoryStore`，让上层不依赖文件格式。

```python
class MemoryStore(Protocol):
    async def list_items(self, user_id: str, *, status: str | None = None) -> list[MemoryItem]:
        raise NotImplementedError

    async def upsert_item(self, item: MemoryItem) -> None:
        raise NotImplementedError

    async def update_status(self, item_id: str, status: str) -> None:
        raise NotImplementedError

    async def append_event(self, event: MemoryEvent) -> None:
        raise NotImplementedError

    async def append_episode(self, episode: TripEpisode) -> None:
        raise NotImplementedError

    async def list_episodes(
        self, user_id: str, *, destination: str | None = None
    ) -> list[TripEpisode]:
        raise NotImplementedError
```

`MemoryManager` 可以继续作为 facade，但内部委托给 `MemoryStore`、`MemoryRetriever` 和 `MemoryFormatter`。

---

## 7. 提取流程

### 当前问题

现有 `_schedule_memory_extraction()` 只在 Phase 1 -> Phase 3 触发，漏掉后续阶段大量长期偏好，例如：

- "以后我都不坐红眼航班。"
- "我每次旅行都希望至少留半天不安排。"
- "以后带父母的话不要安排太多换乘。"

### 新流程

每轮 chat 完成后触发后台候选提取：

```
chat round completed
    │
    ├─ collect new user messages since last extraction
    ├─ collect plan diff and memory events
    ├─ MemoryCandidateExtractor.extract()
    ├─ MemoryPolicy.classify()
    │     ├─ auto_save low-risk active items
    │     ├─ pending medium/high-risk items
    │     └─ ignore one-off trip facts
    └─ MemoryStore.upsert_item()
```

### 提取输入

- 自上次提取 watermark 以来新增的用户消息。`MemoryStore` 为每个 session 维护 `last_extracted_message_id`，避免同一句话在多轮里被反复抽取。当前实现 (`backend/main.py:493-497`) 一次性把所有历史 user message 喂给 LLM，必须在新方案中修掉。
- 本轮 assistant 最终回复摘要。
- 本轮 `TravelPlanState` diff。
- 已有 active/pending memory 摘要。
- 本轮用户行为事件。

### 提取输出

LLM 必须输出候选列表，而不是直接输出最终 memory：

```json
{
  "candidates": [
    {
      "type": "preference",
      "domain": "flight",
      "key": "avoid_red_eye",
      "value": true,
      "scope": "global",
      "polarity": "avoid",
      "confidence": 0.95,
      "risk": "medium",
      "evidence": "以后我都不坐红眼航班",
      "reason": "用户明确表达长期偏好"
    }
  ]
}
```

### 提取规则

- 明确长期表达才允许 `scope=global`。
- 本次目的地、日期、预算默认不是 global memory。
- "这次"、"这趟"、"本次" 默认进入 trip scope。
- 健康、过敏、证件、家庭成员、永久排除、支付信息等高影响记忆必须进入 pending。
- 对已有 memory 的同义更新必须带 `conflict_with`，由 merge policy 处理。

### 调度模型

把触发频率从"Phase 1→3 一次"改成"每轮一次"会显著放大 LLM 调用成本和并发问题，必须显式定义调度规则：

- **触发条件**：本轮存在新的 user message（即 `last_extracted_message_id` watermark 之后有新增）才触发；assistant 纯工具调用轮、重传/恢复轮跳过。
- **fire-and-forget**：extraction 在独立 `asyncio.Task` 中执行，不阻塞主 SSE 流和 `state_mgr.save`；继续沿用现有 `memory_extraction_tasks` 的 task 集合管理。
- **per-session 串行**：同一 session 上一轮 extraction 还未完成时，不开第二个并发任务，而是把新增 watermark 合并进当前任务的下一轮（debounce）；两个 session 之间互不影响。
- **超时与失败**：单次 extraction 设硬超时（默认 20s），LLM 调用、JSON 解析、policy/merger 任意环节失败都不回滚已写入的 memory，只打 telemetry，不向用户冒泡错误。
- **成本观测**：在 §16 telemetry 之外补 `memory.extraction.tokens_used` 和 `memory.extraction.duration_ms`，以便后续评估是否需要节流或换更小的模型。
- **配置开关**：`memory.extraction.trigger` 支持 `each_turn` / `phase_transition` / `disabled`，方便回滚到旧行为。

---

## 8. 合并与确认策略

### Risk 分级

| Risk | 示例 | 默认动作 | confidence 门槛 |
|------|------|----------|-----------------|
| low | 喜欢美食、偏好轻松、喜欢海景 | auto_save | ≥ 0.7 |
| medium | 不坐红眼航班、酒店星级偏好、常用出发机场 | pending（默认）/ auto_save（配置开启） | ≥ 0.8 |
| high | 过敏、健康、证件、家庭成员、永久排除、支付/会员信息 | pending（强制） | ≥ 0.85 |

低于 confidence 门槛的 candidate 不直接丢弃，落入 pending，等待下一次同类 candidate 再次出现以叠加 confidence；连续 3 轮 pending 仍未达标则自动 `ignore` 并打 telemetry。

**low-risk auto_save 仍需被动告知**：第一次被注入到核心画像时，通过 §8 "对话层呈现机制" 的 `memory_pending` 事件做一次只读告知（前端展示但不要求用户操作），让用户有否决机会。这避免 LLM 误推的"喜欢美食"类记忆静默污染长期画像。

### Merge 规则

1. `id` 相同（即 `(user, domain, key, scope, trip)` 一致）且 value 等价：更新 `updated_at`，confidence 取 `max(old, new)`，不做覆盖。
2. `id` 相同但 value 是**标量**且发生冲突（如 `preferred_pace` 由"轻松"变"紧凑"）：旧项标记 `obsolete`，新项进入 `pending_conflict`，等待用户确认，绝不静默替换 confirmed active item。
3. `id` 相同但 value 是**列表/集合**（如 `cuisine_likes`、`liked_destinations`）：执行 set union，不覆盖；confidence 取 `max(old, new)`。从列表中删除某项必须通过显式 reject API 或新的"否定" candidate（同 key + `polarity=avoid`），而不是让新 candidate 静默替换。
4. rejection 按 `(domain, key, value, scope)` 四元组去重，不再只按 `item` 字符串去重。
5. temporary / trip memory 永远不覆盖 global memory；冲突时打 telemetry 但保留两条独立 item。retriever 在装配当前旅行上下文时按 scope 优先级（trip > temporary > global）选择，在装配核心用户画像时只读取 global。
6. 用户通过 confirm/reject API 明确纠正时，以最新用户确认优先，旧自动写入项被强制 `obsolete`。

### 高风险 PII 处理

第一阶段不引入加密存储（留待 §19 Future Work），但必须在写入路径就做 redaction，避免敏感原文落盘到 `memory.json`：

- **过敏 / 健康**：只存类型标记和影响域，不存诊断细节。
  - 允许：`{domain: "food", key: "allergies", value: ["peanut"]}`
  - 拒绝：`{domain: "food", key: "allergies", value: "花生过敏导致急救史，曾住院 3 天"}`
- **证件**：只存"是否持有 + 国别 + 有效期年月"，禁止存证件号。
  - 允许：`{domain: "documents", key: "passport_validity", value: {"country": "CN", "expires": "2030-05"}}`
  - 拒绝：任何包含 9-18 位连续数字、`护照号 / 身份证 / passport number` 关键词的 value
- **支付 / 会员信息**：第一阶段直接拒绝抽取。candidate extractor 的 prompt 加 redaction 规则；解析阶段过滤掉任何 `domain in {payment, membership}` 的 candidate 并打 `memory.extraction.pii_dropped` telemetry。
- **家庭成员**：只存关系和数量（如 `{travelers: [{relation: "child", age: 6}]}`），不存姓名。
- **不可逆字段**：如果 LLM 输出包含上述被禁字段，extractor 解析阶段必须**直接丢弃**（不写 pending），保证敏感原文从未触达 `memory.json`。被丢弃的 candidate 只在 telemetry 留 hash，便于审计但不可还原。

### 用户确认

第一阶段后端先支持 pending 状态和 API，不强制完成完整前端管理页。

新增最小 API：

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/memory/{user_id}` | 查看 active/pending/pending_conflict memory |
| POST | `/api/memory/{user_id}/confirm` | 确认 pending item |
| POST | `/api/memory/{user_id}/reject` | 拒绝 pending item |
| DELETE | `/api/memory/{user_id}/{item_id}` | 标记 obsolete |

#### 对话层呈现机制

pending memory **不通过修改 system prompt 让 LLM 自己生成提示语**，否则文本和数据脱钩，且 LLM 容易幻觉出不存在的 pending item。改为通过 SSE 协议独立通道：

- 后台 extraction 完成、policy 产生 pending item 后，向当前 SSE 流追加事件：

  ```json
  {
    "type": "memory_pending",
    "items": [
      {
        "id": "mem_xxx",
        "type": "preference",
        "domain": "flight",
        "key": "avoid_red_eye",
        "value": true,
        "scope": "global",
        "polarity": "avoid",
        "risk": "medium",
        "evidence": "以后我都不坐红眼航班",
        "summary": "你以后不坐红眼航班"
      }
    ]
  }
  ```

- 前端在对话气泡末尾或独立卡片渲染确认/拒绝按钮，分别调用 `POST /api/memory/{user_id}/confirm` 和 `/reject`。
- 如果 extraction 完成时本轮 SSE 已关闭（fire-and-forget 异步），pending item 直接落库；下一轮 chat 响应开始时由后端补发同样的 `memory_pending` 事件，前端按 `id` 去重渲染。
- LLM prompt 内**不**注入 pending item 文本，避免 LLM 把它当成事实复述给用户。
- `summary` 字段由 policy 层根据 candidate 模板生成（"你 {polarity_text} {value_text}"），不再依赖 LLM 二次产生展示文案，保证文本与底层数据一致。

---

## 9. 注入策略

### 当前问题

当前 `generate_summary()` 把用户记忆拼接为一个 `## 用户画像`，每轮全量注入。随着记忆增长，会导致：

- 不相关偏好干扰当前阶段。
- 旧记忆和新需求冲突时难以判断。
- 上下文成本持续增加。
- 错误记忆每轮被放大。

### 新策略

`MemoryRetriever` 按 phase 和任务选择记忆，`MemoryFormatter` 输出三段：

```text
## 核心用户画像
- [pace] 偏好轻松节奏（global, confidence 0.90）
- [food] 不吃辣（global, confirmed）

## 本次旅行记忆
- 这次东京行希望住新宿或涩谷附近
- 这次不安排迪士尼

## 当前阶段相关历史
- 上次亲子旅行中，用户拒绝了排队时间长的热门景点
```

### 常驻核心记忆

只放最高优先级的 active global memory，默认最多 10 条。排序权重：

1. 用户确认过。
2. 高 confidence。
3. 与旅行安全、硬约束、排除项相关。
4. 与当前 phase/domain 相关。
5. 最近更新。

### 阶段相关检索

| Phase | 注入重点 |
|-------|----------|
| Phase 1 | 目的地偏好、旅行风格、预算风格、排除目的地 |
| Phase 3 brief/candidate | 兴趣、节奏、同行人、避免项、历史 episode |
| Phase 3 skeleton/lock | 酒店、交通、节奏、预算、硬约束 |
| Phase 5 | 每日节奏、餐饮、步行耐受、必避项、天气敏感 |
| Phase 7 | 证件、打包、健康、出发机场、交通偏好 |

### 与工具的关系

记忆不能只作为 prompt 文本存在。后续工具应逐步接收结构化 memory：

- `search_destinations` 使用 `destination`, `pace`, `budget`, `family` memory 扩展 query 和排序。
- `search_accommodations` 使用 `hotel` memory 作为过滤和排序特征。
- `search_flights` 使用 `flight` memory 排除红眼、偏好航司或舱位。
- `assemble_day_plan` 使用 `pace`、`accessibility`、`food` memory 控制活动密度和路线。
- `SoftJudge` 增加 memory compliance 检查。

---

## 10. Trip Memory 与 Episode

### Trip Id 生命周期

`trip_id` 标识"一次具体的旅行规划"，与 `session_id` 不强制 1:1，但需要明确生命周期，否则 trip scope memory 会与 plan 失联或在 backtrack 时残留：

- **分配时机**：Phase 1 完成（`destination` 首次写入 `TravelPlanState`）时由 backend 生成 `trip_id`（建议 `trip_<ulid>`），写入 `TravelPlanState` 元数据，并随 archive snapshot 一起持久化，保证回放/恢复时关联仍然有效。
- **session 与 trip 的映射**：默认 1 session = 1 trip。同一 user 同时打开多个 session 时，每个 session 各自持有独立 `trip_id`，trip scope memory 严格按 `trip_id` 隔离，不按 `user_id` 全量混合。
- **Backtrack 到 Phase 1 的处理**：由 `phase/backtrack.py` 统一裁决：
  - 用户明确换目的地或发出"重新开始 / 换个需求 / 不去这里了"信号 → 旧 `trip_id` 下所有 trip scope memory 整体标记 `obsolete`，新建 `trip_id`；
  - 仅微调原目的地（改日期、改预算、改人数）→ 复用原 `trip_id`，trip memory 保留。
- **多 trip 同 user**：retriever 在装配本次旅行记忆时严格按 `(user_id, trip_id)` 过滤，避免把另一条规划线的"这次想住新宿"漏到当前会话。
- **与 Episode 的关系**：`TripEpisode.id` 不等于 `trip_id`。一个 `trip_id` 在归档时生成一条 `TripEpisode`，episode 内回写 `trip_id`，用于反查相关 trip memory 和 events。

### Trip Scope Memory

本次旅行内有效但不应长期保存的信息进入 trip scope：

- 这次去东京。
- 这次预算 3 万。
- 这次想住新宿。
- 这次不想去迪士尼。

这些信息仍以 `TravelPlanState` 为权威。`trip memory` 的价值是：

- 为 context compression 提供更稳定的短摘要。
- 为 backtrack 后保留用户已确认的局部决策。
- 为最终 episode 生成提供输入。

### Episode 生成

在以下条件之一触发：

- Phase 7 完成并归档。
- 用户明确说"这个方案就这样"。
- 会话被标记 archived。

Episode 从以下来源生成：

- `TravelPlanState` 最终状态。
- `MemoryEvent` 行为反馈。
- 用户最终反馈。
- 被拒绝候选和原因。

Episode 不直接注入每轮上下文，只在相似目的地、相似同行人或用户明确提到"像上次一样"时检索。

---

## 11. 模块设计

### 11.0 与 `state/intake.py` 的边界

项目已有 `state/intake.py` 用于从用户自然语言提取**事实**（destination、dates、travelers、budget）写入 `TravelPlanState`。新引入的 `MemoryCandidateExtractor` 在输入和方法上与它部分重叠，必须明确边界，避免双写或互相覆盖：

- **职责切分**：
  - `intake` → 提取**确定性事实**，目标是 `TravelPlanState`。
  - `MemoryCandidateExtractor` → 提取**偏好 / 约束 / 排除 / 历史**，目标是 `MemoryStore`。
  - 两者写入目标完全不重叠，不会互相覆盖。
- **输入复用**：MemoryCandidateExtractor 接收 intake 已经解析出的结构化结果作为额外上下文。例如 intake 已识别"3 万预算"是本次 trip 级事实，extractor 就**不再**尝试把"预算 3 万"抽成 global memory，降低分类错误率，也减少 LLM token 消耗。
- **执行顺序**：每轮 chat 完成后，先由 main loop 完成 intake → `update_plan_state` 写入路径，再调度后台 MemoryCandidateExtractor。这样 extractor 看到的 `TravelPlanState` diff 永远是最新的。
- **测试隔离**：intake 单元测试只断言 plan 字段，不断言 memory；extractor 单元测试反之。两者 fixture 各自独立，避免跨模块耦合。

### 11.1 `backend/memory/models.py`

扩展模型：

- `MemoryItem`
- `MemorySource`
- `MemoryEvent`
- `TripEpisode`
- `MemoryCandidate`

保留旧 `UserMemory` 的兼容读取能力，避免一次性破坏现有文件。

### 11.2 `backend/memory/store.py`

新增文件存储实现：

- 读取 `memory.json`。
- 写入 schema v2。
- 追加 JSONL event 和 episode。
- 提供 atomic write，避免后台任务并发写坏文件。

#### 并发模型

文件级 JSON 没有事务，"主线程读 + 后台 extraction 写 + API confirm 写"三方会丢更新，必须显式管理：

- **per-user 串行队列**：`MemoryStore` 为每个 `user_id` 维护一个 `asyncio.Lock`。所有 `upsert_item` / `update_status` / `append_event` / `append_episode` 调用先获锁再执行；`list_items` / `list_episodes` 等只读操作内部 snapshot 后立即释放，避免迭代过程中状态被并发修改。
- **写入顺序**：进入 lock → 读最新 `memory.json` 到内存 → 应用变更 → 写 tmp 文件 → `os.replace` 原子替换 → 释放 lock。JSONL 文件在同一 lock 内 append-only 写入，保证事件顺序与 item 状态一致。
- **后台 extraction 与主回复路径解耦**：extraction task 在独立协程中获锁，不阻塞 SSE 流；如果获锁等待超过 `memory.extraction.lock_timeout_seconds`（默认 30s），放弃本轮提取并打 telemetry，绝不让锁等待回压到主对话路径。
- **多进程**：第一阶段假设单进程部署。若未来要多进程部署，应在 `MemoryStore` 接口下切换到 SQLite 或加 file lock 实现，外层调用方无感。
- **并发测试**：单元测试必须覆盖"并发 upsert + confirm + append_event"场景，使用 `asyncio.gather` 触发竞争，断言最终文件结构自洽、无 item 丢失、events 顺序与提交顺序一致。

### 11.3 `backend/memory/extraction.py`

从"直接返回 preferences/rejections"升级为：

- `build_candidate_extraction_prompt()`
- `parse_candidate_extraction_response()`
- `MemoryCandidateExtractor`

旧的 `build_extraction_prompt()` 可保留一段时间用于兼容测试。

### 11.4 `backend/memory/policy.py`

新增：

- `MemoryPolicy.classify(candidate, existing_items)`
- `MemoryMerger.merge(candidate, existing_items)`
- `MemoryRiskClassifier`

该模块不调用 LLM，只做确定性规则。

### 11.5 `backend/memory/retriever.py`

新增：

- `retrieve_core_profile(user_id, plan, limit=10)`
- `retrieve_trip_memory(user_id, session_id, plan)`
- `retrieve_phase_relevant(user_id, plan, phase, available_tools, limit=8)`

第一阶段使用规则、domain 和字符串匹配，不使用 embedding。

### 11.6 `backend/memory/formatter.py`

新增：

- `format_memory_context(retrieved: RetrievedMemory) -> str`
- 对 pending、obsolete、low-confidence 项默认不注入。
- 输出必须短、可读、带 domain 和 scope。

### 11.7 `backend/main.py`

改动点：

- 每轮 chat 完成后调度 `MemoryCandidateExtractor`，遵循 §7 "调度模型" 的串行 / debounce / 超时 / fire-and-forget 规则。
- 不再只依赖 Phase 1 → Phase 3 触发。
- 在构建 system message 前调用 `MemoryRetriever + MemoryFormatter`，把结果作为 `memory_context` 传入 `ContextManager.build_system_message()`。
- 增加 memory API（§14）和 SSE `memory_pending` 事件流（§8 对话层呈现机制）。
- **强制落地至少 4 个 MemoryEvent 触发点**，否则 §11.4 的 events 子系统在第一阶段就是空的。事件落盘走 `MemoryStore` 同一个 per-user 锁，不阻塞 SSE 主流程：
  1. `update_plan_state(field="selected_skeleton_id", value="<skeleton id>")` 成功 → `append_event(event_type="accept", object_type="skeleton", object_payload=selected_skeleton_summary)`
  2. `update_plan_state(field="selected_transport", value={"mode": "train"})` 成功 → `append_event(event_type="accept", object_type="transport", object_payload=selected_transport_summary)`
  3. `update_plan_state(field="accommodation", value={"area": "新宿"})` 成功 → `append_event(event_type="accept", object_type="hotel", object_payload=accommodation_summary)`
  4. backtrack 触发（无论是 LLM 主动调用还是 `_detect_backtrack` fallback） → `append_event(event_type="reject", object_type="phase_output", object_payload={"from_phase": from_phase, "to_phase": target_phase, "reason": reason})`
- 上述事件作为 Phase D 的最小可验证集合。后续 Phase E 再扩展到 like/dislike/edit/reorder 等更细粒度的前端行为事件。

### 11.8 `backend/context/manager.py`

改动点：

- `build_system_message()` 参数从 `user_summary: str` 改为更明确的 `memory_context: str`。
- `## 用户画像` 可改名为 `## 相关用户记忆`。
- 明确提示：记忆只代表用户偏好和历史，不代表实时事实。

---

## 12. 配置

新增配置段：

```yaml
memory:
  enabled: true
  extraction:
    enabled: true
    model: "astron-code-latest"
    trigger: "each_turn"
    max_user_messages: 8
  policy:
    auto_save_low_risk: true
    auto_save_medium_risk: false
    require_confirmation_for_high_risk: true
  retrieval:
    core_limit: 10
    phase_limit: 8
    include_pending: false
  storage:
    backend: "json"
```

兼容现有 `memory_extraction` 配置：加载时如果新 `memory.extraction` 不存在，则从旧配置迁移。

---

## 13. 迁移策略

### 输入

旧结构：

```python
UserMemory(
    explicit_preferences={"节奏": "轻松"},
    implicit_preferences={"住宿": "偏好海边"},
    trip_history=[{"destination": "东京", "dates": "2026-05"}],
    rejections=[{"item": "红眼航班", "reason": "休息不好", "permanent": True}],
)
```

### 迁移规则

- `explicit_preferences` -> `MemoryItem(type="preference", scope="global", status="active", source.kind="migration")`
- `implicit_preferences` -> `MemoryItem(type="preference", scope="global", confidence=0.6, status="active")`
- `rejections` -> `MemoryItem(type="rejection", polarity="avoid", status="active" if permanent else "pending")`
- `trip_history` -> `TripEpisode`，如果信息不足则保留在 `legacy.trip_history`

### 兼容原则

- 读取时自动识别 schema v1/v2。
- 第一次保存时写成 schema v2，并保留 `legacy` 备份。
- 老测试可逐步迁移，不要求同一个 PR 删除所有兼容层。

---

## 14. API 设计

### Memory 管理

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/api/memory/{user_id}` | 列出 active/pending/pending_conflict/obsolete memory |
| POST | `/api/memory/{user_id}/confirm` | 确认 pending memory |
| POST | `/api/memory/{user_id}/reject` | 拒绝 pending memory |
| DELETE | `/api/memory/{user_id}/{item_id}` | 标记 memory obsolete |

### Memory Events

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/api/memory/{user_id}/events` | 记录 accept/reject/edit/reorder 等行为 |
| GET | `/api/memory/{user_id}/episodes` | 查看历史 trip episodes |

第一阶段前端可以不做完整管理页，但 API 应完整测试。

---

## 15. 错误处理

- 提取失败不影响主 chat；记录 warning 日志和 telemetry。
- JSON 解析失败返回空 candidates，并记录 `memory.extraction.parse_failed`。
- Store 写入失败不能破坏原文件；使用临时文件 + rename。
- 合并冲突不自动覆盖高置信 active memory，改为 pending。
- pending memory 不注入上下文，除非配置明确允许。
- migration 失败时回退旧 `UserMemory` summary，不能阻塞对话。

---

## 16. Telemetry

新增事件或 span 属性：

| 名称 | 含义 |
|------|------|
| `memory.extraction.started` | 开始候选提取 |
| `memory.extraction.completed` | 完成候选提取 |
| `memory.extraction.candidate_count` | 候选数量 |
| `memory.policy.auto_saved` | 自动保存数量 |
| `memory.policy.pending` | pending 数量 |
| `memory.merge.conflict` | 合并冲突 |
| `memory.retrieval.core_count` | 核心记忆注入数量 |
| `memory.retrieval.phase_count` | 阶段相关记忆数量 |
| `memory.context.tokens_estimate` | 记忆上下文估算 token |

---

## 17. 测试策略

### 单元测试

新增或扩展：

- `backend/tests/test_memory_models.py`
- `backend/tests/test_memory_store.py`
- `backend/tests/test_memory_extraction.py`
- `backend/tests/test_memory_policy.py`
- `backend/tests/test_memory_retriever.py`
- `backend/tests/test_memory_formatter.py`

关键用例：

- v1 memory 文件迁移到 v2。
- LLM 输出 fenced JSON、坏 JSON、非列表 candidates。
- "这次预算 3 万" 不进入 global memory。
- "以后都不坐红眼航班" 进入 pending medium/global。
- "我不吃花生，过敏" 进入 high-risk pending。
- 同 key 冲突不直接覆盖 confirmed active memory。
- pending/obsolete memory 不注入上下文。
- phase 5 只检索 pace/food/accessibility 等相关 memory。

### 集成测试

- chat 一轮结束后调度 memory extraction。
- Phase 不变也能触发候选提取。
- system prompt 中出现 `## 相关用户记忆`，且数量受 limit 控制。
- memory API 可以 confirm/reject/delete。
- TripEpisode 在归档时生成。

### 评估集

建立小型 fixture：

1. 明确长期偏好。
2. 本次旅行临时偏好。
3. 冲突偏好。
4. 用户纠正旧记忆。
5. 行为反馈导致隐式偏好。
6. 过敏/健康高风险记忆。
7. 相似历史 trip episode 检索。

每个 fixture 定义 expected memory candidates 和 expected injected context。

---

## 18. 分阶段实施建议

### Phase A: 结构化 Memory Core

- 新增 `MemoryItem`、`MemorySource`、`MemoryStore`。
- 实现 v1 -> v2 迁移。
- 保持旧 `generate_summary()` 行为兼容。
- 测试模型、store、迁移。

### Phase B: Candidate Extraction + Policy

- 每轮后台提取 candidates。
- 实现 risk/policy/merge。
- 只自动保存 low-risk，其他进入 pending。
- 保留旧 Phase 1 -> 3 触发作为 fallback 或删除旧路径。

### Phase C: Retrieval + Context Injection

- 新增 retriever 和 formatter。
- `ContextManager` 注入 `## 相关用户记忆`。
- core/trip/phase 三段输出。
- 添加 token limit 和 phase domain filtering。

### Phase D: Events + Episodes

- 记录用户行为事件。
- 归档时生成 `TripEpisode`。
- 基于 episode 做规则检索。

### Phase E: API + Minimal UX Hook

- 增加 memory API。
- SSE 或普通响应中返回 pending memory 提示。
- 前端可先展示简单确认按钮；完整管理页后置。

---

## 19. Future Work

- SQLite memory store，与 session/message/archives 统一。
- Embedding retrieval 或 hybrid search。
- Temporal knowledge graph，处理事实变化和关系演进。
- 外部 connector：日历、邮箱、订单、会员、签证资料。
- Procedural memory：保存用户沟通和决策偏好，影响回复策略。
- 子任务 workspace：酒店、餐厅、交通、POI 调研分别有独立 memory scope。
- Memory management UI：浏览、编辑、合并、删除、导出。
- Privacy controls：敏感字段加密、按类型授权、审计日志。

---

## 20. 成功标准

第一阶段完成后，应满足：

- 用户长期偏好不再是自由 dict，而是可追踪、可确认、可废弃的 `MemoryItem`。
- 本次旅行信息不会污染 global memory。
- Phase 1 -> 3 之外表达的长期偏好也能被捕获。
- 高风险记忆不会静默进入 active context。
- system prompt 中只注入与当前任务相关的少量记忆。
- 历史旅行能以 `TripEpisode` 形式被保存和检索。
- memory 行为有单元测试、集成测试和 fixture 评估集覆盖。
