# 记忆提取与召回升级洞察：长期主轴应是稳定旅行习惯，而不是具体景点偏好

> 记录时间：2026-04-20
> 背景：在重新审视当前记忆提取和召回链路时，原先一度把 `destination / attraction / 经验片段` 看成旅行记忆系统最核心的缺口。进一步回到旅行场景本身后，得到一个更稳的判断：**旅行是高变化、高探索意图的场景，长期记忆最该保留的不是“去过哪里、喜欢哪个具体景点”，而是“这个人通常怎么旅行、能接受什么、不能接受什么”。** 本文据此重写此前的判断，并给出在现有架构上更划算的升级方向。

---

## 1. 先给不了解项目的人一个全景图

这一节专门写给第一次接触这个项目、甚至刚学编程不久的人。先把“这个项目为什么需要记忆、记忆和别的模块怎么分工、数据存在哪里、一次对话里提取和召回分别怎么跑”讲清楚，再往下看后面的升级洞察会轻松很多。

### 1.1 这个项目里的“记忆”到底是干什么的

`Travel Agent Pro` 不是一个纯聊天机器人，而是一个会一步步把旅行规划做完整的 Agent 系统。

它有两类完全不同的信息：

1. **当前这次旅行正在发生的事实**
2. **这个用户跨多次旅行都可能成立的习惯和历史经验**

比如：

- “这次去京都，预算 3 万，五一出发”
  这是**当前旅行事实**
- “我通常不想每天安排太满”
  这是**跨旅行可能成立的习惯**
- “上次带父母去黄山，连续爬坡太累了”
  这是**历史经验**

这个项目设计记忆系统，核心不是为了“把所有说过的话都存起来”，而是为了回答下面这类问题：

- 下次规划时，我还记不记得这个人通常怎么旅行？
- 我还记不记得他有哪些长期硬约束？
- 当用户问“按我以前的习惯来”时，我能不能拿到有用的历史线索？

所以，记忆系统的目标是：

> 在不复制当前 plan state 的前提下，保留跨旅行可复用的用户画像和旅行经验。

### 1.2 这个项目的记忆哲学：不要把 Memory 做成第二套 TravelPlanState

这个项目最重要的一条边界，来自 `docs/mind/2026-04-19-memory-system-upgrade-insight.md`，也已经写进了当前实现：

> **本次旅行的事实，归 `TravelPlanState`；跨旅行的稳定画像，归 memory；旅行结束后的整段经验，归 `TripEpisode`；当前会话里的临时信号，归 working memory。**

为什么要这样分？因为如果不分，系统会变得很乱。

比如用户说：

```text
这次带父母，所以每天别太赶
```

这句话里同时混着两层东西：

- “这次带父母” 是**当前 trip 的状态**
- “别太赶” 可能是**这次才成立的场景化偏好**，也可能以后多次出现后才变成长期习惯

如果系统把这整句直接写成长期记忆，就容易出错：

- 以后情侣游也被误判成慢节奏
- 当前旅行状态和长期画像互相重复
- 用户一改需求，系统不知道哪边才是真相

所以这个项目的记忆哲学不是“尽量多记”，而是“**按职责分层记**”。

### 1.3 当前记忆系统的四层结构

从今天仓库里的主线实现看，记忆系统已经基本分成四层：

1. **TravelPlanState**
   - 负责当前这次旅行的权威状态
   - 例如目的地、日期、预算、候选池、骨架、每日安排
   - 这部分不是 memory 主体，而是旅行规划主状态

2. **v3 Profile（长期画像）**
   - 负责跨旅行可复用的长期偏好、约束、拒绝项
   - 数据结构在 `backend/memory/v3_models.py` 的 `UserMemoryProfile` 和 `MemoryProfileItem`
   - 当前 bucket 分为：
     - `constraints`
     - `rejections`
     - `stable_preferences`
     - `preference_hypotheses`

3. **Working Memory（会话工作记忆）**
   - 负责当前 session / 当前 trip 内的临时信号
   - 例如“这轮先别考虑迪士尼”
   - 数据结构在 `SessionWorkingMemory` 和 `WorkingMemoryItem`
   - 它更像“当前对话的草稿纸”，不是长期画像

4. **TripEpisode / EpisodeSlice（旅行经验）**
   - `TripEpisode` 负责保存一次完整旅行的归档经验
   - `EpisodeSlice` 负责把经验切成更小、可召回的片段
   - 它们更像“历史案例库”和“证据切片”

一句话理解这四层：

```text
TravelPlanState = 这次旅行现在是什么样
Profile = 这个人长期通常怎么旅行
Working Memory = 这轮对话临时记一下的东西
Episode / Slice = 过去旅行留下来的经验档案
```

### 1.4 数据实际存在哪里

当前主线的持久化主要落在文件系统里，路径在 `backend/data/users/{user_id}/...`。

最关键的几个文件是：

```text
backend/data/users/{user_id}/memory/
  profile.json                    # v3 长期画像
  episode_slices.jsonl            # v3 历史经验切片
  sessions/{session_id}/trips/{trip_id}/working_memory.json  # 当前 trip 的 working memory

backend/data/users/{user_id}/memory/
  episodes.jsonl                  # v3 历史完整旅行归档
  events.jsonl                    # v3 记忆审计事件流
```

这里要特别注意：

- **现在主线是 v3-only**：`profile + trip-scoped working memory + episode slices`
- 旧的 `memory.json`、`MemoryItem`、v2 `TripEpisode` 已退出 runtime，不再作为兼容资产保留。

也就是说，这个项目现在是“**v3-only 分层记忆**”的状态：长期画像、当前 trip 工作记忆、历史归档与切片各自有明确边界。

### 1.5 当前的数据模型长什么样

如果把最重要的模型用最直白的话说，可以这样理解：

#### A. `MemoryProfileItem`

它是一条长期画像条目，关心的是：

- 属于哪个领域 `domain`
- 是什么规则 `key`
- 值是什么 `value`
- 是偏好还是规避 `polarity`
- 稳定性如何 `stability`
- 置信度多少 `confidence`
- 现在状态是 active 还是 pending `status`
- 适用于什么场景 `applicability`

比如：

```text
domain = hotel
key = avoid_hostel
value = true
polarity = avoid
stability = explicit_declared
status = active
```

#### B. `WorkingMemoryItem`

它是一条只想在当前 session 里暂时保留的信号，关心的是：

- 它是什么类型 `kind`
- 跟哪些领域有关 `domains`
- 内容是什么 `content`
- 为什么值得临时保留 `reason`
- 什么时候过期 `expires`

比如：

```text
这轮先别考虑迪士尼
```

#### C. `EpisodeSlice`

它不是长期偏好本身，而是“过去某次旅行里的一个可复用经验切片”。

它关心的是：

- 它来自哪次旅行 `source_episode_id`
- 它属于什么经验类型 `slice_type`
- 和哪些领域相关 `domains`
- 命中的实体是什么 `entities`
- 具体内容 `content`
- 适用边界 `applicability`

比如：

```text
上次雨天缺少室内备选，导致半天体验下降
```

### 1.6 “提取”是怎么发生的

当前每次用户发消息后，系统会同时做两件事：

1. **继续当前聊天主流程**
2. **后台异步提交一次记忆提取任务**

这条后台链路在 `backend/main.py` 里，逻辑是：

```text
用户消息进入 chat
  ↓
先 append 到 messages
  ↓
提交 memory snapshot 到 MemoryJobScheduler
  ↓
后台先跑 gate（值不值得提取）
  ↓
如果值得，再跑 extraction（提取结构化候选）
  ↓
policy 分类：drop / pending / active
  ↓
写入 profile 或 working memory
```

这里有几个关键设计：

#### A. 提取是异步后台任务，不阻塞回答

这是为了防止：

- 用户等回答时还要等记忆提取
- 提取慢的时候把主对话拖慢

所以提取走的是 background internal task，而不是 chat 关键路径。

#### B. 提取前先过 gate

gate 做的不是“提取”，而是“**判断这轮值不值得提取**”。

比如：

- “这次五一去京都，预算 3 万” -> 不值得提取
- “我不住青旅” -> 值得提取
- “继续吧” -> 不值得提取

这样做是为了避免每一轮都跑完整 extraction，浪费模型调用和存储空间。

#### C. 提取只看一小段用户消息窗口

`MemoryJobScheduler` 不会傻傻地反复吃完整聊天记录，而是：

- gate 看最近更短的一段窗口
- extraction 看“自上次消费后新增的消息窗口”
- 如果用户连续发很多条，只保留最新快照，做 latest-wins coalescing

这个设计的目的是：

- 避免重复提取
- 避免积压很多旧任务
- 让后台 memory job 始终追最新用户状态

### 1.7 “召回”是怎么发生的

和提取相反，召回发生在**回答前的关键路径**里。

当前 chat 主流程是：

```text
用户消息进入
  ↓
先触发 memory_recall internal task
  ↓
MemoryManager.generate_context()
  ↓
拿到 memory_context
  ↓
把 memory_context 拼进 system prompt
  ↓
再调用主 LLM 回答
```

这里要抓住一个大区别：

- **提取**是“用户说完以后，我顺手记一下”
- **召回**是“我准备回答之前，先想一想过去有没有相关记忆”

因为召回在关键路径上，所以它今天是偏保守、偏确定性的设计。

### 1.8 当前召回具体分哪几步

`MemoryManager.generate_context()` 里，当前召回大致分成四步：

#### 第一步：固定注入长期画像和 working memory

系统会先加载：

- active 的长期 profile 条目
- active 的 working memory 条目

这部分相当于“默认就带着走的记忆背景”。

#### 第二步：判断这轮用户消息是否值得做 query-aware recall

这一步由 `symbolic_recall.py` 负责，靠的是规则触发：

- 有没有“上次”“之前”“我是不是说过”之类的历史查询信号
- 有没有明显的偏好查询信号

如果没有，就不额外做 query-aware recall。

#### 第三步：对 profile 和 episode slices 做符号化检索

如果判定需要查记忆，系统会：

- 先把用户消息转成结构化 `RecallQuery`
- 然后在 profile 里按 domain / keyword 做匹配
- 再在 episode slices 里按 destination / domain / keyword 做匹配

这是一个**symbolic recall**，不是向量检索。

也就是说，它更像：

- 规则判断
- 关键词匹配
- 小范围排序

而不是 embedding / vector DB / 语义检索。

#### 第四步：格式化成 prompt 可读文本

召回结果不会直接把 JSON 原样塞给大模型，而是会经过 `format_v3_memory_context()`，变成类似：

- 长期用户画像
- 当前会话工作记忆
- 本轮命中的历史记忆

这样的自然语言片段，再拼进 system prompt。

### 1.9 当前 TripEpisode 和 EpisodeSlice 是怎么归档的

除了每轮提取和每轮召回，这个系统还有第三条线：**旅行结束后的归档**。

当一段旅行完成后，系统可以把这次旅行归档成 `TripEpisode`：

- 目的地
- 日期
- 预算
- skeleton
- accepted / rejected items
- lessons
- 总结

然后理论上还可以进一步切成 `EpisodeSlice`，作为未来召回时的短证据。

这条线的意义是：

- profile 负责“这个人通常怎样”
- episode 负责“上一次具体发生了什么”

这两者不是一回事。

### 1.10 当前系统的一个现实状态：哲学已经比较清楚，实现还没有完全长齐

如果只看设计思路，这套系统已经有比较清楚的理念：

- 不复制当前 trip state
- 长期画像、working memory、历史经验分层
- 提取异步、召回同步
- query-aware recall 只在显式历史/偏好查询时触发

但如果回到代码现实，也要看到它仍有后续增强空间：

1. 长期画像真正能稳定进入 `active` 的东西还偏少
2. `applicability` 字段还没有被充分用起来
3. `episode_evidence / episode_slices` 这条经验通道还偏弱
4. 当前召回仍以 native symbolic recall 为主，rewrite / rerank 还有继续增强空间
5. v3-only cutover 已完成，后续重点是提升画像质量、证据切片质量和召回排序质量

理解完这一层，就能明白本文后面讨论的重点：

> 现在真正该补的，不是再多记一些具体内容，而是让这套分层设计更准确地记住“稳定旅行习惯”，并把具体经验更好地当作证据使用。

---

## 2. 讨论起点

围绕记忆系统，先有两个相邻判断：

1. **召回架构是否应该对齐提取架构？** 结论仍然是不应该。召回在 chat 关键路径上，必须快、必须可降级；提取是后台副作用，允许异步、允许静默失败。两者生命周期不同，不应强行做成镜像结构。
2. **既然要继续升级记忆系统，到底该优先补什么？** 这次的结论和之前不同：优先级不应该放在“让系统长期记住更多目的地和景点偏好”，而应该放在“让系统更稳定地提取、存储、召回用户的长期旅行习惯”。

一句话概括新的出发点：

> 在旅行场景里，长期记忆的核心价值不是复读用户喜欢过什么内容，而是稳定理解这个人一贯的旅行方式。

---

## 3. 先看现状：当前提取链路实际能进入 active 的内容仍然很窄

当前 `v3` 提取链路由 gate + extraction + policy + store 四段组成。从“用户说一句话”到“这条信息能以 `active` 形态进入下轮召回”之间，仍然要穿过多层过滤。

### 2.1 Layer 0：Gate LLM 会先过滤掉当前 trip state 推进

`backend/memory/extraction.py:521` 的 gate prompt 明确要求：

- 当前 trip 的目的地 / 日期 / 预算 / 旅客 / 候选池 / 骨架 / 每日安排，不值得继续提取
- 寒暄、确认、重复既有偏好、空泛追问，不值得继续提取
- 已有记忆摘要已经覆盖且没有新增细化或冲突的信号，不值得继续提取

这层过滤本身没有问题。它守住了一个重要边界：

> 当前旅行事实应该归 `TravelPlanState`，不是归 memory。

### 2.2 Layer 1：Extraction prompt 继续禁止复制当前 trip state

`backend/memory/extraction.py:458` 的 extraction prompt 继续禁止输出：

- 当前目的地 / 日期 / 预算 / 人数 / 候选池 / 骨架 / 每日计划
- PII
- 对用户没明确说过的偏好推测
- 已有 profile / working memory 的重复内容

这也没有问题。它继续守住“不要把 memory 做成第二套 state”。

### 2.3 Layer 2：Schema 决定了哪些东西可以进入长期画像

当前 `_V3_PROFILE_DOMAINS` 包括：

```python
pace / food / hotel / accommodation / flight / train / budget /
family / accessibility / planning_style / documents / general
```

这里面已经覆盖了一部分真正有价值的长期习惯：

- 节奏
- 饮食
- 住宿
- 航班 / 火车
- 预算
- 家庭 / 无障碍
- 规划风格

这说明系统并不是完全偏离方向。相反，**它已经在试图捕捉“人怎么旅行”这条主轴。**

真正的问题是：

1. 这些长期习惯进入 `active` 的门槛仍然偏高
2. 已经有的 `applicability` 字段几乎没被用起来
3. 具体旅行经验没有被有效沉淀成“可用于未来判断的证据”

### 2.4 Layer 3：Policy 让很多信号卡在 pending

`backend/memory/policy.py:175` 的分类逻辑决定：

- `preference_hypotheses` 一律 `pending`
- `family / documents / accessibility` 一律 `pending`
- `constraints / rejections` 需要 `explicit_declared + confidence >= 0.8` 才能 `active`
- `stable_preferences` 需要 `explicit_declared` 或 `pattern_observed` 且 `confidence >= 0.8` 才能 `active`

这导致一个现象：

> 系统理论上知道该记什么，但真正能稳定进入长期画像的，还是只有少数最硬的约束和最明确的口味。

### 2.5 当前能 active 进入画像的真实子集

目前真正容易进入 `active profile` 的，多半还是：

- 不坐红眼航班
- 不住青旅
- 不吃辣
- 每天别排太满
- 预算上限比较明确

而下面这些，即使很有旅行价值，也未必能稳定进长期画像：

- 带老人时明显更在意少折腾
- 更愿意住核心区步行可达的位置
- 对频繁换酒店容忍度低
- 更接受“1-2 个核心点 + 留白”的节奏
- 对长距离转场非常敏感

这些不是“内容偏好”，而是更接近真正的**稳定旅行习惯**。可它们今天并没有被系统很好地吸收到长期记忆里。

---

## 4. 新判断：旅行长期记忆的主轴，应该是稳定旅行习惯

这次最重要的修正是：

**之前把 `destination / attraction / 经验片段` 一起视为旅行记忆系统最核心的缺口，这个判断不够稳。**

更准确的说法应该是：

> 旅行是多样性的。长期记忆真正应该优先保留的，是跨多次旅行仍然可能成立的习惯、边界和决策方式，而不是某次旅行里喜欢过的具体目的地或景点。

### 3.1 什么是高价值长期记忆

高价值长期记忆通常具备两个特征：

1. **跨旅行可迁移**
2. **会稳定影响未来规划决策**

例如：

- 节奏偏好：松一点 / 紧一点 / 每天最多几个核心点
- 交通容忍度：是否接受红眼航班、转机、长途拉车、频繁换乘
- 住宿底线：是否接受青旅、小房间、频繁换酒店、远郊住宿
- 饮食边界：忌口、排斥项、接受度
- 预算观：愿不愿意为住宿 / 体验 / 交通花钱
- 决策风格：喜欢尽早锁定，还是保留弹性
- 同行场景下的稳定习惯：带娃 / 带老人 / 情侣 / 独行时的常见偏好

这些信息会直接影响：

- Phase 1 候选方向
- Phase 3 骨架设计
- Phase 5 每日节奏和转场密度
- Phase 7 风险提醒和查漏方式

### 3.2 什么不是长期记忆的核心

下列信息不是完全没价值，但不应占据长期记忆主轴：

- “喜欢京都”
- “喜欢寺庙”
- “上次想去某个具体景点”
- “某次住过某一家酒店”

原因不是它们完全无用，而是它们容易有三个问题：

1. **复用性弱**：这次喜欢，不代表下次也要按同样内容来
2. **语境依赖强**：同行人、季节、预算、旅程目标一变，原结论就可能失效
3. **容易让系统过拟合**：把“曾经喜欢过”误当成“以后都喜欢”

### 3.3 经验仍然有价值，但价值在“抽象后的规律”

这不代表具体经验没价值。它们仍然重要，但角色应该调整：

- 具体经验更适合做 `episode evidence`
- 只有当经验能抽象成可迁移规律时，才值得推动到更高层记忆

例如：

```text
上次住祇园四条附近步行很方便
```

这条原始经验本身更适合做 evidence。

真正适合进入长期习惯层的，可能是它后面的抽象：

```text
住宿更偏好步行可达核心区域
远郊住宿会显著拉低体验
```

所以，经验片段不是长期主轴本身；它更像长期主轴的证据来源。

---

## 5. 当前系统真正漏掉的，不是“景点偏好”，而是三种更重要的能力

基于上面的判断，当前系统更值得补的是以下三件事。

### 4.1 系统还不够会提取“稳定旅行习惯”

现在系统虽然有 `pace / hotel / flight / budget / planning_style` 等 domain，但实际提取出来并进入 `active` 的内容仍然偏少，尤其是下面这类高价值习惯：

- 对转场折腾的容忍度
- 对频繁换酒店的容忍度
- 对核心区住宿的偏好
- 对日程密度的长期接受范围
- 场景化习惯是否只在带娃 / 带老人时成立

换句话说，**方向没有错，但提取能力不够细。**

### 4.2 系统还不够会区分“长期习惯”和“场景化反应”

旅行偏好高度依赖语境。

例如：

```text
这次带父母，所以每天别安排太满
```

它不应该直接升级成：

```text
用户长期喜欢慢节奏
```

真正应该保存的是：

- 这是一次场景化观察
- 适用场景是 `with_parents` / `senior_friendly`
- 是否跨多次旅行成立，还需要未来验证

也就是说，当前最缺的不是更多 domain，而是更强的：

- `applicability`
- `context`
- `stability`

### 4.3 系统缺少“经验 -> 规律”的证据通道

当前 `episode_slices` 通道几乎没有进入主链路的有效数据。结果是：

- 系统很难利用过去旅行中的具体经验
- 更难把这些经验反过来支持长期习惯判断

因此真正缺的不是“把更多具体 POI 写进 profile”，而是：

> 给具体旅行经验一个可靠的 evidence 通道，让它在未来召回时既能被看到，又不会被误当成长期硬偏好。

---

## 6. 最小改动 × 最大收益：这次更推荐的三个改动

这次的“最小改动”边界仍然保持不变：

- 不引入新存储
- 不加向量检索
- 不新增独立的第三段经验提取 LLM
- 不重构 gate -> extraction -> policy -> store 的总顺序

在这个边界下，更合理的三个改动如下。

### 5.1 改动 A：重写 extraction prompt 的价值排序

这次最该改的第一件事，不是 schema，而是 prompt 的价值排序。

应明确告诉 extraction LLM：

1. 优先提取**跨旅行更可能成立的稳定旅行习惯**
2. 对“这次因为某个场景才这样”的信号，优先作为 `preference_hypotheses` 或 evidence
3. 不要因为用户表达过某个具体目的地 / 景点喜好，就轻易升格为长期画像

可以直接加入正反例：

```text
“我通常不想频繁换酒店” -> 更接近长期习惯
“这次先别考虑迪士尼” -> working memory
“我上次在京都住祇园很方便” -> episode evidence
“我喜欢京都” -> 默认不是长期稳定偏好，除非有跨多次旅行证据
```

**为什么收益大**：

- 这是整个提取链路的价值入口
- 它直接决定系统把什么看成“该长期记住的东西”
- 而这个价值排序，正是旅行场景里最容易做错的地方

### 5.2 改动 B：不要优先扩 `destination / attraction` 到 profile；优先补 experience evidence 通道

之前的判断里，一度把“给 profile 增加 `destination` / `attraction` domain”当成最小高收益改动。

现在看，这不是最优先。

更合理的做法是：

1. **保守对待 `destination / attraction` 进入长期画像**
2. **优先把具体经验通过 evidence 通道落下来**
3. **让未来召回阶段基于这些 evidence 判断是否能抽象出稳定规律**

从当前代码结构看，`parse_v3_extraction_response()` 已经在解析 `episode_evidence`，但 tool schema 还没有把这个字段正式接起来。这说明代码里已经有一条半成品通道，适合继续补齐，而不是先把 `destination / attraction` 抬成长期画像核心。

建议的方向是：

- extraction tool 正式支持 `episode_evidence`
- 每条 evidence 带上：
  - `source_destination`
  - `slice_type`
  - `domains`
  - `content`
  - `applicability`
  - `entities`
- 这些内容进入 episode / slice 证据层，不直接进入 active global profile

**为什么收益更大**：

- 它保留了具体经验
- 又不会把系统引向“过度记住内容偏好”
- 后续还能反向支持“经验 -> 稳定习惯”的判断

### 5.3 改动 C：把 `applicability` 从闲置字段变成主字段

当前 `applicability` 已经存在于 `v3_models.py`，但提取和召回里没有把它当成真正的一等公民。

旅行场景下，这个字段非常关键，因为很多偏好只有在特定场景才成立：

- `with_kids`
- `with_parents`
- `couple`
- `solo`
- `business`
- `short_break`
- `long_haul`

prompt 应明确要求：

- 如果一条偏好明显只在特定旅行形态下成立，必须标注 `applicability`
- 不确定时可以写 `general`，但要下调稳定性判断

**为什么收益大**：

- 它直接减少过度泛化
- 它让系统更容易区分“稳定习惯”和“场景化习惯”
- 它也为后续召回 rerank 提供了最有价值的过滤条件

---

## 7. 基于这个新前提，召回重构也要换重心

如果长期主轴是“稳定旅行习惯”，那召回层的 Rewrite / Retrieve / Rerank 也不应围绕“更多景点和目的地偏好”来设计，而应围绕三件事：

1. 当前用户是不是在问长期习惯
2. 当前用户是不是在问历史经验证据
3. 当前用户是不是只在推进本次 trip state

### 6.1 Rewrite：先判定“你到底是在问习惯，还是在问证据”

Rewrite 更合理的目标不是扩展更多 POI 同义词，而是先把问题分成几类：

- `habit_recall`：按我的习惯来、我是不是说过不喜欢折腾
- `evidence_recall`：上次去京都住哪里、之前踩过什么坑
- `current_trip_state`：这次预算多少、当前选了哪个骨架
- `chit_chat`

其中最关键的是把：

- **长期习惯查询**
- **历史经验查询**
- **当前旅行状态查询**

这三类严格分开。

### 6.2 Retrieve：优先召回稳定习惯，其次召回证据

新的候选池更适合分层：

```python
candidates = {
    "active_profile": top-10,      # 稳定习惯 / 硬约束优先
    "pending_profile": top-5,      # 场景化假设，必要时纳入
    "working_memory": top-5,       # 只在本轮明确相关时纳入
    "episode_evidence": top-10,    # 历史经验，不是长期主轴，但可做证据
}
```

这里的排序逻辑也应调整：

- 问“按我习惯来”时，`active_profile` 权重最高
- 问“上次发生过什么”时，`episode_evidence` 权重最高
- 问当前 trip state 时，memory 应整体降级甚至跳过

### 6.3 Rerank：不是为了更会找景点，而是为了更会判断“这条记忆现在该怎么用”

Rerank 的真正价值在于：

1. 判断哪些记忆可以直接影响本轮决策
2. 判断哪些只是背景参考
3. 判断哪些 evidence 已经足以支持某条 pending habit 被再次印证

这一层的增值点仍然成立，但侧重点应该改成：

- `direct_state_check`：明确硬约束和长期稳定习惯
- `reference_material`：一般习惯或历史证据
- `temp_override`：本轮场景下的临时放宽或收紧
- `promote_signal`：某条 pending habit 在新场景中再次被印证

尤其最后一项很关键：

> 自动升级 pending -> active，更适合发生在“有上下文的召回阶段”，而不是提取阶段。

因为只有在召回阶段，系统才更容易判断：

- 这到底是一次性的场景反应
- 还是跨场景重复出现的稳定习惯

### 6.4 Formatter：按“本轮作用”分段，比按来源分段更合理

当前 `format_v3_memory_context()` 主要按“长期画像 / 工作记忆 / 命中历史”分段。

更符合旅行场景的方式是按用途分段：

```text
## 本轮直接影响决策的长期习惯
- 不接受红眼航班
- 更偏好少换酒店

## 场景化参考
- 带老人时更倾向慢节奏
- 短假期对长距离转场容忍度低

## 相关历史证据
- 上次雨天缺少室内备选，体验下降
- 上次核心区住宿显著提升步行效率

## 本轮临时覆盖
- 这次用户愿意把节奏稍微拉紧
```

这样模型看到的是：

- 哪些是稳定习惯
- 哪些只是场景化参考
- 哪些只是证据
- 哪些是本轮临时变化

这比简单按 bucket 或 source 分段更贴近真实决策。

---

## 8. 不建议做的事

基于这次重估，以下方向不再是优先选项。

### 7.1 不建议把 `destination / attraction` 默认抬成长期画像主轴

这些信息可以保留，但默认不应获得和“交通容忍度 / 住宿底线 / 节奏习惯”同等级别的长期记忆地位。

如果未来确实要支持，也更适合：

- 先作为 evidence 或 hypothesis 存在
- 只有跨多次旅行、跨场景重复出现时，才考虑提升权重

### 7.2 不建议把具体经验直接当长期偏好写入 active profile

例如：

```text
上次住祇园方便
```

不应直接写成：

```text
长期偏好：所有旅行都住核心区
```

中间至少需要一层 evidence -> habit 的判断。

### 7.3 不建议让 recall 的优化目标变成“更会猜用户喜欢哪些景点”

这会把旅行 agent 往内容推荐系统带偏。

旅行规划 agent 更重要的是：

- 把路线安排对
- 把节奏安排对
- 把折腾程度控制对
- 把用户真正不能接受的边界守住

---

## 9. 更合理的落地顺序与验收口径

### 8.1 Week 1：先修提取价值排序

内容：

- 重写 extraction prompt
- 强化“优先提取稳定旅行习惯”
- 强化“具体目的地 / 景点喜好默认不升级为长期画像”
- 明确 `applicability` 填写要求

验收：

- 20 个 case 中，长期旅行习惯类条目提取率显著提升
- 误把当前 trip state 写进长期记忆的比例不升
- `applicability` 非空比例明显提升

### 8.2 Week 2：补 evidence 通道

内容：

- 正式接通 `episode_evidence`
- 让具体经验有地方落，但不直接进入 active global profile

验收：

- 含“上次住哪里 / 上次踩过什么坑 / 上次为什么那样安排”的 case 能稳定写出 evidence
- 新 evidence 写入率显著高于当前近乎空的状态

### 8.3 Week 3：升级召回排序

内容：

- rewrite 先区分 habit / evidence / current trip state
- retrieve 调整候选池优先级
- rerank 增加 `promote_signal`
- formatter 改为按用途分段

验收：

- “按我习惯来”类问题，命中的长期习惯更稳定
- “上次发生过什么”类问题，命中的 evidence 更稳定
- 当前 trip state 问题不被历史记忆噪音干扰

---

## 10. 暂定结论

这次重估后的结论是：

1. 当前 v3 提取链路真正能进入 `active profile` 的内容仍然很窄，这个判断没有变。

2. 但之前把 `destination / attraction / 经验片段` 一起视为旅行记忆系统最核心缺口，这个判断需要修正。

3. 在旅行场景里，长期记忆最该优先保留的不是“用户喜欢过什么具体内容”，而是：
   - 这个人通常怎么旅行
   - 能接受什么折腾
   - 不能接受什么边界
   - 在不同同行场景下哪些习惯会稳定出现

4. 具体目的地 / 景点信息不是完全没价值，但更适合做：
   - working memory 的临时线索
   - episode evidence 的历史证据
   - pending hypothesis 的弱信号
   而不是长期画像主轴。

5. 经验片段仍然重要，但它的价值主要不在“直接进入长期画像”，而在“作为 evidence 支持系统抽象出稳定规律”。

6. 因此，更划算的升级顺序不是“先把 destination / attraction 写进 profile，再做更强召回”，而是：
   - 先让系统更会提取稳定旅行习惯
   - 再把具体经验沉淀为 evidence
   - 最后让召回阶段完成 habit / evidence / current state 的精细分流

最终原则：

> 对旅行 agent 来说，长期记忆的主轴应该是稳定旅行习惯；具体内容偏好和单次旅行经验可以保留，但默认只能作为弱信号或证据，不能轻易升格为长期核心画像。
