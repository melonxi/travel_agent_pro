# Travel Agent Pro 分阶段联调学习笔记

这份文档整理了上一轮关于“阶段一联调前需要先讲清楚什么”的 8 个大点，方便后续逐条学习和核对实现。

## 1. 初始化状态是什么

先理解“初始化状态”这个词。  
它指的是：用户刚打开页面、还没开始聊天时，系统内部已经准备好的一份初始旅行计划对象。这个对象不是用户输入出来的，而是后端先创建好的“空白草稿”。

前端一加载，就会主动创建这个空白草稿。代码在 `frontend/src/App.tsx`。这里的 `useEffect` 会自动做两件事：

1. `POST /api/sessions`
2. `GET /api/plan/{session_id}`

意思是，页面不是等你发第一句话才建会话，而是你一打开页面，它就先向后端申请一个新的 session，然后把这份 session 对应的 plan 拉回来。

后端收到 `POST /api/sessions` 后，会创建一份新的 `TravelPlanState`。入口在 `backend/main.py`，真正建对象的地方在 `backend/state/manager.py`。

它做了几件事：

1. 生成一个 `session_id`，格式像 `sess_xxxxxxxxxxxx`
2. 创建 `TravelPlanState(session_id=..., version=0)`
3. 建立这个 session 对应的目录
4. 立刻保存一次 plan
5. 给这个 session 绑定一套 agent 实例

这里“保存一次”很重要，因为它说明初始化不是只存在内存里，也会写到磁盘。

这份初始 plan 的字段基本全是空的。定义在 `backend/state/models.py`。你可以把它理解成一张还没填写的表单。核心字段是：

- `phase = 1`
- `destination = None`
- `dates = None`
- `budget = None`
- `accommodation = None`
- `daily_plans = []`
- `preferences = []`
- `constraints = []`
- `backtrack_history = []`

这就是系统为什么一开始会认为自己在“第一阶段”。因为它发现，连“去哪”都还没有。

这个空白状态会直接影响用户看到的界面。因为前端拿到的 plan 里没有预算、没有日程、没有活动点位，所以右边面板一开始基本是空的。代码在 `frontend/src/App.tsx` 以及这些组件里：

- `frontend/src/components/BudgetChart.tsx`
- `frontend/src/components/MapView.tsx`
- `frontend/src/components/Timeline.tsx`

所以刚打开页面时，用户能看到的是：

- 顶部阶段条在第 1 阶段
- 左边聊天框可输入
- 右边地图和时间线还没有实际内容

这个初始化状态不只是“数据”，还包括一套会话级运行环境。后端在创建 session 时，不只是建 plan，还会一起放进内存里的 `sessions` 字典，结构大概是：

- `plan`
- `messages`
- `agent`

也就是：

- `plan` 负责存旅行状态
- `messages` 负责存对话历史
- `agent` 负责这次会话后续的模型调用和工具调用

这一层在 `backend/main.py`。

为什么要这样设计。因为这样系统从一开始就有“状态容器”。后面用户每说一句话，不是在无状态地问答，而是在更新同一份 `plan`。这也是它能做“分阶段推进”“中途回退”“持续补全信息”的基础。

第 1 点的分步展开见：[初始化状态详解](./initial-state-7-points.md)。

## 2. 初始化 system prompt 长什么样

真正送给模型的 system message 由 3 层拼出来，代码在 `backend/context/manager.py`：

1. `soul.md` 身份约束  
路径：`backend/context/soul.md`
2. 当前阶段 prompt  
阶段一 prompt 在 `backend/phase/prompts.py`
3. 当前运行时状态和用户画像  
运行时状态在 `backend/context/manager.py`，用户画像在 `backend/memory/manager.py`

第一次对话时，实际拼出来大致就是这样：

```md
# SOUL.md — 旅行规划 Agent 身份
你是一个专业的旅行规划 Agent，帮助用户完成从模糊意愿到出发前查漏的全流程规划。
- 不替用户做情感决策
- 所有事实性信息必须来自工具返回
- 一次只问一个问题
...

## 当前阶段指引
你现在是旅行灵感顾问。用户可能只有模糊的想法（"想去海边""想放松"）。
你的任务是通过开放式提问帮用户具象化需求，不要急于给出目的地建议。
关注：出行动机、同行人、时间窗口、预算范围。
一次只问一个问题，保持耐心和热情。

## 当前规划状态
- 阶段：1

## 用户画像
暂无用户画像
```

这里有个点要注意：首次会话即使没有任何 memory，系统也会把“暂无用户画像”塞进去，不会省略这一段。见 `backend/memory/manager.py`。

## 3. 如何识别进入阶段一

阶段判定在 `backend/phase/router.py`：

- 没有 `destination`
- 并且没有 `preferences`
- 就是 phase 1

更具体一点：

- 没有目的地，且 `preferences` 为空 -> phase 1
- 有目的地，但没有日期 -> phase 3
- 有日期，没有住宿 -> phase 4
- 有住宿，但天数还没排满 -> phase 5
- 天数排满 -> phase 7

另外，用户消息会先走一遍“事实提取”，代码在 `backend/state/intake.py`。它现在只会自动提取这几类东西：

- 目的地
- 日期
- 预算

不会自动提取 `preferences`。这意味着：

- 用户发“最近想出去散散心，还没想好去哪” -> 仍然 phase 1
- 用户发“我想五一去东京玩5天，预算2万元” -> 会直接提取目的地、日期、预算，阶段直接跳到 4，不会真的停在 phase 1

所以，如果这轮联调是专门测阶段一，第一句不能带明确目的地和完整日期。适合的测试文案像：

- “最近想出去放空一下，但还没想好去哪”
- “想安排一个轻松点的短途旅行，同行人还没定”

## 4. Agent Loop 在这个过程中怎么运转

聊天入口在 `backend/main.py`，顺序是：

1. 取出当前 session 的 `plan / messages / agent`
2. 先检查用户是不是在回退阶段，比如“换个目的地”“改日期”“换住宿”
3. 如果不是回退，就从用户输入里抽取目的地、日期、预算
4. 根据 plan 重新推导 phase
5. 用 phase 对应的 prompt 组 system message
6. 把 system + 历史消息 + 当前用户消息，送进 `AgentLoop.run(...)`
7. `AgentLoop` 调模型，拿到流式文本和 tool call
8. 有 tool call 就执行工具，把结果作为 `tool` 消息再喂回模型
9. 没有 tool call 了，就结束本轮，并把最新 plan 通过 SSE 发给前端

主循环在 `backend/agent/loop.py`。它就是很标准的 think-act-observe：

- `llm.chat(...)`
- 拦截 `tool_call`
- `tool_engine.execute(...)`
- 把工具结果追加回消息
- 再进下一轮 LLM

hook 也挂在这里：

- `before_llm_call`：对话压缩
- `after_tool_call`：阶段推进、硬约束校验、质量评估

见 `backend/main.py`。

## 5. 阶段一实际加载了什么 prompt 和工具

阶段一 prompt 就是上面那段“旅行灵感顾问”，来源是 `backend/phase/prompts.py`。

阶段一真正发给模型的工具，运行时只有一个：`update_plan_state`。  
原因不在 `PHASE_TOOL_NAMES`，而在每个工具自己的 `phases` 声明，以及 `ToolEngine.get_tools_for_phase()`。代码在：

- `backend/tools/update_plan_state.py`
- `backend/tools/engine.py`
- `backend/agent/loop.py`

`update_plan_state` 在阶段一能写这些字段：

- `destination`
- `dates`
- `travelers`
- `budget`
- `accommodation`
- `preferences`
- `constraints`
- `destination_candidates`

也就是说，phase 1 的模型只能“记录信息”，不能查目的地、不能查天气、不能排日程。

这里还有个设计和运行时不完全一致的点：  
`backend/phase/prompts.py` 里虽然定义了 `PHASE_TOOL_NAMES` 和 `PHASE_CONTROL_MODE`，但运行时并没有用它们做真正分派，只在 `backend/phase/router.py` 留了 getter，测试里有用。真正控制工具暴露的是每个工具上的 `phases`。  
这意味着“分阶段”在运行时是生效的，但“control mode”这层现在还是设计稿状态，不是活代码。

## 6. trace 基建检查结果

如果按“有没有完整 agent trace”来判断，答案是：没有。

我查到的现状是：

- 没有 OpenTelemetry / Langfuse / telemetry 相关依赖  
见 `backend/pyproject.toml`
- 全仓库没有 trace/span/otel/langfuse 相关运行时代码
- 有一个 `HookManager`，但只是事件回调，不会把事件落成结构化 trace  
见 `backend/agent/hooks.py`
- 前端只能看到 SSE 三类事件：`text_delta`、`tool_call`、`state_update`  
见 `frontend/src/types/plan.ts`
- 聊天区只把 tool name 以一个 badge 显示出来，不显示 tool 参数、tool 返回值、phase transition  
见 `frontend/src/components/MessageBubble.tsx`
- 前端只有两个 `console.log`，一个打 phase update，一个打 plan update  
见 `frontend/src/App.tsx` 和 `frontend/src/components/ChatPanel.tsx`

磁盘上的运行痕迹也说明同样的问题：

- `backend/data/sessions` 已经有 134 个 session
- 每个 session 都有 `plan.json / snapshots / tool_results` 目录
- `snapshots` 目前只有 10 个文件，而且只在回退时生成  
见 `backend/state/manager.py`
- `tool_results` 文件数是 0，因为 `save_tool_result()` 根本没人调用  
定义在 `backend/state/manager.py`，全仓库引用只有测试

所以现在这套更像：

- 有 plan 持久化
- 有回退快照
- 有前端 SSE 表层事件

但没有：

- 每轮 trace id
- LLM 输入 / 输出记录
- tool arguments / tool result 持久化
- phase transition 结构化日志
- hook 执行日志
- 前端 trace 面板

## 7. 现在直接联调，还差什么

这一点需要修正。重新按项目真实运行方式检查后，之前“当前环境缺少 API key、前端不适合直接联调”的判断不准确。

问题出在检查方法。当时只看了当前 shell 进程里的环境变量，没有按后端的实际启动上下文去看 `backend/.env`。而这个项目的配置加载写在 `backend/config.py` 里，模块导入时会执行 `load_dotenv()`。如果从 `backend` 目录启动后端，`.env` 里的 key 会被读到。

重新核实后的结果是：

1. 后端 `.env` 是可用的  
在 `backend` 目录上下文里导入配置后，`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`DEFAULT_PROVIDER`、`OPENWEATHER_API_KEY`、`GOOGLE_MAPS_API_KEY` 都能读到。  
进一步用 `create_app()` 做了启动级验证，后端应用可以成功创建。

2. 当前配置能形成完整后端运行条件  
`load_config()` 读到的 provider 是 `openai`，model 是 `astron-code-latest`，天气和地图相关 key 也在。说明按当前你的本地环境，后端并不存在“因为没 key 无法起服务”的硬阻塞。

3. 前端至少已经通过了一次真实构建验证  
我重新执行了 `cd frontend && npm run build`，构建成功。这不能完全替代手动联调，但至少说明前端依赖和打包链路是通的。

4. 真正还需要注意的，不是“能不能跑”，而是“怎么跑得更一致”  
如果是手动联调，你之前已经把服务起起来，这说明前后端主链路大概率没有启动级问题。  
如果是自动化联调，还要注意一个细节：`frontend/vite.config.ts` 没固定端口，Vite 默认通常是 `5173`；而 `playwright.config.ts` 写的是 `5174`。这个不影响你手动启动服务，但会影响 Playwright 这类自动化脚本是否能直接连上前端。

## 8. 下一步最合适的动作

基于重新核实后的结果，下一步不需要再把重点放在“补 API key”或“怀疑前端起不来”上了，而是可以直接进入阶段一联调本身。

更合适的顺序是：

1. 继续沿用你现在这套能启动服务的方式起前后端  
这一步已经有你的手工验证，也有我这边的后端创建验证和前端构建验证，不需要再把它当成首要风险点。

2. 先做一次黑盒的阶段一联调  
测试输入要故意保持“模糊”，不要一上来就说明确目的地、完整日期和预算。  
例如：
   - “最近想出去散散心，但还没想好去哪”
   - “想安排一个轻松一点的短途旅行，还没定同行人”

3. 联调时重点看这些现象是不是符合阶段一设计
   - 顶部阶段条仍停在 1
   - 助手只问澄清问题，不直接排日程
   - 不会调用目的地搜索、天气、行程组装工具
   - 右侧地图和时间线保持空白
   - 最多只会出现 `update_plan_state`

4. 如果黑盒现象不对，再补 trace，而不是在联调前先假设服务起不来  
当前更现实的策略是先验证“阶段一行为对不对”，再决定 trace 要加到什么程度。  
如果要补 trace，最轻的一版还是给每一轮写一份 `trace.jsonl`，把这些事件记下来：

- `phase_before`
- `intake_updates`
- `system_prompt`
- `llm_text_delta`
- `tool_call`
- `tool_result`
- `phase_after`
- `state_update`

这样联调时不会只看聊天气泡猜模型在想什么。

所以，更新后的结论不是“先补环境再说”，而是：

- 你的本地环境已经基本具备联调条件
- 现在可以直接开始阶段一黑盒联调
- trace 仍然值得补，但它现在是调试增强项，不是启动阻塞项
