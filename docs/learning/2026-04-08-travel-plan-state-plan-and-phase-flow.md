# TravelPlanState、plan 与 update_plan_state：当前 Agent 如何在不同 phase 之间共享信息

这篇文档专门回答一个容易绕晕的问题：

> 当前项目里，Agent 到底是怎么把上一阶段得到的信息带到下一阶段的？

先给结论：

- 真正承载信息的核心对象是 `TravelPlanState`
- 运行时大家常说的 `plan`，就是某个会话对应的一份 `TravelPlanState` 实例
- `update_plan_state` 是唯一的通用状态写入口，负责把用户已经明确说过的信息，或 Agent 在规划过程中产出的结构化结果，写进这份 `plan`
- phase 切换时，并不是单纯依赖聊天记录，而是重新读取当前 `plan`，再把它注入新的 system prompt

所以，这个系统的信息传递方式，本质上不是“阶段 A 把一句话传给阶段 B”，而是：

```text
阶段 A 把结果写进 plan
阶段 B 再从同一个 plan 里把结果读出来
```

如果你先建立这个心智模型，后面的代码就会清楚很多。

---

## 0. 读这篇文档时，建议同时对照的代码文件

如果你想边看文档边看代码，优先打开这几个文件：

- `backend/state/models.py`
- `backend/state/manager.py`
- `backend/tools/update_plan_state.py`
- `backend/main.py`
- `backend/agent/loop.py`
- `backend/phase/router.py`
- `backend/context/manager.py`
- `backend/tools/engine.py`

它们分别负责：

- `TravelPlanState` 的定义
- `plan` 的创建和持久化
- `update_plan_state` 的实现
- session、agent、chat 入口的组装
- tool 执行后如何触发 phase 检查与上下文重建
- phase 和 phase3_step 的推断
- 如何把 `plan` 注入 system prompt
- 如何根据 phase 决定当前可用工具

---

## 1. 先分清两个概念：`TravelPlanState` 和 `plan`

这两个词很像，但不是一回事。

### 1.1 `TravelPlanState` 是什么

`TravelPlanState` 是一个 Python dataclass，定义在 `backend/state/models.py`。

你可以把它理解成：

> “当前这趟旅行规划的完整结构化状态模板”

它规定了系统里一份旅行计划应该有哪些字段，比如：

- 当前在哪个阶段 `phase`
- 目的地 `destination`
- 日期 `dates`
- 人数 `travelers`
- 预算 `budget`
- 偏好 `preferences`
- 约束 `constraints`
- phase 3 里生成的候选池、骨架方案、已选骨架
- phase 5 里生成的逐日行程 `daily_plans`
- 回退记录 `backtrack_history`

也就是说，`TravelPlanState` 是“类定义”。

### 1.2 `plan` 是什么

`plan` 不是另一个新类型，它只是一个变量名。

在运行时，`plan` 指向某个具体的 `TravelPlanState` 对象。这个对象只属于当前会话。

可以这样理解：

- `TravelPlanState` 像“表格模板”
- `plan` 像“已经填写到一半的这张表”

举个例子：

```python
plan = TravelPlanState(session_id="sess_abc123")
```

这时：

- `TravelPlanState` 是类
- `plan` 是这一类创建出来的实例

后面系统里不同模块不断读写的，都是这一个 `plan`。

---

## 2. `TravelPlanState` 里面到底装了什么

下面不是逐字段背定义，而是按“业务含义”来分组，便于入门理解。

### 2.1 会话和流程元数据

这部分字段用来标识“这是谁的计划”和“系统走到哪一步了”。

- `session_id`：当前会话 ID
- `phase`：当前大阶段，当前项目主要是 `1 / 3 / 5 / 7`
- `phase3_step`：phase 3 的子阶段，可能是 `brief / candidate / skeleton / lock`
- `created_at`、`last_updated`、`version`：创建时间、更新时间、版本号

其中最重要的是 `phase` 和 `phase3_step`。

它们不是“随便写的标签”，而是系统用来决定：

- 当前应该给模型什么 prompt
- 当前应该暴露哪些工具
- 当前系统应该如何解释已有状态

### 2.2 用户已经确认的事实

这类字段代表用户已经明确表达过，或者已经正式确认的信息。

- `destination`
- `dates`
- `travelers`
- `budget`
- `preferences`
- `constraints`
- `accommodation`
- `selected_transport`

这类字段有一个重要原则：

> 只有用户明确说过的信息，才应该写进这些字段。

比如：

- 用户说“就去东京吧” -> 可以写 `destination`
- 用户说“预算 2 万” -> 可以写 `budget`
- 用户说“我不想住太偏” -> 可以写 `preferences` 或 `constraints`

但如果只是 Agent 自己分析出来“东京可能更适合你”，还不能直接写成已确认事实。

### 2.3 Agent 在规划过程中产出的结构化结果

这类字段不是“用户自己拍板”的事实，而是 Agent 在工作过程中产出的中间成果。

- `trip_brief`
- `candidate_pool`
- `shortlist`
- `skeleton_plans`
- `transport_options`
- `accommodation_options`
- `risks`
- `alternatives`

这些字段的意义是：

> 把 Agent 的阶段性分析结果也结构化保存下来，而不是只放在自然语言回复里。

这样后面的 phase 才能复用前面的工作成果。

例如：

- phase 3 的 `brief` 子阶段会形成 `trip_brief`
- `candidate` 子阶段会形成 `candidate_pool` 和 `shortlist`
- `skeleton` 子阶段会形成 `skeleton_plans`
- `lock` 子阶段会形成交通和住宿候选

### 2.4 最终或接近最终的执行结果

- `daily_plans`

这个字段表示按天拆开的行程安排，是 phase 5 的核心产物。

### 2.5 回退相关信息

- `backtrack_history`

当用户说“换个目的地”“改日期”“这家酒店不要了”的时候，系统可能需要回退到更早阶段。回退不会静默发生，而是记录在这里。

---

## 3. `TravelPlanState` 是怎么进入系统的

这一步非常关键，因为它决定了后面为什么“同一个 plan 能被很多模块共享”。

### 3.1 创建 session 时创建 plan

入口在 `backend/main.py` 的 `/api/sessions`。

系统创建 session 时，会先调用 `StateManager.create_session()`，生成一个新的 `TravelPlanState` 实例。

简化后可以理解为：

```python
plan = await state_mgr.create_session()
```

这一步得到的 `plan`，就是当前会话的主状态对象。

### 3.2 把 plan 存进 session 容器

创建完之后，服务端会把它放进内存里的 `sessions` 字典：

```python
sessions[plan.session_id] = {
    "plan": plan,
    "messages": [],
    "agent": agent,
}
```

这里的意思是：

> 一个 session 持有一份 plan

从这一刻开始，这个 session 的后续所有聊天，都会围绕这同一个 `plan` 工作。

### 3.3 创建 agent 时，把同一个 plan 注入进去

`main.py` 里的 `_build_agent(plan, ...)` 会用这个 `plan` 创建当前 session 专属的 Agent。

在这个过程中，有两件事特别关键：

#### 第一件事：创建 `update_plan_state` 工具时绑定 `plan`

```python
tool_engine.register(make_update_plan_state_tool(plan))
```

这行代码的实际意义是：

> 生成一个“已经绑定到当前 plan 的 update_plan_state 工具”

也就是说，后面模型调用这个工具时，不需要再传 `session_id` 去查状态。这个工具天然就知道自己要修改哪一份 `plan`。

#### 第二件事：创建 `AgentLoop` 时也传入同一个 `plan`

```python
AgentLoop(
    ...,
    plan=plan,
    ...
)
```

这意味着：

- 工具层拿到的是这份 `plan`
- AgentLoop 拿到的也是这份 `plan`
- 后面 PhaseRouter、ContextManager 看到的，也还是这份 `plan`

这就是“共享状态”的来源。

---

## 4. `plan` 是怎么在系统里传递的

这里的关键，不是“plan 被复制了很多份”，而是：

> 系统里的多个模块持有的是同一个对象的引用

对入门程序员来说，可以把“引用”简单理解成：

> 大家手里拿的不是一张张复印件，而是同一个文件柜的钥匙

谁改了文件柜里的内容，其他人下次打开看到的也是修改后的结果。

### 4.1 一条完整的传递链

下面是一条真实的数据流：

```text
create_session()
  -> 生成 TravelPlanState 实例
  -> 放进 sessions[session_id]["plan"]
  -> _build_agent(plan)
      -> update_plan_state 工具绑定 plan
      -> AgentLoop 绑定 plan

POST /api/chat/{session_id}
  -> 取出 session["plan"]
  -> 用 plan 构建 system message
  -> AgentLoop.run(messages, phase=plan.phase)
  -> LLM 调用 update_plan_state(...)
  -> update_plan_state 直接修改 plan
  -> PhaseRouter 根据 plan 判断是否切 phase
  -> ContextManager 再把最新的 plan 注入下一轮上下文
```

### 4.2 为什么修改会立刻生效

因为这里没有“写完工具结果后，再人工同步回 plan”的额外步骤。

`update_plan_state` 修改的，就是系统当前正在使用的那份 `plan` 本体。

所以当工具写完后：

- `AgentLoop` 马上能看到新字段
- `PhaseRouter` 马上能根据新字段判断 phase
- `ContextManager` 马上能把新字段写进 system message

这就是当前架构的一个核心特点：

> 状态是 in-place 修改的，不是先返回一份新对象，再另行替换

---

## 5. `update_plan_state` 到底做了什么

很多人第一次看这个项目，会以为它只是“记个日志的工具”。其实不是。

`update_plan_state` 是当前系统里最关键的状态写工具。

你可以把它理解成：

> Agent 写入结构化状态的统一入口

### 5.1 它写的不是聊天记录，而是业务状态

例如它可以更新：

- `destination`
- `dates`
- `travelers`
- `budget`
- `trip_brief`
- `candidate_pool`
- `shortlist`
- `skeleton_plans`
- `selected_skeleton_id`
- `accommodation`
- `daily_plans`

所以它不是简单地说“刚才发生了一次工具调用”，而是直接改变当前旅行计划的事实层。

### 5.2 它既能写“用户事实”，也能写“Agent 产物”

这是初学者最容易忽略的一点。

`update_plan_state` 写入的内容主要有两种：

#### 第一种：用户明确表达的信息

例如：

- 用户拍板目的地
- 用户给出预算
- 用户给出日期
- 用户给出人数
- 用户明确说了住宿区域

#### 第二种：Agent 在阶段工作里生成的结构化结果

例如：

- 旅行画像 `trip_brief`
- 候选池 `candidate_pool`
- shortlist
- 骨架方案 `skeleton_plans`
- 风险和替代方案
- 逐日行程 `daily_plans`

这就是为什么后续 phase 能继续工作。

如果这些内容只存在于聊天回复里，而没有写进 `plan`，后面的 phase 就很难可靠复用。

### 5.3 它是一个绑定到当前 plan 的闭包工具

这一点值得单独说明。

`update_plan_state` 不是全局单例函数直接操作数据库，而是通过：

```python
make_update_plan_state_tool(plan)
```

生成出来的。

也就是说，这个工具在创建时就已经“记住”了当前会话的 `plan`。

因此模型调用：

```python
update_plan_state(field="destination", value="东京")
```

它会直接改当前这份 `plan.destination`，而不是去别处再查一次目标对象。

---

## 6. phase 之间的信息到底是怎么传递的

这部分是整篇文档最重要的内容。

先说一句最核心的话：

> phase 之间传递信息，靠的是共享的 `plan`，不是单纯依赖历史聊天文本。

下面按顺序展开。

### 6.1 phase A 先把结果写进 plan

比如当前在 phase 1，用户说：

> 就去东京吧，五一出发，预算两万，两个人

理想情况下，Agent 会连续调用几次 `update_plan_state`：

- 写 `destination`
- 写 `dates`
- 写 `budget`
- 写 `travelers`

如果进入 phase 3，又生成了旅行画像、候选池、骨架方案，也应该继续写进：

- `trip_brief`
- `candidate_pool`
- `shortlist`
- `skeleton_plans`

这一步做完之后，信息就不再只是“聊天里提到过”，而是变成了“当前 plan 的正式状态”。

### 6.2 `PhaseRouter` 根据 plan 推断应该进入哪个 phase

`PhaseRouter` 的思路不是“模型说自己现在进入下一阶段”，而是：

> 先看 `plan` 里现在已经具备哪些字段，再反推当前应该处于哪个 phase

当前规则可以粗略理解成：

- 没有 `destination` -> phase 1
- 有 `destination`，但还没补齐 `dates / selected_skeleton_id / accommodation` -> phase 3
- 上面这些有了，但 `daily_plans` 还没补齐 -> phase 5
- `daily_plans` 补齐 -> phase 7

phase 3 里还有 `phase3_step`，也是根据 `trip_brief`、`candidate_pool`、`shortlist`、`skeleton_plans`、`selected_skeleton_id`、`accommodation` 自动推导的。

所以系统的阶段推进，本质上是：

```text
先写状态
再由状态推断阶段
```

### 6.3 一旦 phase 变化，AgentLoop 会重建消息上下文

这是很多人第一次看代码时没注意到的关键点。

`AgentLoop` 在一批工具调用执行完后，会检查 phase 有没有变化。

如果 phase 变化了，它不会傻傻地继续拿旧 prompt、旧工具集跑下去，而是会调用：

```python
_rebuild_messages_for_phase_change(...)
```

它会做三件事：

#### 第一件事：生成新 phase 的 system message

新的 system message 会使用：

- 新 phase 对应的 prompt
- 当前最新的 `plan`
- 当前 phase 可用的工具列表
- 当前用户画像

#### 第二件事：把旧阶段内容压缩成摘要

旧阶段并不会被完全丢掉，而是会通过 `compress_for_transition()` 压缩成一段摘要，带到新阶段。

不过注意，这个摘要只是辅助信息。

真正可靠的核心信息，仍然是已经写进 `plan` 的结构化字段。

#### 第三件事：保留当前用户消息

系统还会把当前这条用户消息保留下来，作为新阶段继续理解用户意图的锚点。

所以 phase 切换后的消息大致会变成：

```text
system: 你现在处于新阶段，这是当前 plan，这是当前工具
assistant: 这是上一阶段的摘要
user: 用户刚才这条消息
```

### 6.4 新 phase 再从 plan 中读取所需信息

新的 system message 是通过 `ContextManager.build_system_message(plan, ...)` 生成的。

这个函数会把 `plan` 里的关键信息写进“当前规划状态”部分。

例如：

- 当前阶段
- phase 3 子阶段
- 当前可用工具
- 目的地
- 日期
- 人数
- 预算
- 旅行画像
- 候选池
- shortlist
- 已选骨架
- 住宿
- 已规划天数

因此，新 phase 并不是在猜“上一阶段可能做了什么”，而是在读取一份结构化的当前状态快照。

这就是所谓的“通过 TravelPlanState 共享信息”。

### 6.5 `plan` 还会影响当前能用哪些工具

`plan` 不只是“保存信息的地方”，它还会影响 Agent 当前的工作能力。

原因是工具注入并不是固定写死的，而是由 `ToolEngine.get_tools_for_phase(...)` 根据当前 phase 过滤出来的。

尤其在 phase 3，系统还会继续读取 `plan.phase3_step`，决定当前是：

- `brief`
- `candidate`
- `skeleton`
- `lock`

不同子阶段能看到的工具也不同。

所以 `plan` 一旦更新，带来的变化不只是“上下文变了”，还可能是：

- prompt 变了
- 当前工具集变了
- Agent 下一步能做的事也变了

这也是为什么说 `plan` 是整个系统的共享真相，而不是一个普通数据对象。

---

## 7. 一个完整例子：从 phase 1 到 phase 5

下面用一个尽量贴近真实流程的例子，把整条链路串起来。

### 第一步：用户确认目的地和基础信息

用户说：

> 就去东京吧，5 月 1 日到 5 月 5 日，两个人，预算 2 万

Agent 调用 `update_plan_state` 后，`plan` 中会有：

- `destination = 东京`
- `dates = 2026-05-01 ~ 2026-05-05`
- `travelers = 2`
- `budget = 20000`

此时 `PhaseRouter` 看到：

- 目的地有了
- 但骨架和住宿还没确定

所以系统会进入 phase 3。

### 第二步：phase 3 生成旅行画像和候选结构

Agent 在 phase 3 中形成：

- `trip_brief`
- `candidate_pool`
- `shortlist`

这些内容都通过 `update_plan_state` 写入 `plan`。

于是 phase 3 的后续回合，不需要重新从长聊天记录里猜用户需求，可以直接从 `plan.trip_brief`、`plan.shortlist` 继续推理。

### 第三步：phase 3 生成骨架并锁定骨架

Agent 继续生成：

- `skeleton_plans`
- `selected_skeleton_id`

这一步写入后，系统知道用户已经选定哪套行程骨架了。

### 第四步：phase 3 锁定住宿

Agent 或用户确认住宿后，写入：

- `accommodation`

此时 `PhaseRouter` 看到：

- `dates` 已有
- `selected_skeleton_id` 已有
- `accommodation` 已有

于是系统进入 phase 5。

### 第五步：phase 5 细化每日行程

到了 phase 5，system message 会自动注入：

- 当前目的地
- 日期范围
- 旅行画像
- 已选骨架方案
- 住宿信息

所以 phase 5 不需要重新问“你去哪”“几天”“住哪”“你想走什么节奏”。

这些信息已经通过 `plan` 传过来了。

然后 phase 5 继续把结果写入：

- `daily_plans`

当 `daily_plans` 的天数补齐后，系统再进入 phase 7。

---

## 8. 为什么这个设计比“只靠聊天记录”更稳

如果系统只靠聊天记录传递信息，会有几个明显问题。

### 8.1 自然语言不稳定

聊天记录里有大量解释、寒暄、比较、建议。

如果后续 phase 只能从这些文本里重新理解用户需求，就容易出现：

- 漏掉关键信息
- 误把建议当成已确认事实
- 把旧需求和新需求混在一起

### 8.2 对话压缩后会丢细节

当前系统会在消息太长时做压缩。

如果核心信息只存在聊天文本里，一旦压缩，就有可能丢掉关键约束。

而结构化的 `plan` 不会因为消息压缩而消失。它会在下一轮继续被注入 system message。

### 8.3 phase 切换需要稳定的输入

不同 phase 的任务重点完全不同。

例如：

- phase 1 更关注收敛目的地
- phase 3 更关注规划框架
- phase 5 更关注逐日编排

如果进入新 phase 时没有一个稳定的结构化状态对象，模型就很难“站在前面结果的基础上继续工作”。

---

## 9. 回退时又是怎么处理的

回退也是围绕 `plan` 完成的。

当用户要求推翻之前的决定时，系统会通过 `update_plan_state(field="backtrack", value=...)` 或 API backtrack 入口触发回退。

回退不是简单地改一下 `phase`，而是：

1. 记录一条 `BacktrackEvent`
2. 清空目标阶段之后产生的下游字段
3. 把 `plan.phase` 改回更早阶段

例如从 phase 5 回退到 phase 3 时，系统会清掉：

- `daily_plans`
- 以及从 phase 3 开始生成的那批下游产物字段

但会保留：

- 已确认的目的地
- 偏好
- 约束

这样回退之后，系统仍然是在“同一个 plan”上继续工作，只是把不再可信的下游产物清掉了。

---

## 10. 持久化：为什么刷新后 plan 不会丢

`plan` 平时在内存里工作，但每轮聊天结束后，系统还会调用 `StateManager.save(plan)` 把它序列化到磁盘。

这意味着：

- 内存中的 `plan` 负责本轮实时共享
- 磁盘上的 `plan.json` 负责跨请求、跨进程保底

因此从架构上看，`plan` 既是：

- 运行时共享状态
- 也是持久化状态

---

## 11. 你可以记住的最终心智模型

如果你看完整篇文档，只想记住最重要的 5 句话，记下面这些就够了：

1. `TravelPlanState` 是旅行规划的结构化状态模型。
2. `plan` 是某个 session 当前正在使用的那一份 `TravelPlanState` 实例。
3. `update_plan_state` 负责把事实和阶段产物写进这份 `plan`。
4. 不同 phase 之间共享信息，靠的是同一个 `plan`，不是只靠聊天记录。
5. phase 切换时，系统会基于最新 `plan` 重建 prompt、工具和上下文，所以新阶段能继续接着旧阶段工作。

---

## 12. 一句话总结

当前 Agent 系统的核心不是“让模型记住之前说过什么”，而是“让模型和工具共同维护一份共享的 `plan`”。`update_plan_state` 负责写，`PhaseRouter` 负责根据它推断阶段，`ContextManager` 负责把它重新注入下一阶段，这就是整个 phase 间信息传递机制的主轴。
