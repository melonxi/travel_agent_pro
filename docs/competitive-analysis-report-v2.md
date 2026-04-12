# Travel Agent Pro 竞争力深度分析报告 v2

> **目标**：基于项目代码深度审查 + 2025-2026 行业趋势调研，给出从 Agent 应用开发工程师简历中脱颖而出的完整策略。
>
> **与 v1 报告的核心区别**：v1 报告的核心判断"补证据链"方向正确，但低估了项目已有的工程深度，遗漏了关键叙事维度（Harness Engineering、Context Engineering、符号-LLM 混合决策、记忆作为独立学科），对优先级排序和简历包装方式有明显不同判断。

---

## 0. 执行摘要

Travel Agent Pro 不是"又一个旅行攻略聊天机器人"。经过对 25,000+ 行后端代码的逐模块审查，这个项目在 portfolio 层面已经处于顶尖水平：

- **Agent Loop（718 行）带三层自我修复**：状态同步检测、行程完整性修复、冗余操作跳过——不是简单的 while 循环。
- **24+ 工具接入真实 API**（Amadeus、Google Maps、FlyAI、Tavily），双源搜索 + 失败降级——不是 mock。
- **结构化记忆系统**实现了 2026 年行业刚刚定义为"独立工程学科"的完整架构：分层存储、后台提取、policy 风险分类、多维 PII 脱敏、pending 确认、trip_id 隔离。
- **Context Engineering 的优秀实践**：阶段级工具门控、两层渐进压缩、阶段相关记忆检索、运行时状态注入。
- **符号-LLM 混合决策**：阶段推断、转换摘要、硬约束校验、工具门控全部用规则驱动，不额外调 LLM。

但要从简历中脱颖而出，光有工程深度不够。最大的缺口是三个：

1. **缺少量化证据**：面试官会问"你怎么知道它好"，当前无法回答。
2. **缺少叙事包装**：面试官需要在 30 秒内理解"这个人为什么厉害"，当前的功能列表做不到。
3. **Harness 层投入与核心引擎不匹配**：Agent Loop 718 行、update_plan_state 394 行，但 guardrail + validator + judge 加起来只有 219 行。质量守护层的工程量配不上核心引擎的水平。

因此本报告的核心结论是：

> **在补量化 eval 和强化 harness 层的同时，必须重新包装叙事。2025 年是 Agent 年，2026 年是 Agent Harness 年——"Harness Engineering + Context Engineering + 结构化记忆 + 符号-LLM 混合决策 + 可评估"这五个关键词，比"自研 Agent Loop + 24 个工具 + 543 个测试"有力得多。**

---

## 0.5 可信度声明

| 可信度 | 依据类型 | 本报告使用方式 |
|--------|----------|----------------|
| **高** | 项目代码逐模块审查（agent/loop.py, tools/engine.py, memory/store.py 等 3800+ 行核心代码） | 支撑项目能力判断 |
| **高** | 正式论文（TravelPlanner ICML 2024, TravelBench 2025, TripTailor ACL 2025）、官方文档（Anthropic Context Engineering, OpenAI Agents SDK, MCP）、官方产业报告（Mem0 State of Agent Memory 2026） | 支撑趋势和优先级判断 |
| **中** | arXiv 最新论文（Flex-TravelPlanner, ATLAS, HiMAP-Travel, WorldTravel）、厂商博客（Galileo, LangSmith）、开源案例 | 支撑趋势判断，不当成定论 |
| **中** | 面试指南和 JD 分析（AgenticCareers, hackajob, InterviewQuery） | 支撑面试策略，不作为唯一依据 |

检索时间：**2026-04-12**。

---

## 1. 项目代码深度审查

### 1.1 规模与结构

| 指标 | 当前值 |
|------|--------|
| Python 源码（含测试） | 25,451 行 |
| TypeScript / TSX | 1,851 行 |
| CSS | 1,902 行 |
| 后端测试文件 | 86 个（22,000+ 行） |
| 后端测试用例 | 543 个 |
| 后端核心模块 | agent, context, phase, state, tools, llm, memory, storage, telemetry, harness |
| 领域工具 | 24+ 个 |
| 真实外部 API 集成 | Amadeus, FlyAI, Google Maps, Tavily, OpenWeather |

### 1.2 逐模块工程深度评估

以下评估基于实际代码审查，不是基于文档描述。

#### Agent Loop（`backend/agent/loop.py`，718 行）— 深度：深，8.5/10

**真正的亮点**：

- **三层自我修复机制**：
  1. Phase 3 状态修复（L560-641）：检测 LLM 生成了状态内容但未调用 `update_plan_state` 时，自动注入修复提示
  2. Phase 5 行程修复（L643-705）：正则检测逐日行程的日期/时间/活动关键词，天数不足则强制修复
  3. 冗余更新跳过（L707-717）：检测重复状态写入，避免无意义的工具调用
- **智能批处理调度**（L193-246）：识别"读"工具并行执行、"写"工具顺序执行，批次中检测 `saw_state_update` 自动触发阶段转换评估
- **回溯感知**（L278-296）：工具返回 `backtrack_result` 时自动跳过后续工具，重建消息历史

**薄弱点**：最大迭代限制为 3（有安全边界的合理原因，但限制了复杂修复场景）。

#### 工具系统（`backend/tools/`）— 深度：深，8.5/10

**关键发现：这是真实 API 集成，不是 mock。**

- `search_flights.py`：Amadeus API（Bearer token 认证）+ FlyAI CLI（Node.js 子进程异步包装）双源搜索，任一源失败不阻断，响应标准化合并去重
- `search_accommodations.py`：Google Maps API + FlyAI CLI 双源搜索，同样的异步并发 + 失败降级
- `web_search.py`：真实 HTTP POST 到 Tavily API，支持 basic/advanced 深度搜索
- `flyai_client.py`：asyncio.create_subprocess_exec 子进程管理，临时文件避免 Node.js pipe 缓冲问题，完整超时和错误处理
- `update_plan_state.py`（394 行）：支持 42 个字段写入、智能冗余检测（`is_redundant_update_plan_state()` 比较规范化值）、日期/预算/人数自动解析、`field="backtrack"` 触发 BacktrackService

#### 记忆系统（`backend/memory/`）— 深度：深，8/10

**这是整个项目面试价值最被低估的模块。**

- `store.py`：Schema v2 JSON 格式，AsyncLock per user_id 并发安全，自动向后兼容迁移，JSONL 事件日志 + 旅行 episode 归档
- `extraction.py`：LLM 结构化提取候选，JSON Code Block 解析 + 错误恢复
- `policy.py`：
  - 域阻断：payment/membership 信息直接 drop
  - PII 检测：护照号、email 正则、9-18 位数字序列、身份号关键词
  - 风险分类：低风险 + 高置信 → auto_save，高风险或冲突 → pending
  - 合并冲突检测：同 ID 不同值 → pending_conflict
- `retriever.py`：阶段相关检索——Phase 1 只召回 destination/pace/budget/family/planning_style，Phase 3 加入 hotel/flight/train/accessibility，Phase 5 加入 food，Phase 7 加入 documents。按置信度、类型优先级、更新时间排序

#### 上下文压缩（`backend/agent/compaction.py` + `context/manager.py`）— 深度：深，8/10

- Token 预算计算：`budget = context_window - output_tokens - safety_margin(2000)`
- 三级渐进阈值：<60% 不压缩、60-85% 温和压缩、85%+ 激进压缩
- 工具结果差异化压缩：web_search（8→5 结果、300→200 字符摘要）、xiaohongshu_search（12→8→5 项）、URL 去查询参数
- 阶段转换压缩：规则驱动，无额外 LLM 调用，格式 `用户: ... → 决策: field = value → 工具 {name} 成功: {preview}`
- 消息分类：检测偏好信号词标记 must_keep vs compressible

#### 质量守护（`backend/harness/`）— 深度：中，5/10

**这是报告与 v1 最大的分歧点之一：v1 对质量守护评估过于乐观。**

- `guardrail.py`：
  - 输入护栏：6 个英文正则检测提示注入（ignore, disregard, you are now a, system prompt），**中文注入完全不检测**，简单改写即可绕过
  - 日期验证：禁止过去日期
  - 预算验证：> 0
  - 输出护栏：空结果警告、异常高价检测（> 100K）
- `validator.py`：时间冲突、预算超支、天数超限——**是事后检查（Phase 7 之后），不是前置拦截**
- `judge.py`：pace/geography/coherence/personalization 四维评分接口存在，但评分模型薄弱

**核心问题**：护栏过于基础，验证时机太晚，工具结果缺乏结构化验证。

#### 阶段路由（`backend/phase/router.py` + `backtrack.py`）— 深度：中，7/10

- 规则驱动的阶段推断（非 LLM）：字段填充情况 → Phase 1/3/5/7
- Phase 3 子阶段自动推导：brief → candidate → skeleton → lock
- 回退服务：记录回退事件、调用 `plan.clear_downstream()` 清空下游数据
- 薄弱点：推断规则固定，缺乏"悬挂状态"处理（如用户提供不完整信息时）

#### 前端（`frontend/src/`）— 深度：中，6.5/10

- 组件模块化清晰：Phase3Workbench、ChatPanel（SSE 流渲染）、Timeline、MapView（Leaflet）、BudgetChart
- 状态管理为 React Hooks（useSSE 流监听、session 管理）
- 薄弱点：交互深度不足（无骨架拖拽编辑、表单验证最小化）、缺乏 Agent 执行链路可视化

#### 测试（`backend/tests/`，86 文件，22,000+ 行）— 深度：中，6/10

- 优质测试：`test_loop_payload_compaction.py`（9 个参数化用例）、`test_phase_router.py`（10 个测试，覆盖所有阶段转换路径）
- 薄弱点：**缺少真实 API 集成测试**（web_search, search_flights 均 mock）、**缺少端到端测试**（无完整 Agent 流程验证）、**缺乏负面测试**（API 超时、解析失败等错误路径验证不足）

### 1.3 工程成熟度总评

| 维度 | 评分 | 评价 |
|------|------|------|
| Agent 循环 | 8.5/10 | 三层自我修复，错误恢复层次清晰 |
| 工具集成 | 8.5/10 | 真实 API，双源降级，读写分离并行 |
| 记忆系统 | 8/10 | 架构成熟，PII 脱敏完整，portfolio 中极其罕见 |
| 上下文管理 | 8/10 | 两层渐进压缩，阶段转换规则摘要 |
| 可观测性 | 8/10 | OpenTelemetry 完整，Jaeger 集成 |
| 阶段路由 | 7/10 | 清晰可靠，规则硬编码但够用 |
| 前端 | 6.5/10 | 组件清晰，交互深度不足 |
| 测试 | 6/10 | 单元测试清晰，E2E/集成严重不足 |
| 质量守护 | 5/10 | 框架级，非生产级，验证时机晚 |

**总体评分：7.5/10 — 核心 Agent 系统生产级，质量守护和测试需强化。**

---

## 2. 旅行规划为什么难：最新 Benchmark 视角

### 2.1 核心难点

旅行规划 Agent 的难点不是生成"看起来像攻略"的文本，而是持续满足多个现实约束：

- 日期、天数、预算、人数、出发地、目的地
- 航班、火车、住宿、景点开放时间、天气和地理距离
- 用户偏好、同行人限制、老人/儿童/无障碍需求
- 多轮变更：预算降低、目的地改动、否定酒店、回退阶段
- 工具使用正确性：该查航班时查航班，不能靠模型编
- 约束优先级：硬约束不能牺牲，软偏好可以权衡
- **不可解任务识别**：Agent 应能判断"预算 500 元住 5 星 7 天"是不可行的，而不是编造假行程

### 2.2 最新 Benchmark 全景（2024-2026）

| Benchmark | 时间 | 核心贡献 | 关键结论 | 可信度 |
|-----------|------|---------|---------|--------|
| **TravelPlanner** | ICML 2024 | 1,225 个规划意图 + 参考方案 | 最强 LLM 生成的行程 **不到 10% 达到人类水平**；失败集中在多约束跟踪、工具使用、可行性 | 高 |
| **TripTailor** | ACL 2025 | 50 万+ 真实 POI + 4,000 条多样化行程 | 个性化评估维度：POI 匹配度、偏好满足度 | 高 |
| **TravelBench** | 2025.12 | 单轮 + 多轮 + **不可解任务** 三子任务 | 新增"Agent 能否识别不可能的任务"维度 | 高 |
| **WorldTravel** | 2026.02 | 多模态 + **紧耦合约束** | 时间/预算/地理约束必须同时满足，不能独立优化 | 中 |
| **Flex-TravelPlanner** | 2025 | 多轮动态约束 + 优先级 | 单轮表现不能预测多轮适应能力；约束引入顺序显著影响表现 | 中 |

参考：
- TravelPlanner: https://proceedings.mlr.press/v235/xie24j.html
- TravelBench: https://arxiv.org/abs/2512.22673
- TripTailor: https://aclanthology.org/2025.findings-acl.503/
- WorldTravel: https://arxivlens.com/PaperView/Details/worldtravel-a-realistic-multimodal-travel-planning-benchmark-with-tightly-coupled-constraints-682-f3187da9
- Flex-TravelPlanner: https://arxiv.org/abs/2506.04649

### 2.3 对本项目的直接启发

1. **不到 10% 达到人类水平**——这个数据本身就是最好的面试叙事："我选择旅行规划这个最难的 Agent 应用场景"。
2. **不可解任务检测**——TravelBench 新增的维度，当前项目完全缺失，但工程量小、区分度高。
3. **紧耦合约束**——WorldTravel 强调的维度，当前项目的硬约束校验器已有基础，但验证时机太晚。
4. **多轮变更稳定性**——Flex-TravelPlanner 的核心发现，当前项目有 backtrack 机制但缺少评估。

---

## 3. 2025-2026 行业趋势与面试官期望

### 3.1 六大行业趋势

#### 趋势一：2025 是 Agent 年，2026 是 Agent Harness 年

**可信度：高。**

Harness Engineering 在 2026 年已经从模糊概念变成被广泛认可的 Agent 工程子领域。核心定义：**Agent Harness = 模型之外的一切**——编排循环、工具调用、记忆管理、上下文管理、状态持久化、错误处理和护栏。模型生成文本，harness 决定这些文本能触达什么。

关键证据：
- LangChain 仅通过改变 harness（不换模型）就让 DeepAgent 从 TerminalBench Top 30 之外跃升至 Top 5——直接证明"模型是商品，harness 才是竞争力"
- Anthropic 从双 Agent Harness 演进到三 Agent Harness（Planner + Generator + Evaluator），核心设计哲学是"将执行工作的 Agent 和评判工作的 Agent 分离"
- LangChain 2026《State of AI Agents》报告：57% 的组织已部署 Agent，32% 将质量列为首要障碍——这正是 harness 要解决的问题
- Princeton 研究：harness 配置相比基础设置可将 Agent 解题成功率提升 64%

参考：
- LangChain: https://blog.langchain.com/improving-deep-agents-with-harness-engineering/
- Anthropic: https://www.anthropic.com/engineering/harness-design-long-running-apps
- InfoQ: https://www.infoq.com/news/2026/04/anthropic-three-agent-harness-ai/
- Martin Fowler: https://martinfowler.com/articles/exploring-gen-ai/harness-engineering.html

**对本项目的意义**：这是项目最重要的叙事升级机会。项目已经在做 harness engineering（Agent Loop + Hook 系统 + Guardrails + Validator + Judge + Reflection + ToolChoice + Memory Policy），但从未用这个词。同时，harness 层（guardrail 113 行 + validator 49 行 + judge 57 行 = 219 行）的投入与核心引擎（loop 718 行 + update_plan_state 394 行）严重不匹配，需要强化。

#### 趋势二：三代演进——Prompt → Context → Harness Engineering

**可信度：高。**

这三者形成层级关系而非替代关系：

| 层级 | 时代 | 关注点 | 本项目对应 |
|------|------|--------|-----------|
| **Prompt Engineering** | 2022-2024 | 优化单次交互的指令措辞 | `phase/prompts.py`（431 行阶段提示词）、`context/soul.md` |
| **Context Engineering** | 2025 | 策划完整上下文环境 | 工具门控 + 两层压缩 + 记忆检索 + 状态注入 |
| **Harness Engineering** | 2026 | 构建完整执行基础设施 | Agent Loop + Hooks + Guardrails + Validator + Judge + Reflection + ToolChoice + Memory Policy |

Anthropic 2025 年 9 月发布《Effective Context Engineering for AI Agents》，将行业共识从"如何写好 prompt"转向"如何策划进入上下文窗口的全部信息"。2026 年进一步演进到 Harness Engineering——Context Engineering 存在于 Harness Engineering 之内。

参考：
- Anthropic Context Engineering: https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Prompt vs Context vs Harness: https://medium.com/@server_62309/prompt-engineering-vs-context-engineering-vs-harness-engineering-whats-the-difference-in-2026-2883670f78f1

**对本项目的意义**：项目天然覆盖了三代演进的全部层级。面试时可以用这条演进链条来讲述项目架构：prompt 层（阶段提示词）、context 层（门控+压缩+记忆）、harness 层（循环+钩子+护栏+验证+评分+反射）。

#### 趋势三：Agent Memory 成为独立工程学科

Mem0 在 2026 年 4 月发布《State of AI Agent Memory 2026》："AI Agent Memory 三年前几乎不存在作为一个独立工程学科。开发者把对话历史塞进上下文窗口，就称之为'记忆'了。"微软发布 PlugMem 研究，强调将原始交互转化为可复用知识。

参考：
- Mem0: https://mem0.ai/blog/state-of-ai-agent-memory-2026
- Microsoft PlugMem: https://www.microsoft.com/en-us/research/blog/from-raw-interaction-to-reusable-knowledge-rethinking-memory-for-ai-agents/

**对本项目的意义**：项目的记忆系统已经实现了行业刚刚定义为"成熟"的架构。这是与大多数 portfolio 项目的最大差异化点之一，应该提升为第一梯队亮点。

#### 趋势四：符号-LLM 混合架构兴起

OpenSymbolicAI 在 TravelPlanner 基准上实现 100% 通过率，token 消耗仅为 LangChain 的 1/6，LLM 调用次数仅为 CrewAI 的 1/17。核心思想："LLM 规划一次，代码执行"。

参考：
- OpenSymbolicAI: https://www.opensymbolic.ai

**对本项目的意义**：项目已经在做混合架构——阶段推断、转换摘要、硬约束校验、工具门控全部规则驱动（不调 LLM）。应该明确包装为"符号+LLM 混合决策"。

#### 趋势五：评估从 Benchmark 驱动转向系统级

Galileo 2026 评估框架区分轨迹级指标（工具选择正确性、步骤效率）和结果级指标（任务完成度）。74% 的团队在自动化评估之外仍依赖人工审核。生产级评估需要提交驱动 + 定时驱动 + 事件驱动三种触发。

参考：
- Galileo: https://galileo.ai/blog/agent-evaluation-framework-metrics-rubrics-benchmarks
- KDD 2025 Tutorial: https://sap-samples.github.io/llm-agents-eval-tutorial/

#### 趋势六：评估层是 Portfolio 项目的真正区分器

"Pipeline 本身已经被充分理解了；让你的项目与众不同的是评估层。""两到三个深入的、有完善文档和真正评估工作的项目，会压倒十个表面级实现。""顶级 AI 公司的招聘经理花在你 GitHub 上的时间比简历多。"

参考：
- AgenticCareers: https://agenticcareers.co/blog/ai-agent-portfolio-projects-get-hired-2026

### 3.2 面试官看重什么

| 面试官关注维度 | 对本项目的映射 | 当前状态 |
|---------------|--------------|---------|
| **评估方法论**：能定义指标、构建 eval 数据集、量化 Agent 质量 | eval pipeline + golden cases | 缺失 |
| **生产直觉**：原型 vs 产品的区别、成本优化、可靠性保障 | 成本统计 + 失败分析 | 缺失 |
| **Context Engineering**：策划完整上下文环境 | 阶段门控 + 压缩 + 记忆检索 | 已实现，未包装 |
| **工具编排与错误处理** | 24+ 工具 + 双源降级 + 读写分离 | 已实现 |
| **结构化 Memory** | global/trip + PII + policy + retriever | 已实现 |
| **可观测性 / Tracing** | OpenTelemetry + Jaeger | 已实现 |
| **失败案例意识** | 知道 Agent 什么时候会犯错 | 缺少系统性分析 |
| **成本与延迟优化** | token 统计 + 模型切换 | 有机制但无统计数据 |

### 3.3 Agent 工程必备能力 vs 高区分度加分项

| 必备能力 | 本项目覆盖 |
|---------|-----------|
| Python + 异步编程 | 完整 async/await |
| LLM API 使用（多供应商） | OpenAI + Anthropic 双供应商 |
| Context Engineering | 阶段门控 + 压缩 + 记忆检索 |
| 工具定义与调用编排 | 24+ 工具 + @tool 装饰器 + 阶段过滤 |
| 评估方法论 | **缺失** |
| 基本安全意识 | PII 脱敏有，提示注入检测弱 |

| 高区分度加分项 | 本项目覆盖 |
|--------------|-----------|
| 结构化 Agent Memory | 完整实现 |
| 可观测性 / Tracing | OpenTelemetry + Jaeger |
| Agent 评估体系 | **缺失** |
| 上下文压缩 / 窗口管理 | 两层渐进压缩 |
| 成本与延迟优化 | 有机制，无统计 |
| 多 LLM 按阶段热切换 | 已实现 |
| 阶段化执行流程 | 7 阶段 + 子步骤 |
| 护栏与安全系统 | 框架级，需强化 |

**关键发现**：项目在 7 个高区分度加分项中覆盖了 5 个，唯一完全缺失的必备能力是"评估方法论"。这也是为什么补 eval 是 P0。

---

## 4. 叙事重构：Harness Engineering 统领下的五个核心维度

当前项目最大的问题不是缺能力，而是缺叙事。**Harness Engineering 是把项目里所有散落的智能组件统一到一个认知框架下的最佳顶层叙事。**

### 4.0 顶层叙事：Harness Engineering

面试时的第一句话不要从"我做了一个旅行 Agent"开始。应该从 harness 讲起：

> 2026 年 Agent 领域的核心洞察是"模型是商品，harness 才是竞争力"——LangChain 仅通过改变 harness、不换模型，就让排名从 Top 30 之外跃升到 Top 5。我的项目本质上就是一个完整的 Agent Harness 系统：不只是调用 LLM 生成文本，而是围绕 LLM 构建了编排循环、阶段状态机、工具门控、上下文压缩、结构化记忆、质量护栏、自省注入、强制工具决策和可观测追踪。

项目的 harness 架构全景：

```
Travel Agent Pro — Harness Architecture
├── Orchestration Layer（编排层）
│   ├── AgentLoop — 718 行，三层自我修复
│   ├── HookManager — before_llm_call / after_tool_call 事件驱动
│   └── PhaseRouter — 规则驱动阶段推断 + BacktrackService
├── Context Engineering Layer（上下文层）
│   ├── ContextManager — soul + 阶段指引 + plan 快照 + 记忆注入
│   ├── Compaction — token 预算渐进压缩 + 阶段转换规则摘要
│   └── ToolEngine — 阶段级工具门控 + 读写分离并行调度
├── Quality Assurance Layer（质量层）
│   ├── ToolGuardrail — 输入/输出护栏（需强化）
│   ├── Validator — 硬约束校验（需前移触发时机）
│   ├── SoftJudge — 4 维 LLM 评分（需加固）
│   ├── ReflectionInjector — 关键阶段自省 prompt 注入
│   └── ToolChoiceDecider — 强制 update_plan_state 调用
├── Memory Layer（记忆层）
│   ├── Extraction — 后台候选提取
│   ├── Policy — 风险分类 + PII 脱敏 + 合并冲突
│   ├── Retriever — 阶段相关三路检索
│   └── Store — schema v2 + AsyncLock 并发安全
├── Tool Layer（工具层）
│   └── 24+ 旅行工具 — 真实 API + 双源降级
└── Observability Layer（可观测层）
    └── OpenTelemetry + Jaeger — span 覆盖全链路
```

这个架构图本身就是面试时最有力的展示——它不是"我做了很多功能"，而是"我设计了一个完整的执行基础设施"。

### 4.1 Context Engineering 子层

> 我的系统体现了 Context Engineering 的核心理念：不是把所有工具和信息一次性扔给 LLM，而是在每个阶段精确控制 LLM 能看到什么（阶段级工具门控）、上下文窗口里放什么（两层渐进压缩）、记忆检索什么（阶段相关三路检索）、系统提示注入什么（soul + 阶段指引 + plan 快照）。

这个叙事直接对标 Anthropic 2025 年提出的核心概念。Context Engineering 是 Harness Engineering 的子层。

### 4.2 结构化记忆作为独立工程系统

> 我把记忆当作一个独立的工程系统来设计。它不是简单的对话历史或 summary，而是有完整的数据生命周期：后台异步提取候选 → policy 风险分类（域阻断 + PII 检测 + 置信度判断）→ 低风险自动保存 / 高风险 pending 人工确认 → 阶段相关三路检索（core profile / trip memory / phase-domain）→ trip_id 隔离 → 新行程回退时轮转 trip_id 避免污染 → Phase 7 完成后幂等归档为 episode。

与 Mem0 2026 报告定义的"Agent Memory 作为独立学科"完全对齐。

### 4.3 符号-LLM 混合决策

> 我的系统在关键决策点用符号规则代替 LLM 调用。阶段推断是规则驱动（字段填充 → 阶段判定），阶段转换摘要是规则驱动（不额外调 LLM），硬约束校验是代码执行，工具门控是静态配置。这种混合架构降低了延迟和成本，同时在确定性决策点保持了可预测性。只在需要创造性推理的环节（需求理解、方案设计、行程编排）才用 LLM。

对标 OpenSymbolicAI 等前沿研究，展示了对"什么该用 LLM、什么不该"的工程判断力。

### 4.4 Quality Assurance 子层（当前最需补强）

当前是项目 harness 中最薄弱的部分。目标叙事（强化后）：

> Harness 的质量层包含多级验证：工具调用前的输入护栏（提示注入检测、日期/位置/预算校验）、工具调用后的输出护栏（结果结构验证、异常检测）、阶段转换前的硬约束校验（时间冲突、预算超支、天数超限）、关键决策点的 LLM 软评分（节奏/地理/连贯/个性化四维度）、以及阶段边界处的自省注入。核心设计原则是"每次工具调用后都有验证步骤"——这被认为是 harness 工程中影响力最高的单一模式。

参考：
- TraceSafe 论文指出多步骤工具调用中的静默失败会复合累积：https://arxiv.org/html/2604.07223

### 4.5 可评估、可观测的 Agent 系统

这是当前最缺的叙事，需要通过 P0 改进来补齐（见第 7 节）。目标叙事：

> 我不仅构建了 Agent，还构建了评判 Agent 的系统。通过 golden-case eval 追踪任务完成率、硬约束通过率、工具选择准确率、pass@k 稳定性、token 成本和延迟。通过 OpenTelemetry 追踪每轮执行的完整链路。通过失败案例分析知道 Agent 在什么场景下会犯错。

---

## 5. 差距矩阵

| 差距 | 当前状态 | 面试官追问 | 最小可行改进 | 完成后怎么证明 |
|------|----------|-----------|-------------|---------------|
| **Harness 质量层薄弱** | guardrail+validator+judge 仅 219 行 | "质量怎么保障？" | 强化护栏（中文注入、约束前移、结构验证）+ 加固评分器 | 测试用例 + 拦截率统计 |
| **Agent 质量不可量化** | 有 harness 但无 batch eval | "你怎么知道它好？" | 10-20 个 golden cases + eval runner | eval 报告 + 趋势图 |
| **失败案例未分析** | 没有系统性的失败记录 | "什么时候会失败？" | 手动跑 5-10 个场景，记录失败点和修复 | 失败案例分析文档 |
| **成本不可见** | 有压缩和模型切换，无统计 | "一次规划花多少钱？" | 每次 LLM/工具调用记录 token、耗时、成本 | session cost report |
| **叙事未包装** | 功能列表式描述 | "30 秒介绍你的项目" | 用 Harness Engineering 重构叙事 | 简历 + 话术 |
| **不可解任务不识别** | 无 Phase 1 可行性预判 | "不可能的需求怎么办？" | Phase 1 结束时加 feasibility pre-check | demo 展示 |
| **质量守护基础** | 提示注入只检测英文，验证事后 | "安全怎么保证？" | 中文注入检测 + 约束前移 + 结构化验证 | 测试用例 |
| **Demo 不够可复现** | 有 dev 脚本，缺标准数据 | "我怎么跑？" | seed data + demo script + 录屏 | README + 录屏 |
| **记忆不可解释** | 后端成熟，前端不可视 | "用户怎么控制？" | memory center + 来源展示 | UI demo |
| **多轮变更未评估** | 有 backtrack，缺评估 | "中途改需求呢？" | 多轮约束变更 golden cases | pass@k |
| **长链路调试体验不足** | 有 Jaeger，前端不可视 | "失败怎么定位？" | trace timeline / waterfall | 复盘 demo |
| **RAG 缺失** | 靠搜索和工具 | "知识怎么管理？" | 旅行约束 KB | retrieval eval |
| **MCP 缺失** | 自研工具协议 | "能否接标准生态？" | 轻量 MCP adapter | 3-5 个工具可被 MCP 调用 |

---

## 6. 与 v1 报告的具体判断差异

| 判断点 | v1 报告 | 本报告 | 差异原因 |
|-------|--------|--------|---------|
| **项目基线认知** | 隐含"还不够好" | 明确"portfolio 顶尖水平" | 代码审查证实工程深度远超大多数 portfolio 项目 |
| **安全基线优先级** | P0（1-2 周） | P2 | Portfolio 项目的面试官不会因为 CORS 是 `*` 否定你 |
| **Context Engineering 叙事** | 完全未提及 | P0 叙事重构 | 2025-2026 最重要的 Agent 趋势，项目已实现但未包装 |
| **符号-LLM 混合叙事** | 完全未提及 | P0 叙事重构 | 对标 OpenSymbolicAI 等前沿研究 |
| **记忆系统面试价值** | P1"放大优势" | 提升为第一梯队亮点 | 2026 年 Memory 成为独立学科，项目实现远超同级 |
| **Harness Engineering 叙事** | 完全未提及 | 作为顶层叙事框架 | 2026 年最核心的 Agent 工程趋势，项目天然覆盖但未意识到 |
| **不可解任务检测** | 完全未提及 | P0（工程量小、区分度高） | TravelBench 2025 新增维度 |
| **失败案例分析** | 未提及 | P0（2-3 天产出，面试 ROI 最高） | 面试官对"什么时候会失败"的兴趣远大于"能做什么" |
| **质量守护评估** | "面试价值：高" | 5/10，框架级非生产级，需重点强化 | 代码审查发现护栏基础、验证时机晚、中文不检测；harness 质量层与核心引擎投入不匹配 |
| **简历写法** | 功能列表 | Harness Engineering 叙事 + 工程决策 + 问题解决 | 功能列表无区分度，harness 框架让面试官看到系统设计能力 |
| **竞品 Benchmark** | TravelPlanner, Flex, ATLAS, HiMAP | + TravelBench, WorldTravel, TripTailor | 2025 年底新增的重要 Benchmark |
| **MCP 写入简历** | v1 指出不应写（正确） | 同意 | — |
| **多 Agent 优先级** | 不是最短路径（正确） | 同意，进一步说明项目当前架构更务实 | 多 Agent 在可控性和调试上有显著挑战 |

---

## 7. 改进路线

### P0：让项目可信 + 重构叙事（1-2 周）

#### 7.1 手动失败案例分析（2-3 天，面试 ROI 最高）

**为什么排在最前面**：不需要自动化框架，产出是一份文档，但面试说服力极高。面试官对"什么时候会失败"的兴趣远大于"能做什么"。

做法：
- 手动跑 5-10 个真实场景，覆盖简单/复杂/边界/不可能任务
- 记录每个场景：用户输入 → Agent 行为 → 成功/失败 → 失败根因 → 修复方式

示例场景：
| 场景 | 预期难度 | 观察目标 |
|------|---------|---------|
| "5 天 3000 元日本自由行" | 高（预算极紧） | 预算约束是否在 lock 前生效 |
| "带 80 岁老人去九寨沟" | 高（特殊需求） | 是否考虑高海拔、无障碍 |
| "500 元去马尔代夫住 5 星 7 天" | 不可解 | 是否识别为不可行 |
| "先去东京再去京都，但中途改成大阪" | 多轮变更 | backtrack 是否正确清理 |
| "3 个人春节去三亚，一个素食者" | 约束组合 | 饮食约束是否进入行程 |

产出：`docs/failure-analysis.md`，每个失败案例含根因分析和修复方案。

#### 7.2 端到端 Eval Pipeline（1 周）

最小实现：
- `evals/golden_cases/*.yaml`：10-20 个 case
- 每个 case 定义：
  - 用户多轮输入
  - 期望阶段变化
  - 必须写入的 state 字段 / 禁止写入的字段
  - 必须调用或禁止调用的工具
  - 硬约束断言
- `evals/runner.py`：批量执行 + JSON 报告
- `evals/report.md`：结果汇总

建议指标（轨迹级 + 结果级双轨）：

| 指标 | 类型 | 含义 |
|------|------|------|
| task_completion | 结果级 | 是否完成目标阶段 |
| hard_constraint_pass | 结果级 | 日期/预算/天数/路线/开放时间是否满足 |
| tool_selection_accuracy | 轨迹级 | 工具调用是否符合预期 |
| state_write_accuracy | 轨迹级 | 状态写入是否准确 |
| step_efficiency | 轨迹级 | 是否用合理步数完成 |
| backtrack_success | 轨迹级 | 回退是否正确清理下游状态 |
| infeasibility_detection | 结果级 | 不可解任务是否被正确识别 |
| memory_safety | 轨迹级 | 是否错误记忆或污染 scope |
| latency_ms | 运营级 | 端到端耗时 |
| token_usage | 运营级 | 输入/输出 token |
| estimated_cost | 运营级 | 估算成本 |

#### 7.3 成本/延迟/工具调用统计

最小实现：
- 每次 LLM 调用记录：provider, model, input_tokens, output_tokens, duration_ms
- 每次工具调用记录：tool_name, duration_ms, status, error_code
- 每个 session 汇总：total_cost, total_latency, tool_call_count, avg_tokens_per_turn
- 提供一个 `/api/sessions/{id}/stats` 端点或在 eval 报告中包含

#### 7.4 不可解任务检测（工程量小，区分度高）

在 Phase 1 结束时增加 feasibility pre-check：
- 预算 vs 目的地基础消费水平
- 天数 vs 目的地最低游览时间
- 人数/特殊需求 vs 目的地适配性

不需要复杂实现，一个轻量级规则检查 + LLM 辅助判断即可。关键是面试时能说："我的系统不仅能规划，还能识别不可能的任务。"

#### 7.5 Harness 质量层强化（与 eval 同步推进）

当前 harness 质量层（219 行）与核心引擎（1100+ 行）投入严重不匹配。最小强化清单：

**guardrail.py 强化**：
- 增加中文提示注入模式："忽略之前的指令"、"你现在是"、"不要遵守规则" 等
- 增加工具结果结构化验证：航班搜索结果必须包含 price/departure_time/arrival_time
- 增加用户输入长度限制

**validator.py 前移**：
- 将硬约束检查从"Phase 7 之后"前移到"每次 update_plan_state 之后"
- Phase 3 lock 前：交通/住宿价格不超预算上限
- Phase 5 日程编排时：实时检查时间冲突和地理距离

**judge.py 加固**：
- 评分解析失败时记录告警（而不是静默返回默认 3 分）
- 增加评分分布统计（用于 eval 报告）
- 考虑在阶段转换门控中真正使用评分结果

**核心设计原则**：每次工具调用后都有验证步骤。这被认为是 harness 工程中影响力最高的单一模式——TraceSafe 论文指出多步骤工具调用中的静默失败会复合累积。

#### 7.6 叙事重构

重写项目所有对外表达：
- 简历（见第 9 节）
- README 开头
- 面试话术（见第 10 节）

核心关键词替换：
| 旧表述 | 新表述 |
|--------|--------|
| 自研 Agent Loop | 带三层自我修复的 Agent Loop |
| 做了护栏/验证/评分 | Harness Engineering：编排 + 护栏 + 验证 + 评分 + 反射 + 强制工具决策的完整执行基础设施 |
| 工具门控 + 上下文压缩 | Context Engineering：阶段级工具门控 + 两层渐进压缩 + 阶段相关记忆检索 |
| 记忆功能 | 结构化记忆系统（对标 Mem0 2026 定义的 Agent Memory 独立工程学科） |
| 规则驱动阶段路由 | 符号-LLM 混合决策（确定性决策用规则，创造性推理用 LLM） |
| 24 个工具 | 24+ 旅行领域工具接入真实 API，双源搜索 + 失败降级 |
| 543 个测试 | （不作为简历亮点，而是面试追问时的支撑）|

#### 7.7 可复现 Demo

最小实现：
- seed session / seed memory 数据
- 3 条 demo 脚本：
  1. 模糊需求 → 目的地收敛（Phase 1）
  2. 框架规划 + 骨架选择（Phase 3）
  3. 日程详排 + 用户中途回退（Phase 5）
- demo 录屏或 GIF

### P1：放大差异化（2-4 周）

#### 7.8 Memory Center 与记忆可解释

把后端记忆能力展示到前端：
- 前端展示 active / pending / rejected / obsolete 状态
- 每条记忆展示 source quote、confidence、scope、domain、trip_id
- 支持确认、拒绝、删除操作
- Agent 回复或 debug 面板显示本轮命中的记忆
- memory eval：错误记忆率、过度泛化率、trip 污染率

#### 7.9 质量守护升级（P1 阶段继续深化 P0 的 harness 强化）

当前质量守护是代码审查评分最低的模块（5/10），需要重点提升：

1. **提示注入检测支持中文**：增加中文常见注入模式（"忽略之前的指令"、"你现在是"等）
2. **约束检查前移**：
   - Phase 3 lock 前：验证交通/住宿价格不超预算上限
   - Phase 5 日程编排时：实时检查时间冲突和地理距离
3. **工具结果结构化验证**：航班搜索结果是否真的包含 price/departure_time/arrival_time 字段
4. **提示注入检测**不只靠正则——考虑加一个轻量级 LLM 分类器或更强的规则引擎

#### 7.10 Phase 5 约束验证增强

旅行规划核心质量点在 Phase 5：
- 景点开放时间校验
- 活动间交通时间校验
- 每日节奏和地理聚类
- 预算累计实时检查
- 天气风险提示
- 老人/儿童/无障碍约束

把这些变成可测试的 validator 组件，而不是只靠 prompt。

#### 7.11 Agent Trace Viewer

前端展示一轮规划的执行链路：
- LLM 调用（model, tokens, duration）
- 工具调用 waterfall（名称, 耗时, 状态）
- state diff（哪些字段被修改）
- memory hit（本轮命中了哪些记忆）
- validator / judge 结果
- context compression 事件

#### 7.12 服务层拆分

建议拆分 `main.py`（856 行）：
- `routers/chat.py`
- `routers/session.py`
- `routers/memory.py`
- `services/memory_extraction.py`
- `services/session_lifecycle.py`
- `services/event_stream.py`
- `dependencies.py`

目标不是"文件更小"，而是让 eval、trace、安全和 memory 逻辑更容易测试。

### P2：先进架构展示（时间充裕再做）

#### 7.13 旅行约束知识库 / RAG

不要做泛泛的百科 RAG。优先做：
- 签证规则
- 城市交通卡和机场交通
- 区域关系（东京-京都-大阪的关系）
- 景点闭馆日
- 季节风险
- 亲子、老人、无障碍提示

要求：有来源引用、有过期时间或刷新策略、检索结果能进入 validator 或 constraints、有 retrieval eval。

#### 7.14 MCP Adapter

只做轻量兼容：
- 暴露 `web_search`、`get_poi_info`、`calculate_route`、`check_weather`、`search_accommodations`
- 保留现有 `@tool` 系统作为内部工具治理层
- adapter 只负责协议转换

#### 7.15 Specialist Critic / Researcher

在现有 Agent Loop 上增加 specialist，而不是重写成复杂多 Agent：
- Researcher：负责候选信息收集
- Critic：负责约束检查和方案批判
- Repairer：负责根据 validator 错误修复方案
每个 specialist 必须有明确输入输出和 eval case。

---

## 8. 不建议优先做的事

| 不建议优先项 | 原因 |
|-------------|------|
| 全量 MCP 化 | 工具系统已有治理能力，协议不是第一瓶颈 |
| 直接改成多 Agent | 增加复杂度、降低可控性，不一定提升质量 |
| 泛泛做目的地百科 RAG | 与 web search 重叠，面试说服力有限 |
| 完整账号系统 / 重 JWT | 作品集阶段成本高，与面试价值不成比例 |
| 大规模 UI 重设计 | 当前短板在可证明能力和叙事，不是视觉 |
| CORS/限流/安全加固 | 部署到公开环境时再做，不是 portfolio 的 P0 |
| 堆更多功能标签 | 面试官看的是深度，不是广度 |

---

## 9. 简历

### 9.1 当前可用版本

以下只包含当前已实现的能力，以 Harness Engineering 为顶层叙事框架：

**Travel Agent Pro — 基于 Harness Engineering 的复杂旅行规划 Agent 系统**
独立设计与开发 | Python, FastAPI, React, TypeScript

- 围绕 LLM 构建完整的 Agent Harness 执行基础设施，将旅行规划拆解为 7 阶段认知决策流，涵盖编排循环、阶段状态机、工具门控、上下文压缩、结构化记忆、质量护栏、自省注入和可观测追踪。
- 自研 Agent Loop 替代框架，循环内置三层自我修复（状态同步检测、行程完整性修复、冗余操作跳过），通过 Hook 事件系统将质量验证、上下文重建和阶段转换门控与核心循环解耦。
- 采用 Context Engineering 理念在每个阶段精确控制 LLM 的可见信息：阶段级工具门控（Phase 3 四子步骤逐步开放工具）、两层渐进上下文压缩（token 预算 + 阶段转换规则摘要）、阶段相关三路记忆检索注入。
- 设计结构化记忆系统（global/trip 双 scope），实现后台候选提取 → policy 风险分类 → PII 多维脱敏 → pending 确认的完整写入链路，按 trip_id 隔离跨行程记忆。
- 接入 24+ 旅行领域工具调用真实 API（Amadeus、Google Maps、FlyAI、Tavily），双源搜索 + 失败降级，读写分离并行调度。
- 在关键决策点采用符号-LLM 混合架构：阶段推断、转换摘要、硬约束校验和工具门控均为规则驱动（零额外 LLM 调用），仅在创造性推理环节使用 LLM。
- 接入 OpenTelemetry + Jaeger 追踪完整执行链路，覆盖 Agent Loop、工具调用、阶段转换和上下文压缩事件。

英文版：

**Travel Agent Pro — Complex Travel Planning Agent System Built on Harness Engineering**
Sole designer & developer | Python, FastAPI, React, TypeScript

- Built a complete Agent Harness around LLM for travel planning: orchestration loop, phase state machine, tool gating, context compaction, structured memory, quality guardrails, reflection injection, and observability tracing across a 7-phase cognitive workflow.
- Developed a custom Agent Loop with three layers of self-repair (state sync detection, itinerary completeness repair, redundant operation skipping), decoupling quality validation, context rebuild, and phase transition gating from the core loop via a Hook event system.
- Applied Context Engineering to precisely control LLM-visible information at each phase: phase-level tool gating (Phase 3 progressively opens tools across 4 sub-steps), two-layer progressive context compaction, and phase-aware three-path memory retrieval injection.
- Designed a structured memory system (global/trip dual scope) with background candidate extraction, policy-driven risk classification, multi-dimensional PII redaction, pending confirmation, and trip_id isolation.
- Integrated 24+ travel domain tools calling real APIs (Amadeus, Google Maps, FlyAI, Tavily) with dual-source search, failure degradation, and read/write parallel dispatch.
- Applied symbolic-LLM hybrid architecture: phase inference, transition summaries, hard constraint validation, and tool gating are all rule-driven (zero extra LLM calls), reserving LLM for creative reasoning.
- Integrated OpenTelemetry + Jaeger to trace the full execution pipeline across Agent Loop iterations, tool calls, phase transitions, and context compression events.

### 9.2 完成 P0 升级后的目标版

以下内容必须完成对应实现后再使用：

**目标版新增亮点：**

- 建立旅行规划 eval pipeline，覆盖多轮约束变更、不可解任务识别、预算/路线/开放时间约束、工具选择和回退场景，并在 CI 中运行 smoke eval。
- 实现 pass@k 稳定性评估，持续追踪任务完成率、硬约束通过率、工具选择准确率、状态写入准确率、步骤效率。
- 进行系统性失败案例分析，记录 Agent 在预算极紧、特殊人群、多轮变更、不可解任务等场景下的失败模式、根因和修复策略。
- 增加会话级 token、成本、延迟和工具调用统计，支持 Agent 行为回归分析和成本优化。
- 实现不可解任务预判，在 Phase 1 结束时进行可行性检查，避免为不可能的需求生成虚假行程。

目标版简历条目示例：

- 设计旅行规划 Agent 评估体系，覆盖 30+ golden cases（含多轮约束变更和不可解任务场景），追踪任务完成率、硬约束通过率、工具选择准确率、pass@k 稳定性、token 成本和端到端延迟。对 Agent 进行系统性失败分析，定位了预算约束注入时机、特殊人群需求覆盖不足等核心失败模式并逐一修复。
- 构建 Agent trace viewer，将 LLM 调用、工具 waterfall、state diff、memory hit、validator 结果和 context compression 事件可视化，用于复盘失败案例和定位规划质量回归。

---

## 10. 面试话术

### 10.1 "30 秒介绍你的项目"

> 2026 年 Agent 领域有一个核心洞察——"模型是商品，harness 才是竞争力"。LangChain 仅通过改变 harness、不换模型，就让 Agent 排名从 Top 30 之外跃升到 Top 5。我的项目就是围绕这个理念构建的：不只是调用 LLM 生成旅行攻略，而是围绕 LLM 构建了一套完整的 Harness——编排循环、阶段状态机、工具门控、上下文压缩、结构化记忆、质量护栏、自省注入和可观测追踪。在 TravelPlanner benchmark 上，最强 LLM 生成的行程不到 10% 达到人类水平。我的核心工作就是通过 harness 让这个比例提升。

### 10.2 "为什么不用 LangChain？"

> 旅行规划需要精确控制阶段转换、工具门控、状态写入、上下文压缩和回退恢复。我需要在每个阶段只暴露该阶段需要的工具、在每次 LLM 调用前按 token 预算动态压缩上下文、在检测到 LLM 没有调用必要工具时自动注入修复提示、在阶段转换前运行质量门控。这些是 harness 层面的精细控制，通用框架很难做到。而且 LangChain 自己在 2026 年也明确区分了 framework / runtime / harness 三层，harness 是最外层的应用级编排。我选择自研 harness 是为了获得旅行规划场景下的完整控制力和可观测性。

### 10.3 "怎么评估 Agent 质量？"

当前诚实版：

> 目前有运行时质量守护——硬约束校验、软评分、阶段转换 gate 和工具 guardrails。我还做了 5-10 个场景的手动失败分析，知道 Agent 在预算极紧、特殊人群需求、多轮变更时的失败模式。下一步是把这些固化为自动化 eval pipeline，追踪任务完成率、硬约束通过率、工具选择准确率和 pass@k 稳定性。

完成升级后版：

> 我建立了一套旅行规划 eval，覆盖 30+ golden cases，包括多轮约束变更和不可解任务识别。CI 跑 smoke eval，完整 eval 统计任务完成率、硬约束通过率、工具选择准确率、步骤效率、token 成本和延迟。我还做了系统性失败分析，发现了 X 个核心失败模式并逐一修复，整体约束通过率从 Y% 提升到 Z%。

### 10.4 "记忆系统怎么设计？"

> 我把记忆当作独立的工程系统。它不是 chat summary，而是有完整的数据生命周期：每轮对话后后台异步提取候选 → policy 判断风险（payment/membership 域直接阻断、PII 多维检测脱敏、置信度分级）→ 低风险高置信自动保存、高风险或冲突进入 pending 等用户确认 → 读取时按阶段三路检索（core profile 是长期画像、trip memory 只取当前行程、phase-domain 按当前阶段过滤）→ 新行程回退时轮转 trip_id 避免旧记忆污染 → Phase 7 完成后幂等归档为 episode。
>
> 这套设计参考了 Mem0 在 2026 年提出的 Agent Memory 独立学科理念，核心是把记忆从"聊天附属品"提升为有数据治理的系统。

### 10.5 "怎么控制成本？"

> 有几个层面。第一，阶段级工具门控，每个阶段只暴露需要的工具，减少 LLM 在工具选择上的错误和无效调用。第二，符号-LLM 混合决策，阶段推断、转换摘要、硬约束校验全部规则驱动，不额外调 LLM。第三，两层上下文压缩，LLM 调用前按 token 预算渐进压缩、阶段转换时用规则生成摘要替代 LLM 总结。第四，按阶段切换模型——需求收集阶段用 Claude Sonnet，日程详排阶段用 GPT-4o。（完成升级后补充：第五，每次调用都记录 token 和耗时，eval 报告包含每个 case 的成本，可以看到 prompt 或工具策略变更对成本的影响。）

### 10.6 "什么时候 Agent 会失败？"（最有区分度的问题）

> 旅行规划 Agent 的失败不是"生成了一段不好的文字"，而是"约束不满足"。我的失败分析发现了几个典型模式：
>
> 第一，预算约束注入时机太晚——Agent 在 Phase 3 lock 选酒店时没有把预算上限传给搜索工具，导致推荐了超预算的住宿。修复方式是在 lock 子步骤前注入预算约束。
>
> 第二，特殊人群需求覆盖不足——"带老人去高海拔地区"，Agent 没有考虑高海拔风险。根因是缺少旅行约束知识库，纯靠 LLM 的世界知识不够可靠。
>
> 第三，多轮变更时的状态残留——用户先说去东京再说改去大阪，backtrack 清除了行程但没有清除记忆中的东京相关偏好。修复方式是回退时同时轮转 trip_id。
>
> （这种回答比"我的 Agent 做了 XX 功能"有力得多，因为它展示了你对系统边界的理解。）

### 10.7 "什么是 Harness Engineering？你的项目怎么体现？"

> 2026 年行业形成了一个共识：Agent 的竞争力不在模型，而在 harness——围绕 LLM 构建的整个执行基础设施。LangChain 仅通过改变 harness 就让 Agent 排名跃升。Anthropic 从双 Agent Harness 演进到三 Agent Harness。Princeton 研究表明 harness 配置可以让成功率提升 64%。
>
> 我的项目本身就是一个 harness。它分为五层：
>
> **编排层**：Agent Loop（带三层自我修复）+ Hook 事件系统（before_llm_call / after_tool_call）+ 阶段路由与回退
>
> **上下文层**：阶段级工具门控 + 两层渐进压缩 + 阶段相关记忆检索 + 运行时状态注入——这就是 Context Engineering
>
> **质量层**：输入/输出护栏 + 硬约束验证 + LLM 四维软评分 + 自省注入 + 强制工具决策
>
> **记忆层**：后台提取 → policy 风险分类 → PII 脱敏 → pending 确认 → 阶段检索 → trip_id 隔离
>
> **可观测层**：OpenTelemetry + Jaeger 覆盖全链路
>
> 这五层加在一起，就是"模型之外的一切"。面试官如果熟悉这个概念，一听就知道这不是一个套框架的 demo。

### 10.8 "Context Engineering 是什么？你怎么应用的？"

> Context Engineering 是 Harness 中的一个子层，Anthropic 在 2025 年提出——不是写好一个 prompt 就行了，而是要策划进入 LLM 上下文窗口的全部信息。在我的系统里有四个层面：
>
> 第一，工具定义：每个阶段只暴露该阶段需要的工具子集，Phase 3 的 brief 子步骤只有 3 个工具，lock 子步骤才开放航班和住宿搜索。这样 LLM 不会在收集需求阶段去搜航班。
>
> 第二，消息历史：两层渐进压缩。LLM 调用前按 token 预算压缩（先压工具结果、再压历史消息），阶段转换时用规则生成摘要（不额外调 LLM，只保留关键决策和结果）。
>
> 第三，检索知识：记忆检索按阶段过滤——Phase 1 只召回目的地和预算相关记忆，Phase 5 才加入餐饮偏好。避免无关记忆占用上下文。
>
> 第四，系统提示：运行时注入 soul + 阶段指引 + plan 快照 + 工具使用规则，让 LLM 在每一轮都知道"我在哪个阶段、应该做什么、有什么约束"。

---

## 11. 最终结论

Travel Agent Pro 已经是一个工程深度显著超越大多数 portfolio 项目的 Agent 系统。它本质上就是一个完整的 Agent Harness——编排层、上下文层、质量层、记忆层和可观测层齐备。在 2025-2026 年的 Agent 工程趋势（Harness Engineering、Context Engineering、Agent Memory 独立学科、符号-LLM 混合架构）中都找到了明确对标。

但要从 Agent 应用开发工程师简历中脱颖而出，需要同时做三件事：

**第一，强化 harness 质量层**：
当前 harness 的编排层（Agent Loop 718 行）和记忆层（memory 系统完整）是生产级的，但质量层（guardrail + validator + judge 仅 219 行）是原型级的。这个不匹配必须修复——护栏需要支持中文、验证需要前移时机、评分器需要加固。

**第二，补量化证据**：
1. 用失败案例分析证明你理解 Agent 的边界
2. 用 eval pipeline 证明规划质量
3. 用成本/延迟统计证明工程 tradeoff 可控
4. 用 demo 环境证明项目可复现

**第三，重构叙事**：
1. Harness Engineering（不是"做了护栏和验证"）——顶层框架
2. Context Engineering（不是"工具门控"）——harness 子层
3. 结构化记忆作为独立工程系统（不是"记忆功能"）——harness 子层
4. 符号-LLM 混合决策（不是"规则驱动路由"）
5. 可评估、可观测的 Agent 系统（不是"有测试"）

完成这些后，这个项目的叙事会从"我做了一个旅行 Agent"升级为：

> **我构建了一个基于 Harness Engineering 理念的复杂旅行规划 Agent 系统。Harness 涵盖编排、上下文、质量、记忆和可观测五层。它通过 Context Engineering 在每个阶段精确控制 LLM 的可见信息，通过符号-LLM 混合决策在确定性环节消除不必要的 LLM 调用，通过结构化记忆系统实现跨会话用户理解，通过质量护栏和评估体系保障规划可靠性。我能用 eval 数据证明它在哪里有效、在哪里会失败、以及每次改动对质量和成本的影响。**

这不是一个"又做了一个 Agent demo"的故事。这是一个"深入理解 2026 年 Agent Harness Engineering 并能用数据说话"的故事。
