# 记忆系统面试压力评审

本文基于当前代码实现，对项目记忆系统做面试式高压审视。

补充说明（2026-04-18 复核）：

- 本文大部分核心判断仍然成立。
- 当前系统已经补上了记忆命中的可观测性：`generate_context()` 会返回命中 item id/计数，`backend/main.py` 会记录 `memory_hits`，并在 SSE 中发出 `memory_recall`。
- 但这属于“看见命中了什么”，不是“扩大了召回的知识来源”。`TripEpisode` 仍未进入主召回闭环，这也是本文的核心批评点之一。

评审原则：

- 先讲项目里现在是怎么做的，不默认读者了解本项目
- 再讲它对旅行垂类 Agent 的真实问题
- 最后给出三档修复方案：最佳实践方案、投入最少收益最大方案、大厂务实方案

本文不是否定式吐槽，而是把“哪里真有问题、哪里只是表述过猛”拆开说清楚。

## 1. TripEpisode 写入了，但没有进入主召回闭环

### a. 项目中相关功能/模块的具体实现

本项目的记忆系统位于 `backend/memory/`，核心对象分两类：

- `MemoryItem`：结构化记忆条目，用于用户偏好、约束、拒绝项等可检索记忆，定义在 `backend/memory/models.py`
- `TripEpisode`：一次完整旅行或一次规划会话的归档摘要，定义在 `backend/memory/models.py`

在运行链路上，后端会在合适时机把本次会话中的记忆和计划状态整理成一个 episode：

- `backend/main.py:1077-1149`
- `_build_trip_episode(...)` 会从当前 `TravelPlanState` 中提取目的地、日期、预算、已选骨架、被接受和被拒绝的 memory items、lessons 等信息，构造成 `TripEpisode`
- `_append_trip_episode_once(...)` 会把这个 episode 追加写入 `backend/data/users/{user_id}/trip_episodes.jsonl`

对应的存储实现位于 `backend/memory/store.py`：

- `append_episode(...)` 负责把 episode 追加到 `trip_episodes.jsonl`
- `list_episodes(...)` 负责把历史 episode 读出来

但在真正给 LLM 构造记忆上下文时，主链路调用的是 `MemoryManager.generate_context(...)`，实现位于 `backend/memory/manager.py`。这条链路只做了三类召回：

- `retrieve_core_profile(items)`
- `retrieve_trip_memory(items, plan)`
- `retrieve_phase_relevant(items, plan, plan.phase)`

这里的输入全部都是 `MemoryItem`，没有把 `TripEpisode` 纳入召回结果，也没有把 episode 格式化后注入到 system prompt。

需要补一句当前新变化：`generate_context(...)` 现在会额外返回命中的 item id 和分组计数，`backend/main.py` 用它们记录 telemetry，并通过 SSE 发出 `memory_recall`。但这没有改变召回源仍然只来自 `MemoryItem` 的事实。

### b. 当前存在的具体问题

对旅行垂类 Agent 来说，episode 不是“锦上添花”，而是最接近真实旅行经验复用的那层资产。

一个用户第二次来规划旅行时，真正有价值的信息通常不是某个离散的偏好键值对，而是：

- 上次去了哪里，玩了几天，满意还是不满意
- 上次最后选了怎样的旅行骨架
- 上次哪些决策被接受，哪些被否掉
- 上次踩了哪些坑，这次是否应主动规避

现在的问题是：项目已经把这些东西写成了 `TripEpisode`，但没有把它们读回主推理链路。结果就是：

- 系统付出了归档成本，但主 Agent 几乎感知不到这份经验资产
- 用户跨旅行会话的“经验迁移”能力弱
- 记忆系统更像“偏好 KV 存储”，而不是“旅行经验系统”

对于旅行垂类 Agent，这是明显的能力缺口。因为旅行规划本质上高度依赖过去同类决策经验，而不是只靠静态偏好字段。

### c. 修复意见

最佳实践方案：

- 给 `TripEpisode` 建立独立召回链路，而不是硬塞进 `MemoryItem`
- 按目标目的地、旅行天数、同行人类型、预算段、满意度、最近一次出行等维度做 episode 检索与重排
- 在 `MemoryManager.generate_context(...)` 中新增 `episode_recall`，将“过往相似旅行经验”作为单独 section 注入 system prompt
- 引入 episode 摘要压缩策略，只保留对当前阶段有帮助的经验，例如 Phase 1 更关心目的地和满意度，Phase 5 更关心骨架与 lessons

投入最少收益最大方案：

- 保持当前文件存储不变
- 在 `MemoryManager.generate_context(...)` 中补一次 `list_episodes(user_id)`
- 只召回最近 3 条、目的地相同或最近完成的 episode
- 把它们格式化为单独的“历史旅行经验”区块注入 prompt

这是最值得先做的改动，因为实现成本低，但能明显提升“记得上次旅行发生了什么”的感知。

大厂务实方案：

- 先不追求复杂检索，只做规则式 episode recall
- 建立明确的 prompt 预算上限，例如 episode section 最多 400-600 tokens
- 只在 Phase 1、Phase 3 注入 episode，Phase 5/7 默认不注入，避免上下文膨胀
- 在 telemetry 中记录“episode 是否被召回、是否命中当前决策”的效果指标，先验证 ROI，再决定是否升级到 embedding 检索

## 2. 记忆召回以规则过滤为主，不是语义召回

### a. 项目中相关功能/模块的具体实现

当前的结构化记忆召回主要由 `backend/memory/retriever.py` 负责，核心逻辑有三段：

- `retrieve_core_profile(...)`：召回 `scope == global` 且 `status == active` 的核心画像
- `retrieve_trip_memory(...)`：召回 `scope == trip` 且 `trip_id` 与当前计划匹配的本次旅行记忆
- `retrieve_phase_relevant(...)`：根据当前阶段白名单筛选 domain，再按置信度和更新时间排序

阶段白名单由 `_PHASE_DOMAINS` 定义：

- Phase 1：`destination`、`pace`、`budget`、`family`、`planning_style`
- Phase 3：`destination`、`pace`、`budget`、`family`、`hotel`、`flight`、`train`、`accessibility`
- Phase 5：`pace`、`food`、`accessibility`、`family`、`budget`
- Phase 7：`documents`、`flight`、`train`、`food`、`accessibility`

排序逻辑也比较简单，位于 `_sort_key(...)`：

- `constraint` 和 `rejection` 比普通 preference 更优先
- 再按 `confidence` 降序
- 再按 `updated_at` 降序

也就是说，当前召回不是“语义上最相关的记忆”，而是“规则过滤后最像应该出现的记忆”。

### b. 当前存在的具体问题

对旅行垂类 Agent 来说，这种规则检索有两个天然问题。

第一，它强依赖提取阶段给出的 `domain` 是否命中预设分类。如果提取时把一个很关键的偏好放进了 `general`，那后面各阶段可能根本召回不到。

第二，它不会理解自然语言之间的相似性。用户说“我不喜欢人太多的地方”，你也许提取成了 `general/crowd_preference`；后续在做节奏规划、区域筛选、景点推荐时，这条记忆其实很重要，但规则召回不一定命中。

这会导致一个典型问题：

- 系统“有记忆”，但召回不出来
- 召回出来的，又不一定是当前这轮最有帮助的

在旅行规划场景里，很多偏好并不是硬字段，而是语义模糊、跨阶段生效的习惯。例如：

- 不喜欢排队
- 想住交通方便但别太游客区
- 带老人，节奏要松，但又不想太无聊

这类信息靠 domain 白名单很难稳定命中。

### c. 修复意见

最佳实践方案：

- 引入向量化语义检索，为 `MemoryItem.value`、`key`、`reason`、`attributes` 的可检索文本建立 embedding
- 召回时做混合检索：规则过滤做安全边界，embedding 相似度做相关性排序
- 对不同 phase 定义不同 query 生成模板，例如 Phase 1 用“目的地偏好类 query”，Phase 5 用“节奏/饮食/可达性类 query”

投入最少收益最大方案：

- 先不接向量库
- 扩充 `retrieve_phase_relevant(...)` 的逻辑：除了 `domain` 命中，还允许基于 `key/value` 关键词做软匹配
- 对 `general` 域的高置信度 item 做一次补充扫描，避免重要记忆因分类保守而永久沉底

大厂务实方案：

- 先保留规则召回作为主路径，保证稳定性和可解释性
- 增加一个“候选补充召回”分支，只从高置信度、最近更新、`general` 域中补 1-3 条潜在相关项
- 通过线上日志观察这些补充召回是否真的改善工具选择或回复质量，再决定是否正式引入 embedding 基础设施

## 3. 记忆提取输入过窄，只看用户明文消息

### a. 项目中相关功能/模块的具体实现

记忆提取主链路在 `backend/main.py` 的 `_extract_memory_candidates(...) / _do_extract_memory_candidates(...)`。

大致流程是：

1. `_extract_memory_candidates(...)` 从 `messages_snapshot` 里筛出最近几条用户消息
2. `build_candidate_extraction_prompt(...)` 把这些用户消息、当前已有 memory items、当前计划事实拼成一个 extraction prompt
3. 使用专门的提取模型调用 LLM，拿回候选 memory items
4. 用 `MemoryPolicy` 做分类：`drop / pending / auto_save`
5. 用 `MemoryMerger` 合并进现有 items，然后逐条 `upsert`

其中最关键的输入筛选逻辑是：

- 只保留 `message.role == Role.USER`
- 默认只取最近 `max_user_messages` 条，配置在 `backend/config.py`，默认是 8 条
- 然后再附带当前 `plan_facts` 和已有 `memory_items`

这意味着 assistant 回复、工具调用结果、工具反馈、用户对候选方案的接受或否定方式，都不会直接进入提取 prompt。

### b. 当前存在的具体问题

这套设计对于“抽取用户显式偏好”是成立的，但对于旅行垂类 Agent 来说仍然偏窄。

用户的稳定偏好经常不是一句话直接说出来的，而是在交互行为里逐步显露：

- 连续拒绝红眼航班
- 总是追问亲子友好、无障碍、步行距离
- 对某类区域、酒店风格、行程节奏反复否决
- 面对几个候选方案，总是偏向低密度、高确定性的那个

这些信号大多存在于：

- assistant 提出的候选项
- 工具返回的真实方案
- 用户对这些方案的二次反馈

如果提取器只看用户原话，就会错过大量“偏好是通过选择行为表达出来”的信息。对旅行 Agent 来说，这会让记忆系统更像“聊天摘抄”，而不是“用户决策建模”。

### c. 修复意见

最佳实践方案：

- 把记忆提取改造成对话级抽取，而不是用户消息摘抄
- 输入不只包含用户文本，还包含：最近几轮 assistant 提议、关键工具结果摘要、用户对这些候选方案的接受/拒绝信号
- 明确区分“用户明确表达”“用户行为推断”“系统推断待确认”三种证据等级，并映射到不同风险等级

投入最少收益最大方案：

- 先不把所有消息都塞进提取 prompt
- 只把最近几轮中与用户决策直接相关的 assistant 内容拼进去，例如候选酒店、交通方式、骨架方案摘要
- 当用户出现明显否定或确认时，把那一轮前后的 assistant 提议一起交给 extraction model

大厂务实方案：

- 默认仍以用户消息为主，避免提取成本和提示词复杂度失控
- 只在以下场景启用增强提取：
- 用户做了明确选择
- 用户连续两次否定同类方案
- Phase 3 的 shortlist / skeleton / lock 决策完成
- 这样能在不大改架构的前提下，把“决策行为”纳入记忆系统

## 4. 记忆系统存在明显的新旧模型并存负担

### a. 项目中相关功能/模块的具体实现

项目中同时存在两套记忆数据表示。

旧模型定义在 `backend/memory/models.py`：

- `UserMemory`
- `Rejection`
- `TripSummary`

这套模型是偏传统画像结构：

- `explicit_preferences: dict[str, Any]`
- `implicit_preferences: dict[str, Any]`
- `trip_history: list[TripSummary]`
- `rejections: list[Rejection]`

新模型同样定义在 `backend/memory/models.py`：

- `MemoryItem`
- `MemoryCandidate`
- `MemoryEvent`
- `TripEpisode`

存储层 `backend/memory/store.py` 会在读取时兼容两种格式：

- `load_envelope(...)` 发现 `schema_version == 2`，走新模型 envelope
- 否则会把旧 `UserMemory` 迁移成 `MemoryItem`

管理层 `backend/memory/manager.py` 也有回退兼容逻辑：

- `load(...)` 在读到 v2 数据时，优先读 `legacy`
- 如果没有 `legacy`，再通过 `_legacy_memory_from_items(...)` 把 `MemoryItem` 反推成 `UserMemory`

也就是说，当前系统不是“已完成迁移”，而是“新旧模型并存并互相转换”。

### b. 当前存在的具体问题

这不是立刻会把系统打崩的问题，但它会显著增加理解和维护成本。

对旅行垂类 Agent 来说，记忆系统本来就容易不断演进：

- 早期只想记偏好
- 后来想记约束、拒绝、行为信号
- 再后来想记整段旅行经验

如果新旧模型长期并存，就会产生几个实际问题：

- 开发者不容易判断某段逻辑到底服务于哪一代模型
- 测试和接口容易围绕兼容层打转，而不是围绕当前主路径优化
- 新功能可能被迫同时考虑两种结构，拖慢迭代速度

### c. 修复意见

最佳实践方案：

- 明确宣布 `MemoryItem/TripEpisode` 为唯一主模型
- 给 legacy 数据设计一次性迁移脚本，把所有用户数据迁移完成
- 迁移后删除 `UserMemory` 反向兼容读路径，把兼容成本从运行时挪到离线迁移阶段

投入最少收益最大方案：

- 先不删 legacy dataclass
- 但在代码层明确标注：哪些入口是兼容层，哪些入口是主路径
- 在文档中写清楚“聊天主路径只依赖 MemoryItem，UserMemory 只为迁移和少量旧接口保留”

大厂务实方案：

- 保留兼容读能力一个版本周期
- 新写路径全部只写 v2 envelope，不再写 legacy
- 新测试优先围绕 v2 写，legacy 只保留少量迁移回归测试
- 等线上数据完成平滑迁移后，再清理反向转换逻辑

## 5. `expires_at` 字段存在，但生命周期治理未落地

### a. 项目中相关功能/模块的具体实现

`MemoryItem` 在 `backend/memory/models.py` 中定义了：

- `expires_at: str | None = None`

这说明设计上是考虑过“记忆并非永久有效”的。

但从当前代码看：

- `MemoryPolicy.to_item(...)` 没有给它赋值
- `FileMemoryStore.list_items(...)` 没有过滤过期项
- `MemoryRetriever` 的三类检索也没有检查过期
- 也没有后台清理或懒清理逻辑把过期记忆标成 obsolete

因此，这个字段目前更像是 schema 预留位，而不是被系统真正使用的生命周期机制。

### b. 当前存在的具体问题

对旅行垂类 Agent 来说，很多记忆天然有时效性：

- 这次带娃出行，不等于以后永远亲子优先
- 一次性的签证材料要求、一次性的预算上限、一次性的特殊安排，不应该长期污染用户画像
- 某些 trip-scope 事实在 trip 结束后仍然挂在 active 状态，会给后续召回制造噪音

如果没有过期治理，记忆系统会逐渐出现两个问题：

- 该遗忘的没遗忘，造成上下文污染
- 系统越来越不敢自动保存，因为怕写进去就永远留下来

### c. 修复意见

最佳实践方案：

- 为不同 memory type 和 scope 定义 TTL 策略
- 例如 global 偏好默认不过期，trip 事实默认在 trip 完成后进入短期保留，再自动转 obsolete 或 archive
- 检索时统一过滤过期项，并通过后台任务做实际清理或状态变更

投入最少收益最大方案：

- 先不做复杂 TTL 引擎
- 只对 `scope == trip` 的记忆引入简单过期规则，例如回退重开新 trip 后，旧 trip item 自动 obsolete；trip 完成后也做一次批量过期处理
- 对 prompt 构造路径增加过期过滤，先解决召回污染问题

大厂务实方案：

- 不急着物理删除数据
- 先引入“逻辑过期”概念：过期项默认不参与召回，但保留在存储里做审计和回溯
- 等 TTL 策略稳定后，再补批量清理和归档能力

## 6. 两套 `MemoryMerger` 的问题真实存在，但不是“互相打架”

### a. 项目中相关功能/模块的具体实现

项目中确实存在两个名为 `MemoryMerger` 的类：

- `backend/memory/extraction.py:158` 的 `MemoryMerger`
- `backend/memory/policy.py:174` 的 `MemoryMerger`

但它们处理的是两代不同数据结构。

`extraction.py` 里的版本：

- 输入是 `UserMemory`
- 负责把 `preferences` 和 `rejections` 合并进旧画像结构

`policy.py` 里的版本：

- 输入是 `list[MemoryItem]` 和一个 `incoming MemoryItem`
- 负责在 v2 结构化记忆里处理去重、同值合并、列表合并、冲突转 `pending_conflict`

主链路里真正被调用的是后者，见 `backend/main.py:983-1004`。

### b. 当前存在的具体问题

因此，问题不在于“两个 merger 在当前主路径相互冲突”，而在于：

- 命名重复，容易误导开发者
- 旧 merger 已经不在主链路，却仍然保留在活代码目录里
- 阅读成本高，容易让人误判系统状态

对旅行垂类 Agent 团队来说，这种问题的真实危害不是线上 bug，而是研发协作效率下降：

- 新同学容易看不出哪条才是当前真实链路
- 做重构时容易误改到兼容层代码
- 文档和代码心智模型不一致

### c. 修复意见

最佳实践方案：

- 删除旧提取链路中不再使用的 merger，或迁移到明确的 `legacy_memory.py`
- 当前主链路只保留一个语义明确的合并器，例如 `MemoryItemMerger`
- 让类名直接体现处理对象，降低误读概率

投入最少收益最大方案：

- 先不删旧代码
- 但立即重命名两个类，至少把主链路那个改成 `MemoryItemMerger`
- 同时在旧类上加注释：仅供 legacy path 使用，不参与当前聊天主链路

大厂务实方案：

- 先保持行为不变，只做命名澄清和目录归位
- 等 legacy 路径完全退场后，再真正删除旧类

## 7. PII 防护不是空白，但离“强合规”还差很远

### a. 项目中相关功能/模块的具体实现

PII 相关逻辑主要集中在 `backend/memory/policy.py`。

目前已经实现的能力包括：

- 拒绝某些敏感 domain，例如 `payment`、`membership`
- 基于正则检测长数字串、分隔数字串、邮箱
- 检测护照号、身份证等关键词
- 在 `to_item(...)` 前对 `candidate.value`、`attributes`、`evidence` 做脱敏
- 对嵌套 dict/list/tuple/set 做递归脱敏

也就是说，当前系统不是完全不管 PII，而是已经做了一个规则式防护层。

### b. 当前存在的具体问题

真正的问题不是“完全没有防护”，而是“防护能力还停留在规则式基础版”。

对于旅行垂类 Agent，PII 风险比通用聊天更高，因为用户更容易提供：

- 护照、身份证、手机号、邮箱
- 航班号、订单号、会员号
- 家庭成员信息、儿童信息、紧急联系人

仅靠规则式正则会面临几个现实问题：

- 覆盖不全，容易漏掉格式变化后的敏感信息
- 对中文地址、人名、证件变体缺乏识别能力
- 很难判断“像号码但不是敏感数据”的误报场景

所以当前实现可以算“有基础安全意识”，但还不能算成熟的旅行行业敏感信息治理方案。

### c. 修复意见

最佳实践方案：

- 建立多层 PII 防护：规则检测 + 分类模型/LLM 审核 + 存储前 redaction
- 细分旅行场景专属敏感字段，如证件、预订号、会员号、联系方式、同行人身份信息
- 对 memory extraction、tool result、日志落盘三条链路统一执行脱敏

投入最少收益最大方案：

- 继续保留当前 regex 方案
- 重点补齐旅行场景专属关键词和格式，例如常见航司会员号、订单号、手机号、微信号、地址关键词
- 把脱敏覆盖面从 candidate 扩大到 episode lessons 和事件 reason_text 等易漏字段

大厂务实方案：

- 主路径仍以规则过滤为主，保证成本和稳定性
- 对高风险输入场景增加二次审查，例如用户消息命中证件/联系方式关键词时，禁止自动保存，只允许 pending
- 先把“绝不落盘高风险敏感信息”做到位，再考虑更复杂的智能识别

## 8. 文件存储的扩展性一般，但在当前阶段还没到必须推翻的程度

### a. 项目中相关功能/模块的具体实现

当前记忆存储由 `backend/memory/store.py` 中的 `FileMemoryStore` 实现。

它的主要策略是：

- 每个用户一个目录：`backend/data/users/{user_id}/`
- `memory.json` 存结构化 memory items envelope
- `memory_events.jsonl` 追加写记忆事件
- `trip_episodes.jsonl` 追加写 episode

写入时会：

- 用 `asyncio.Lock` 按 `user_id` 加锁
- 把 `memory.json` 全量读出、修改后写到临时文件
- 再用 `os.replace(...)` 原子替换

这说明它在“单进程、本地开发、轻量用户规模”场景下是可工作的，而且有最基本的并发保护和原子写意识。

### b. 当前存在的具体问题

问题也很明确：

- `memory.json` 是全量读写，不适合高频写入和大用户规模
- `trip_episodes.jsonl`、`memory_events.jsonl` 只追加不压缩，长期会膨胀
- `asyncio.Lock` 只在单进程事件循环内有效，多 worker 进程下不能提供跨进程一致性

对于旅行垂类 Agent，如果未来产品形态是：

- 用户反复回来规划多次旅行
- 每轮对话都在做候选提取
- 还要做管理后台、记忆中心、统计分析

那文件存储迟早会成为瓶颈。

但如果看当前项目阶段，它还不能算“立刻就要推翻重做”的致命问题，更准确的表述是：这是一个已知扩展性债务。

### c. 修复意见

最佳实践方案：

- 把结构化记忆、事件、episode 全部迁移到 SQLite 或 Postgres
- 至少为 `user_id + status + scope + trip_id + updated_at` 建检索索引
- 让 list/filter/update 不再依赖全量 JSON 反序列化

投入最少收益最大方案：

- 由于项目已经使用了 `aiosqlite`，优先把 `memory.json` 改为 SQLite 表
- `events` 和 `episodes` 先保留 JSONL 不动，减少迁移面
- 这样可以先把最频繁的读写主路径迁出文件系统，收益最大

大厂务实方案：

- 开发和 demo 环境保留 `FileMemoryStore`
- 生产环境引入 `SQLiteMemoryStore` 或数据库后端，通过配置切换
- 先保留双实现，降低切换风险；等线上稳定后再决定是否彻底删除文件后端

## 9. ID 截断和事件日志问题存在，但应当准确描述风险级别

### a. 项目中相关功能/模块的具体实现

`MemoryItem` 的 ID 由 `backend/memory/models.py:105-121` 的 `generate_memory_id(...)` 生成。

实现方式是：

- 根据 `user_id/type/domain/key/scope/trip_id/value` 拼出原始字符串
- 取 SHA1
- 截断前 16 位作为 ID

另外，事件日志 `memory_events.jsonl` 当前仍以追加写入为主：

- `backend/memory/store.py` 负责 `append_event(...)`
- API 层提供了追加事件接口，主业务链路也会写入事件，但当前仍没有形成完整读侧分析能力

### b. 当前存在的具体问题

对于 ID 方案，更准确的批评不是“马上会撞”，而是：

- 这是一个工程上偏便宜的实现
- 当前项目规模下大概率够用
- 但如果未来记忆量上升，64-bit 截断哈希不是理想长期方案

对于事件日志，更准确的问题不是“伪装成 event sourcing”，而是：

- 它更像审计日志或行为流水
- 但当前没有充分读侧利用，业务价值释放不足

### c. 修复意见

最佳实践方案：

- ID 改成真正的稳定主键策略，例如 UUIDv7 或数据库自增主键 + 业务唯一键组合
- 事件日志补齐查询、过滤、统计和回放用途，明确它到底是审计日志还是事件源

投入最少收益最大方案：

- 先保留当前 ID 逻辑，但增加冲突检测和监控
- 事件侧先补一个只读查询接口，至少支持按 user_id、event_type、session_id 查看

大厂务实方案：

- 不急着推翻现有事件结构
- 先在文档中明确 `memory_events.jsonl` 的角色是审计记录，不是主存储来源
- 等确实需要管理后台或回放分析时，再把它升级成正式分析数据源

## 总结

如果把这套记忆系统放到面试场景里，最准确的评价不是“全错”也不是“已经很成熟”，而是：

- 方向对：已经具备结构化记忆、候选提取、风险分类、待确认、trip/global scope、episode 归档这些关键构件
- 主短板清晰：episode 没进主召回闭环、召回缺少语义能力、提取输入过窄、生命周期治理未落地
- 新增但不改变结论的进展：记忆命中现在已经能被前端和 telemetry 看见，但“看得见命中”不等于“召回机制本身更强”
- 工程阶段真实：当前更像一套认真做过设计的记忆系统原型，而不是已经完成生产化的记忆基础设施

如果按投入产出比排序，最值得优先补的不是“大重构”，而是三件事：

1. 让 `TripEpisode` 真正参与召回
2. 扩大记忆提取输入，把决策行为纳入候选提取
3. 在当前规则召回上补一层轻量软匹配，减少重要记忆沉底

这三件事改完，记忆系统会更像“旅行经验系统”；否则它仍然更接近“用户偏好 KV 存储”。
