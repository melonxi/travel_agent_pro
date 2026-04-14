# 初始化状态详解

这份小文档继续解释“初始化状态是什么”，但视角改成以 `agent loop` 为核心。换句话说，不再优先看页面一开始长什么样，而是看：第一轮 loop 真正开始前，系统已经准备了哪些东西，loop 会拿这些东西怎么运转。

## 1. 初始化状态，首先是为第一轮 loop 预备输入

从 loop 视角看，初始化状态不是一份静态表单，而是“第一轮 `AgentLoop.run()` 还没开始时，系统已经准备好的最小运行现场”。  
这个现场至少要有三类东西：

- 一份当前 `plan`
- 一段当前 `messages`
- 一个可运行的 `agent`

没有这三样，loop 根本进不去。

## 2. 前端加载页面，其实是在替 loop 申请一个运行现场

前端在 `frontend/src/App.tsx` 里一加载就会先调：

1. `POST /api/sessions`
2. `GET /api/plan/{session_id}`

表面上看，这是在“创建会话并拉初始数据”；从 loop 视角看，这一步其实是在向后端申请一个新的运行上下文。  
因为后端不仅会生成 `session_id`，还会把之后每轮 loop 需要用到的 `plan / messages / agent` 都挂到这个 session 上。

## 3. 后端创建 session，本质上是在把 loop 的执行器装配起来

`POST /api/sessions` 的处理在 `backend/main.py`。这里做的不只是建一条数据，而是把 loop 运行所需的核心对象一次性装配好：

1. `state_mgr.create_session()` 创建 `TravelPlanState`
2. `_build_agent(plan)` 创建当前 session 专属的 agent
3. `sessions[session_id] = { "plan": ..., "messages": [], "agent": ... }`

这里最关键的是 `_build_agent(plan)`。  
它会把这些部件接进 loop：

- LLM provider
- ToolEngine
- HookManager
- 与当前 `plan` 绑定的工具

所以初始化阶段并不是“先有一个抽象 loop，后面再慢慢配”，而是创建 session 时就已经把 loop 的执行骨架配好了。

## 4. 初始 plan 的空白字段，决定了第一轮 loop 的语义起点

`TravelPlanState` 初始值里最重要的不是“字段为空”这件事本身，而是这些空值会如何影响第一轮 loop 的行为。

初始核心字段大致是：

- `phase = 1`
- `destination = None`
- `dates = None`
- `budget = None`
- `accommodation = None`
- `daily_plans = []`
- `preferences = []`

这意味着第一轮 loop 拿到的运行时状态是：  
“还没有目的地，也没有日期和住宿，因此当前不是规划行程的时候，而是先收集需求的时候。”

也就是说，空白 plan 不是被动数据，它直接定义了 loop 此刻应该扮演什么角色。

## 5. 第一轮 loop 真正开始前，system message 也已经由初始化状态决定了

当用户发来第一句话，后端在 `backend/main.py` 里不会立刻把消息原样扔给模型，而是会先做这些事：

1. 从用户输入里提取已有事实
2. 用当前 `plan` 推导 phase
3. 读取这个 phase 对应的 prompt
4. 用 `ContextManager` 组装 system message

`backend/context/manager.py` 会把三层内容拼起来：

- `soul.md` 身份约束
- 当前阶段 prompt
- 当前运行状态和用户画像

所以 loop 第一轮看到的，不只是“用户说了一句话”，而是“用户消息 + 当前阶段说明 + 当前 plan 状态 + 用户画像”的组合上下文。

## 6. 初始化状态还决定了第一轮 loop 能调用哪些工具

`AgentLoop.run()` 开始时，会先根据 `phase` 取当前可用工具：

- `tools = self.tool_engine.get_tools_for_phase(phase)`

这一步很重要。因为它说明初始化状态不仅决定“模型应该怎么想”，还决定“模型被允许怎么做”。

在 phase 1 下，当前项目实际暴露给 loop 的核心工具是 `update_plan_state`。  
所以第一轮 loop 的能力边界更像：

- 可以记录信息
- 可以补充偏好和约束
- 不能直接排详细行程
- 不应该进入后面的规划阶段工具链

这也是为什么初始化状态本质上是 loop 的行为约束条件，而不只是前端显示内容。

## 7. 整个初始化设计，目的就是让 loop 从第一轮开始就处在一个可控的语义环境里

如果没有这套初始化设计，第一轮 loop 很容易变成“用户来一句，模型自由发挥一句”。  
但当前项目不是这么做的。它先把以下内容准备好：

- 当前 session 的状态容器
- 当前 plan
- 当前消息列表
- 当前 agent 实例
- 当前阶段 prompt
- 当前可用工具集合

这样第一轮 loop 一启动，就不是在真空里工作，而是在一个已经定义好角色、边界和目标的环境里工作。  
这也是为什么这个项目后面能继续做阶段推进、回退、工具校验和上下文压缩，因为这些能力都不是脱离 loop 独立存在的，而是建立在初始化阶段已经把 loop 的运行现场准备好了。
