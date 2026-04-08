# Phase 3 提示词问题清单

整理时间：2026-04-08

本文档只记录当前 `backend/phase/prompts.py` 中 phase 3 提示词相关的问题，不包含修复实现。重点不是评价文案是否“好听”，而是确认它是否和运行时机制、状态模型、工具暴露、前端消费方式真正对齐。

涉及文件：
- `backend/phase/prompts.py`
- `backend/context/manager.py`
- `backend/phase/router.py`
- `backend/state/models.py`
- `backend/tools/engine.py`
- `backend/tools/update_plan_state.py`
- `backend/agent/loop.py`
- `frontend/src/components/Phase3Workbench.tsx`
- `frontend/src/types/plan.ts`

## 结论概览

phase 3 当前最大的问题不是提示词长度，而是它把自己写成了一个“严格依赖结构化状态的子阶段状态机”，但运行时并没有把这些结构化状态完整地喂回给模型，也没有把关键字段 schema 钉死。结果是：

- 提示词要求模型严格按状态推进，但模型常常看不见前一轮真正写入了什么。
- 提示词要求模型写结构化产物，但很多字段没有稳定 schema，只能依赖模型自由发挥。
- 提示词把 `phase3_step` 当作主控制杆，但实现里它本质上是衍生状态。
- 系统不得不依赖 repair 机制，在模型已经偏航之后再补救。

---

## 1. Phase 3 看不到自己真正依赖的状态内容

严重程度：高

问题描述：

phase 3 提示词要求模型“如果当前规划状态里已经有 `phase3_step`，严格按该子阶段推进”，还要求基于已有的 `trip_brief`、`candidate_pool`、`shortlist`、`skeleton_plans` 等产物继续推进。

但当前 phase 3 的运行时上下文只注入“数量”和少量摘要，不注入这些字段的完整内容：

- `trip_brief` 在 phase 3 中只显示“已生成旅行画像：N 项”
- `candidate_pool` / `shortlist` 只显示数量
- `skeleton_plans` 只显示“骨架方案：N 套”
- 即使已有 `selected_skeleton_id`，phase 3 也看不到被选中骨架的具体内容
- `preferences` / `constraints` 的具体内容也不会在 phase 3 中展示

对应代码：

- `backend/phase/prompts.py`
- `backend/context/manager.py`
- `backend/tests/test_context_manager.py`

关键位置：

- `backend/phase/prompts.py` 中要求严格按子阶段推进，并依赖已有产物继续推进
- `backend/context/manager.py` 在 `plan.phase < 5` 时，对 `trip_brief` 和 `skeleton_plans` 只输出计数
- `backend/tests/test_context_manager.py` 明确要求 phase 3 仍然保持 count-only 格式

影响：

- 跨轮对话时，模型可能只知道“已经有 2 套骨架”，却不知道骨架具体内容
- 恢复会话或 phase rebuild 后，模型可能重复生成已存在的候选或骨架
- 提示词要求“沿着现有结构往下推”，但模型拿不到足够结构，容易发生漂移

这不是文案问题，而是 prompt contract 和 runtime context 不一致。

---

## 2. `skeleton_plans` 缺少稳定 schema，`selected_skeleton_id` 存在悬空风险

严重程度：高

问题描述：

提示词要求模型在 skeleton 子阶段写入 `skeleton_plans`，用户选中后再写 `selected_skeleton_id`。但提示词没有把骨架结构的最低要求钉死，尤其没有强制：

- 每套骨架必须有稳定 `id`
- `id` 必须是后续选择时唯一引用的主键
- `days`、`summary`、`tradeoffs` 等字段应采用什么最小结构

当前下游逻辑存在三套不同的容错规则：

- 后端恢复已选骨架时优先按 `id` 匹配，再退化到 `name`
- 前端展示骨架卡片时会继续退化到 `title` / `name` / `style`
- 提示词正文里又倾向于让模型输出“方案 A/B/C”“轻松版/平衡版/高密度版”

对应代码：

- `backend/phase/prompts.py`
- `backend/context/manager.py`
- `frontend/src/components/Phase3Workbench.tsx`
- `frontend/src/types/plan.ts`

影响：

- 前端可能“看起来能显示”，但后端不一定能稳定找回被选中的那套骨架
- `selected_skeleton_id` 可能写成 `A`、`plan_A`、`轻松版`、`方案A` 这类不稳定值
- phase 5 如果依赖 `selected_skeleton_id` 找骨架，可能拿不到正确方案

这是一个典型的“UI 勉强可展示，但状态不可可靠引用”的问题。

---

## 3. 提示词默许“先说人话，下一轮再补状态”，导致 repair 机制成为常态

严重程度：高

问题描述：

phase 3 prompt 里有一句：

- 如果已经在自然语言里给出了 brief / 候选池 / shortlist / 骨架方案 / 锁定建议，就必须在同一轮或紧接着的下一轮先调用 `update_plan_state`

这等于承认“先输出自然语言，再补结构化状态”是被允许的。

而系统实现已经证明这会频繁失效，所以 `backend/agent/loop.py` 专门增加了 phase 3 repair 逻辑，在模型只输出文本、不写 `trip_brief` / `candidate_pool` / `skeleton_plans` / lock 结构化产物时，强行提醒再来一轮。

对应代码：

- `backend/phase/prompts.py`
- `backend/agent/loop.py`

影响：

- 多消耗一个或多个 round
- 增加重复解释和状态补写的概率
- 让 phase 3 的稳定性依赖 repair，而不是依赖第一轮正确行为

从设计上看，这说明 prompt 没有把“结构化状态优先于自然语言展开”立住。

---

## 4. `phase3_step` 在提示词中被过度神化，但实现里它只是衍生状态

严重程度：中高

问题描述：

phase 3 prompt 把 `phase3_step` 写成主控制逻辑：

- brief 完成后，手动写 `phase3_step = candidate`
- shortlist 足够后，手动写 `phase3_step = skeleton`
- 选中骨架后，手动写 `phase3_step = lock`

但在实现里，`phase3_step` 会被 `PhaseRouter.sync_phase_state()` 根据以下字段重新推断：

- `dates`
- `trip_brief`
- `candidate_pool`
- `shortlist`
- `skeleton_plans`
- `selected_skeleton_id`
- `accommodation`

也就是说，真正驱动子阶段推进的并不是“写了哪个 step”，而是“关键产物是否已经存在”。

对应代码：

- `backend/phase/prompts.py`
- `backend/phase/router.py`
- `backend/state/models.py`

影响：

- 模型可能误以为“先改 `phase3_step` 就算完成推进”
- 但 router 下一次同步时，仍会按真实状态把 step 改回去
- 这会制造一种假象：模型“自认为进入下一步”，系统却并没有真正接受

这里的问题不是 `phase3_step` 不该存在，而是 prompt 把它写成了主因，代码却把它当结果。

---

## 5. `trip_brief` 和候选/骨架字段的 key 太自由，下游消费不稳定

严重程度：中高

问题描述：

提示词要求模型尽快把旅行画像写入 `trip_brief`，但没有给出稳定字段约定。当前系统对 `trip_brief`、`candidate_pool`、`skeleton_plans` 的消费都是“半结构化 + 容错式”的：

- 前端 `Phase3Workbench` 只会优先识别一部分预设 key，例如 `goal`、`pace`、`departure_city`、`must_do`、`avoid`
- `frontend/src/types/plan.ts` 允许大量 `[key: string]: unknown`
- `update_plan_state` 对这些字段基本只是 merge / append，不做 schema 校验

同时，phase 3 prompt 把“出发地”列为关键信息，但状态模型里没有一等的 `origin` / `departure_city` 字段，只能塞进 `trip_brief`。而航班 / 火车工具在 lock 阶段又要求明确 `origin` 参数。

对应代码：

- `backend/phase/prompts.py`
- `backend/tools/update_plan_state.py`
- `frontend/src/components/Phase3Workbench.tsx`
- `backend/tools/search_flights.py`
- `backend/tools/search_trains.py`

影响：

- 模型可能写 `from_city`、`departure`、`depart_from`、`出发城市` 等任意字段名
- 前端有可能显示不出来
- lock 阶段无法稳定复用 brief 中记录的出发地
- 结构化状态表面存在，实际难以作为后续工具输入

这个问题会直接降低“phase 3 产物可复用性”。

---

## 6. prompt 强调“严格按子阶段推进”，但工具边界与上下文缺口会迫使模型跳步

严重程度：中

问题描述：

prompt 希望模型按 `brief -> candidate -> skeleton -> lock` 顺序线性推进，但系统存在两个现实情况：

1. router 会在有 `dates + trip_brief` 后自动把 step 推到 `candidate`
2. 对成熟目的地，prompt 又鼓励模型“可以先基于常识产出第一版候选池，再少量搜索补验证”

这本身不是错，但它意味着 phase 3 的真实执行更接近“产物驱动的 opportunistic flow”，而不是“严格线性问答流程”。

当前 prompt 一边强调严格阶段纪律，一边又鼓励在信息足够时直接写候选、写骨架、少搜甚至先产出，这会让模型难以判断何时该停、何时该推进。

对应代码：

- `backend/phase/prompts.py`
- `backend/state/models.py`
- `backend/tests/test_agent_loop.py`

影响：

- 模型可能过度拘泥于“当前 step 名称”，不敢利用现成信息直接写结构化产物
- 也可能反过来，借“信息足够”为由跳过该确认的约束

这属于 prompt 内部执行哲学不统一。

---

## 7. lock 子阶段的“完成标志”和真实 phase 切换条件不完全一致

严重程度：中

问题描述：

prompt 在 lock 子阶段定义的完成标志是：

- `dates` 已确认
- 已有 `selected_skeleton_id`
- `accommodation` 已确认
- 关键风险已被指出或给出备选

但真实 phase 3 -> 5 的切换条件在 `PhaseRouter.infer_phase()` 里只有：

- `dates`
- `selected_skeleton_id`
- `accommodation`

`risks` / `alternatives` 并不参与 phase 切换。

对应代码：

- `backend/phase/prompts.py`
- `backend/phase/router.py`

影响：

- prompt 会让模型觉得“风险和备选没写就还不算完成”
- 但系统可能已经因为另外三个条件齐备而切到 phase 5
- 这会让“提示词定义的完成”和“系统定义的完成”出现偏差

这类偏差很容易造成 phase 切换后的困惑。

---

## 8. 当前 phase 3 更像“右侧工作台填充器”，而不是“真正面向规划决策的协议”

严重程度：中

问题描述：

prompt 多次强调：

- 不要只在正文里列候选而不写状态
- 右侧工作台依赖这些结构化字段展示

这句话没有错，但它暴露了一个倾向：phase 3 prompt 目前更像是在驱动模型填充 UI 所需字段，而不是先定义一套后续阶段、工具、上下文压缩都能稳定复用的 planning protocol。

对应代码：

- `backend/phase/prompts.py`
- `frontend/src/components/Phase3Workbench.tsx`

影响：

- 一旦字段只是“能展示”，而不是“能可靠被引用和复用”，后续阶段仍会出问题
- UI 可视化需求会反向影响状态设计，导致 schema 越来越松

这不是 bug，但会长期拖累 phase 3 的工程质量。

---

## 已确认问题与推断风险的边界

以下问题是从代码中直接确认的：

- phase 3 runtime context 只注入计数，不注入完整 `trip_brief` / `skeleton_plans`
- `phase3_step` 会被 router 自动重算
- phase 3 repair 机制已经存在，且专门修复“只说不写状态”
- lock 完成标志和真实 phase 切换条件不完全一致

以下属于基于当前代码路径的高概率推断风险：

- `selected_skeleton_id` 可能因为 schema 松散而悬空
- `trip_brief` 中自由命名的字段会影响前端展示与后续工具复用
- phase 3 在跨轮、恢复、重建消息时更容易重复生成或漂移

---

## 建议的后续处理顺序

如果后面逐条修，建议优先级如下：

1. 先定义 phase 3 产物的最小稳定 schema，尤其是 `trip_brief`、`candidate_pool`、`skeleton_plans`
2. 明确 `selected_skeleton_id` 必须绑定哪一个字段，禁止再依赖 `title` / `方案A` 这类软引用
3. 决定 phase 3 是否继续只注入 count-only；如果不改，prompt 里就不能再假设模型“看得到已有细节”
4. 弱化 `phase3_step` 的手动推进语义，改成“产物形成后可更新 step，但以真实状态为准”
5. 重新统一 lock 完成标志与 phase 切换条件
6. 最后再润色提示词语言风格

如果只改文案，不改 schema 和 runtime injection，这些问题大多还会复现。
