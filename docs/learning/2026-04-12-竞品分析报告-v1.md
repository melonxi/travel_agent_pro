# Travel Agent Pro 竞争力分析、调研与升级报告

> 目标：判断当前项目离“能让 Agent 应用开发工程师面试官眼前一亮”还差什么，并给出可执行升级路线与可信简历表达。

---

## 0. 执行摘要

Travel Agent Pro 当前最强的竞争力，不是“又做了一个旅行攻略聊天机器人”，而是已经具备了复杂 Agent 系统的几个关键工程骨架：

- 自研 Agent Loop，而不是简单套 LangChain。
- 7 阶段旅行规划流程，把模糊意图、框架规划、日程详排和出发前检查拆成可控状态。
- 24+ 旅行领域工具，并带有阶段门控、读写分离并行和结构化错误反馈。
- 结构化记忆系统，支持 `global / trip` 双 scope、`trip_id` 隔离、PII 脱敏、pending 确认和 episode 归档。
- 上下文压缩、质量守护、OpenTelemetry 追踪、SSE 流式交互和较完整测试覆盖。

但如果目标是从众多 Agent 应用开发工程师简历里脱颖而出，当前最缺的不是更多 buzzword，而是“可证明能力”：

1. 这个旅行 Agent 规划质量到底如何？
2. 它在多轮约束变更下是否稳定？
3. 它什么时候会失败？
4. 它的 token 成本、延迟、工具调用是否可度量？
5. 面试官能不能一键跑起来并复现一条复杂 demo？

因此，本报告的核心判断是：

> **优先补 Agent 垂类评估闭环、可复现 demo、成本/延迟/工具调用统计和安全基线，而不是优先追 MCP、多 Agent、通用 RAG。**

MCP、RAG、多 Agent 都有价值，但应该排在“证明当前 Agent 真的擅长复杂旅行规划”之后。

---

## 0.5 可信度声明与资料边界

本报告的结论由两类证据支撑：

1. **项目内部事实**：来自当前仓库代码、文档和本地统计，包括源码行数、测试数量、模块结构、配置、记忆系统实现和 CORS 设置。这部分可信度高。
2. **外部趋势与竞品信息**：优先使用论文页面、官方协议文档、官方产品文档和厂商案例。行业文章和二手总结只作为辅助，不作为关键结论依据。

资料检索时间为 **2026-04-12**。由于 Agent 领域变化很快，本报告把外部结论分为三档：

| 可信度 | 依据类型 | 本报告使用方式 |
|--------|----------|----------------|
| 高 | 正式论文、官方文档、官方仓库、官方案例 | 可支撑优先级判断 |
| 中 | arXiv 最新论文、厂商博客、开源案例 | 可支撑趋势判断，但不当成行业定论 |
| 低 | 二手文章、排行榜总结、未验证 benchmark 摘要 | 只作背景，不直接决定路线 |

本报告中“应该优先做 eval、trace、成本统计，而不是优先追 MCP / 多 Agent / 通用 RAG”的结论，不是单纯来自某一篇论文，而是由以下事实共同推导：

- 旅行规划 benchmark 普遍强调多约束、多工具、长链路规划的失败率。
- 官方 Agent SDK 和评估平台都把 tracing、eval、guardrails、datasets 作为生产化 Agent 的核心能力。
- MCP / A2A 是互操作趋势，但它们解决的是协议连接问题，不直接证明旅行规划质量。
- 当前项目已经有较强的内部工具治理和阶段状态机，继续补“质量证据链”的边际收益最高。

---

## 1. 当前项目事实审查

### 1.1 规模与结构

以下数字来自当前仓库本地统计：

| 指标 | 当前值 |
|------|--------|
| Python 源码（含测试） | 25,451 行 |
| TypeScript / TSX | 1,851 行 |
| CSS | 1,902 行 |
| 后端测试文件 | 75 个 |
| 后端测试用例 | 543 个 |
| 后端核心模块 | agent, context, phase, state, tools, llm, memory, storage, telemetry, harness |
| 领域工具 | 24+ 个 |
| 主要外部服务 | Tavily, 小红书 CLI, FlyAI, Google Maps, Amadeus, OpenWeather, OpenTelemetry |

### 1.2 已实现亮点

| 能力 | 当前状态 | 面试价值 |
|------|----------|----------|
| 阶段化旅行规划 | 已实现 7 阶段认知流程和阶段路由 | 高。说明你理解复杂任务需要状态和流程治理 |
| 自研 Agent Loop | 已实现 LLM 调用、工具执行、阶段转换、重试、上下文重建 | 很高。比“套框架”更能展示工程理解 |
| 工具系统 | 24+ 工具，阶段门控，读写分离并行 | 高。旅行 Agent 的真实价值依赖工具编排 |
| 结构化记忆 | schema v2、global/trip、trip_id、pending、PII、episode | 很高。比普通 chat memory 更成熟 |
| 上下文压缩 | token 预算、渐进压缩、阶段转换摘要 | 高。长会话 Agent 的关键工程能力 |
| 质量守护 | 硬约束、软评分、阶段转换 gate、工具 guardrail | 高。但需要补评估数据证明效果 |
| 多 LLM 供应商 | OpenAI / Anthropic 抽象与阶段切换 | 中高。说明有 provider abstraction |
| 可观测性 | OpenTelemetry + Jaeger | 中高。已有 trace 基础 |
| 前端交互 | SSE、工具卡片、地图、Phase3 工作台 | 中高。适合 demo 展示 |
| 测试覆盖 | 543 个测试用例 | 高。能支撑可信度 |

### 1.3 当前真实短板

| 短板 | 严重度 | 真实影响 |
|------|--------|----------|
| 缺少 Agent 自动化评估体系 | 高 | 无法证明规划质量是否变好 |
| 缺少 golden cases / pass@k | 高 | 无法回答“稳定性如何” |
| 缺少成本、延迟、token、工具调用统计 | 高 | 无法回答“线上成本如何控制” |
| 缺少完整 CI/CD | 高 | 无法形成质量闭环 |
| 缺少可复现 demo 环境 | 高 | 面试或作品集演示成本高 |
| `main.py` 过大 | 中高 | 长期维护困难，影响继续升级 |
| 安全基线不足 | 中高 | CORS 当前过宽，缺少限流和请求约束 |
| 结构化日志不足 | 中 | 排查复杂 Agent 失败时不够方便 |
| 前端缺少 Agent 执行链路可视化 | 中 | 演示说服力不足 |
| RAG / 知识库缺失 | 中 | 对签证、交通规则、区域知识等长期知识支持不足 |
| MCP / A2A 兼容缺失 | 中低 | 前沿协议展示不足，但不是当前最关键瓶颈 |

---

## 2. 垂类 Agent 难点：旅行规划为什么难

旅行规划 Agent 的难点不是生成一篇“看起来像攻略”的文本，而是持续满足多个现实约束：

- 日期、天数、预算、人数、出发地、目的地。
- 航班、火车、住宿、景点开放时间、天气和地理距离。
- 用户偏好、同行人限制、老人/儿童/无障碍需求。
- 多轮变更：预算降低、目的地改动、否定酒店、回退规划阶段。
- 工具使用正确性：该查航班时查航班，该查天气时查天气，不能靠模型编。
- 约束优先级：硬约束不能牺牲，软偏好可以权衡。

这意味着旅行 Agent 的竞争力应该用“约束满足率、工具选择正确率、多轮稳定性、成本和延迟”来证明，而不只是看 UI 或回复质量。

---

## 3. 当前先进实现与研究趋势

### 3.1 TravelPlanner：真实旅行规划 benchmark

**可信度：高。**

TravelPlanner 是 ICML 2024 的旅行规划 benchmark，目标是评估语言 Agent 在真实旅行规划场景中的计划能力。它提供工具可访问的数据环境，并包含 1,225 个规划意图与参考方案。该工作的重要结论是：真实旅行规划对现有语言 Agent 很难，失败点集中在多约束跟踪、工具使用、可行性和任务一致性上；论文和项目资料中报告 GPT-4 在严格设置下成功率很低。

参考：

- TravelPlanner paper: https://proceedings.mlr.press/v235/xie24j.html
- TravelPlanner GitHub: https://github.com/OSU-NLP-Group/TravelPlanner

对本项目的启发：

- 面试时最有说服力的是“我知道旅行规划 Agent 为什么难，并用评估体系证明我的系统改进了哪些失败点”。
- 当前项目已有状态机、工具门控、质量守护，正好可以围绕 TravelPlanner 风格的约束做评估。

### 3.2 Flex-TravelPlanner：多轮变化与约束优先级

**可信度：中。**

Flex-TravelPlanner 是 2025 年 arXiv 论文，关注动态、多轮、优先级约束场景。它指出真实规划不是一次性输入需求，而是用户不断添加、修改、撤销条件。其摘要结论包括：单轮任务表现不能可靠预测多轮适应能力；约束引入顺序会显著影响模型表现；模型容易错误偏向新出现但优先级较低的偏好。

参考：

- Flex-TravelPlanner: https://arxiv.org/abs/2506.04649

对本项目的启发：

- 当前项目的 backtrack、phase router、trip_id 轮转很有价值，但还缺少多轮约束变更 eval。
- 应该把“用户中途改变预算、改目的地、否定某方案、要求回到前一阶段”做成 golden cases。

### 3.3 ATLAS：约束管理、计划批判、交错搜索

**可信度：中。**

ATLAS 是 2025 年 arXiv 论文，面向真实旅行规划提出约束感知多 Agent 协作框架。它强调动态约束管理、迭代计划 critique 和 adaptive interleaved search。论文摘要报告其在 TravelPlanner benchmark 上提升了 final pass rate。这里最值得借鉴的不是“多 Agent”标签，而是把约束维护、检索、批判和修正拆成明确职责。

参考：

- ATLAS: https://arxiv.org/abs/2509.25586

对本项目的启发：

- 不需要立刻把 7 个阶段全部改成 sub-agent。
- 更现实的升级是先把 critic / validator / researcher 做成独立可测试组件。
- Phase 5 的日程详排应该重点强化“约束检查 + 失败修复”。

### 3.4 HiMAP-Travel：层级多 Agent 与并行日级规划

**可信度：中。**

HiMAP-Travel 是 2026 年 3 月提交的 arXiv 论文，关注长程约束旅行规划。其核心思想是把规划拆成 strategic coordination 和 parallel day-level execution：Coordinator 做跨天资源分配，Day Executors 并行规划每日行程，并用 transactional monitor 约束预算和唯一性。摘要报告该方法在 TravelPlanner 和 FlexTravelBench 上取得更高 Final Pass Rate，并通过并行化降低延迟。

参考：

- HiMAP-Travel: https://arxiv.org/abs/2603.04750

对本项目的启发：

- Phase 5 可以考虑“天级规划”并行化，但前提是先有跨天预算、地点唯一性和全局约束 monitor。
- 与其直接重构成多 Agent，不如先把日级 plan assembly 和 cross-day validator 做成清晰边界。
- 如果未来做多 Agent，应该围绕“全局协调器 + 每日执行器 + 事务式约束监控”设计，而不是简单按 7 个阶段拆 Agent。

### 3.5 DocentPro / LangGraph 类案例：模块化 agent 与可观测工作流

**可信度：中。**

DocentPro 的公开案例展示了多 Agent 旅行陪伴产品如何用 LangGraph 把不同主题交给不同 agent chain，并结合 RAG、翻译、TTS 等能力。

参考：

- DocentPro case study: https://www.langchain.com/blog/customers-docentpro

对本项目的启发：

- 模块化和可观测性很重要。
- 但本项目已有自研 Agent Loop，没必要为了“看起来先进”强行换框架。
- 更适合展示的是：自研循环如何获得更精确的阶段门控、上下文压缩和 trace。

### 3.6 OpenAI Agents SDK / LangSmith / OpenAI Evals：评估和 tracing 已成工程标配

**可信度：高。**

OpenAI Agents SDK 的官方文档把 agents、handoffs、guardrails 和 tracing 作为核心原语，并说明 tracing 会记录 LLM generations、tool calls、handoffs、guardrails 和 custom events。LangSmith 官方文档也把 datasets、evaluators、experiments、online/offline evaluation 作为应用质量闭环。OpenAI Evals API 也提供 eval、grader 和 datasource 管理能力。

参考：

- OpenAI Agents SDK: https://openai.github.io/openai-agents-python/
- OpenAI Agents SDK tracing: https://openai.github.io/openai-agents-python/tracing/
- LangSmith Evaluation: https://docs.langchain.com/langsmith/evaluation
- OpenAI Evals API: https://platform.openai.com/docs/api-reference/evals/getRuns

对本项目的启发：

- 当前已有 OpenTelemetry + Jaeger 是优势，但需要把 trace 与 Agent 语义绑定：LLM call、tool call、state diff、memory hit、validator result。
- 当前已有测试和 harness，但缺 datasets / golden cases / experiments 这一层。
- 报告中的 P0 优先级“eval + trace + 成本统计”与主流 Agent 工程产品方向一致。

### 3.7 MCP / A2A：协议互操作是趋势，但不是第一瓶颈

**可信度：高。**

MCP 官方文档将 tools、resources、prompts 等作为服务器暴露给客户端的核心能力。Google A2A 官方文档和公告明确把 A2A 定位为 Agent 间通信协作协议，并说明 A2A 与 MCP 是互补关系：MCP 更偏 Agent 到工具/资源，A2A 更偏 Agent 到 Agent。

参考：

- MCP resources: https://modelcontextprotocol.io/docs/concepts/resources
- MCP Inspector: https://modelcontextprotocol.io/docs/tools
- Google A2A announcement: https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/
- A2A and MCP: https://google-a2a.github.io/A2A/latest/topics/a2a-and-mcp/

协议兼容对工程视野有加分，但对当前项目来说，不应优先于评估、成本、可复现 demo 和核心规划质量。

对本项目的建议：

- P2 做一个轻量 MCP adapter，先暴露 3-5 个代表性工具即可。
- 不要为了协议改造牺牲已有工具门控和阶段治理。

### 3.8 外部调研对路线的直接结论

| 外部证据 | 说明 | 对本项目的路线影响 |
|----------|------|--------------------|
| TravelPlanner | 严格旅行规划 benchmark 暴露多约束和工具使用失败 | 优先做垂类 eval，而不是只做功能堆叠 |
| Flex-TravelPlanner | 多轮动态约束比单轮更难 | golden cases 必须包含回退和变更 |
| ATLAS | 约束管理、critique、交错搜索能提升规划 | 先组件化 critic / validator / researcher |
| HiMAP-Travel | 层级协调和日级并行适合长程规划 | Phase 5 后续可做 day-level planner + global monitor |
| OpenAI Agents SDK / LangSmith / OpenAI Evals | tracing、guardrails、datasets、experiments 是 Agent 工程基础设施 | P0 补 eval、trace、成本统计是合理优先级 |
| MCP / A2A | 协议互操作是趋势，但解决的是连接问题 | P2 做轻量 adapter，不作为当前核心卖点 |

---

## 4. 和旧报告不同的判断

### 4.1 P0 第一优先级应是垂类评估，而不是 Docker / JWT

旧报告把 Docker、CI/CD、JWT、安全加固和 Agent 评估都放 P0。我的判断是：

1. Agent eval harness 是第一优先级。
2. CI 应先服务于 test + smoke eval。
3. Docker 或一键启动是为了 demo 可复现。
4. JWT 不是个人作品集的第一优先级，安全基线更实际。

原因：面试官最难被“又一个 Agent demo”打动，但很容易被“我能量化证明这个 Agent 在复杂旅行规划约束上的表现”打动。

### 4.2 MCP 不应被写进当前简历

旧报告前面说项目缺少 MCP，后面简历却写“工具系统兼容 MCP 协议”。这是高风险表述。

当前应写：

> 工具系统采用自研 `@tool` 注册、阶段门控和读写分离调度。

完成 MCP adapter 后才可以写：

> 为核心旅行工具实现 MCP adapter，支持标准协议暴露和外部 Agent 调用。

### 4.3 多 Agent 不是当前最短路径

多 Agent 可以加分，但 Travel Agent Pro 当前最大优势是阶段状态机和强工具治理。直接拆成多 Agent 会增加复杂度，未必提高质量。

更好的路径：

- 先拆 critic / validator / researcher 组件。
- 每个组件有明确输入输出和测试。
- 再考虑 Orchestrator + specialist agents。

### 4.4 RAG 应该做成旅行约束知识库，而不是通用知识库

旧报告说“目的地百科、签证政策、价格趋势”。我会改为：

- 签证、交通卡、机场到市区、区域关系、闭馆日、季节风险、亲子/老人/无障碍限制。
- RAG 结果不仅进入 prompt，还要进入 validator 或 planning constraints。
- 必须有引用、时效性和召回质量评估。

否则 RAG 只是多一个搜索入口，面试价值有限。

### 4.5 记忆系统应被更重点包装

当前记忆系统比很多 demo 项目成熟。下一步不是马上做向量记忆，而是补：

- 用户可见的 memory center。
- 每条记忆显示来源、置信度、scope、状态。
- Agent 回复能说明“本轮使用了哪些记忆”。
- memory eval：错误记忆率、过度泛化率、trip 污染率。

这会把已有优势变成可展示、可解释、可评估的亮点。

### 4.6 `main.py` 拆分是工程治理，不是简历核心亮点

`main.py` 过大确实需要拆，但不应在简历里写“拆分 main.py”。简历要写结果：

> 将 chat orchestration、memory extraction、session lifecycle 解耦为可测试服务，支撑 CI 中的 agent regression eval。

---

## 5. 差距矩阵

| 差距 | 当前状态 | 面试官可能追问 | 最小可行改进 | 完成后怎么证明 |
|------|----------|----------------|--------------|----------------|
| Agent 质量不可量化 | 有单元测试和 harness，但无 batch eval | “你怎么知道 Agent 变好了？” | 建 20-50 个 golden cases | eval 报告、趋势图、CI smoke eval |
| 多轮变更稳定性未知 | 有 backtrack 机制，但缺少评估 | “用户中途改需求怎么办？” | 多轮约束变更测试集 | pass@k、约束通过率 |
| 工具选择质量不可见 | 有工具门控，但缺指标 | “工具调用错了怎么办？” | 记录 expected tools / actual tools | tool precision / recall |
| 成本不可见 | 有压缩和模型切换，但无统计 | “成本如何控制？” | 记录 token、模型、耗时、工具次数 | session cost report |
| Demo 不够可复现 | 有 dev 脚本，但缺标准 demo 数据 | “我怎么跑？” | seed data + demo script + 一键启动 | README 和录屏 |
| 安全基线不足 | CORS 宽，缺限流 | “公开部署安全吗？” | CORS 白名单、限流、长度限制、timeout | 安全配置和测试 |
| 记忆不可解释 | 后端机制成熟，前端展示不足 | “用户怎么控制记忆？” | memory center + 来源展示 | UI demo |
| 长链路调试体验不足 | 有 Jaeger，但前端不可视 | “失败怎么定位？” | trace timeline / waterfall | 一条失败案例复盘 |
| RAG 缺失 | 靠搜索和工具 | “知识怎么管理？” | 旅行约束 KB | retrieval eval |
| MCP 缺失 | 自研工具协议 | “能否接标准生态？” | 轻量 MCP adapter | 3-5 个工具可被 MCP 调用 |

---

## 6. 改进路线

### P0：让项目可信，优先 1-2 周

#### 6.1 Agent 垂类评估体系

目标：回答“这个 Agent 到底好在哪里”。

最小实现：

- `evals/golden_cases/*.yaml`
- 20 个基础 case，覆盖 Phase 1、Phase 3、Phase 5、回退和 Phase 7。
- 每个 case 定义：
  - 用户多轮输入
  - 期望阶段变化
  - 必须写入的 state 字段
  - 禁止写入的字段
  - 必须调用或禁止调用的工具
  - 硬约束断言
- 输出 JSON / Markdown eval report。

建议指标：

| 指标 | 含义 |
|------|------|
| task_completion | 是否完成目标阶段 |
| hard_constraint_pass | 日期、预算、天数、路线、开放时间是否满足 |
| tool_selection_accuracy | 工具调用是否符合预期 |
| state_write_accuracy | 状态写入是否准确 |
| backtrack_success | 回退是否正确清理下游状态 |
| memory_safety | 是否错误记忆或污染 scope |
| latency_ms | 端到端耗时 |
| token_usage | 输入/输出 token |
| estimated_cost | 估算成本 |

#### 6.2 CI smoke eval

目标：每次变更至少能证明没有破坏主链路。

最小实现：

- GitHub Actions 或本地等价脚本。
- 跑单元测试。
- 跑 3-5 个 smoke golden cases。
- 上传 eval summary。

#### 6.3 可复现 demo

目标：面试官或自己演示时不依赖临场发挥。

最小实现：

- 一键启动说明。
- seed session / seed memory。
- 3 条 demo 脚本：
  - 模糊需求到目的地收敛。
  - Phase 3 框架规划和骨架选择。
  - Phase 5 日程详排 + 用户中途回退。
- Demo 录屏或 GIF。

#### 6.4 成本、延迟、工具调用统计

目标：把 Agent 工程 tradeoff 讲清楚。

最小实现：

- 每次 LLM 调用记录 provider、model、input_tokens、output_tokens、duration。
- 每次工具调用记录 tool_name、duration、status、error_code。
- 每个 session 汇总成本、延迟和工具调用次数。

#### 6.5 安全基线

目标：能公开 demo，不留下明显安全问题。

最小实现：

- CORS 白名单。
- 请求体长度限制。
- 每 IP / 每 session 限流。
- 工具 timeout。
- 外部 URL / 搜索输入的基础校验。
- API key 只走环境变量。

### P1：放大现有差异化，2-4 周

#### 6.6 Memory Center 与记忆可解释

把后端已有记忆能力展示出来：

- 前端展示 active / pending / rejected / obsolete。
- 每条记忆展示 source quote、confidence、scope、domain、trip_id。
- 支持确认、拒绝、删除。
- Agent 回复或 debug 面板显示本轮命中的记忆。

#### 6.7 Phase 5 约束验证增强

旅行规划最核心的质量点在 Phase 5：

- 景点开放时间。
- 活动间交通时间。
- 每日节奏和地理聚类。
- 预算累计。
- 天气风险。
- 老人/儿童/无障碍约束。

建议把这些变成可测试 validator，而不是只靠 prompt。

#### 6.8 Agent trace viewer

前端展示一轮规划的执行链路：

- LLM 调用。
- 工具调用 waterfall。
- state diff。
- memory hit。
- validator / judge 结果。
- context compression 事件。

这会显著提升面试 demo 的可解释性。

#### 6.9 服务层拆分

建议拆分：

- `routers/chat.py`
- `routers/session.py`
- `routers/memory.py`
- `services/memory_extraction.py`
- `services/session_lifecycle.py`
- `services/event_stream.py`
- `dependencies.py`

目标不是“文件更小”，而是让 eval、trace、安全和 memory 逻辑更容易测试。

### P2：先进架构展示，时间充裕再做

#### 6.10 旅行约束知识库 / RAG

不要做泛泛的百科 RAG。优先做：

- 签证规则。
- 城市交通卡和机场交通。
- 区域关系。
- 景点闭馆日。
- 季节风险。
- 亲子、老人、无障碍提示。

要求：

- 有来源引用。
- 有过期时间或刷新策略。
- 检索结果能进入 validator 或 constraints。
- 有 retrieval eval。

#### 6.11 MCP adapter

只做轻量兼容：

- 暴露 `web_search`、`get_poi_info`、`calculate_route`、`check_weather`、`search_accommodations`。
- 保留现有 `@tool` 系统作为内部工具治理层。
- adapter 只负责协议转换。

#### 6.12 Specialist critic / researcher

在现有 Agent Loop 上增加 specialist，而不是重写成复杂多 Agent：

- Researcher：负责候选信息收集。
- Critic：负责约束检查和方案批判。
- Repairer：负责根据 validator 错误修复方案。

每个 specialist 必须有明确输入输出和 eval case。

---

## 7. 不建议优先做的事

| 不建议优先项 | 原因 |
|--------------|------|
| 全量 MCP 化 | 工具系统已有治理能力，协议不是第一瓶颈 |
| 直接改成多 Agent | 容易增加复杂度，不一定提升规划质量 |
| 泛泛做目的地百科 RAG | 与 web search 重叠，面试说服力有限 |
| 完整账号系统 / 重 JWT | 作品集阶段成本高，安全基线更实用 |
| 大规模 UI 重设计 | 当前短板主要在可证明能力，不是视觉 |

---

## 8. 当前可写入简历的项目介绍

以下版本只包含当前已经实现或有代码支撑的能力。

**Travel Agent Pro — 基于 LLM 的智能旅行规划 Agent 系统**  
独立设计与开发 | Python, FastAPI, React, TypeScript

- 自研 Agent Loop 与阶段路由机制，将旅行规划拆解为 7 阶段认知流程，支持阶段状态管理、回退、快照恢复和 SSE 流式交互。
- 构建 24+ 旅行领域工具系统，覆盖目的地搜索、UGC 检索、航班/火车/住宿、POI、路线、天气、可行性检查和方案摘要，并支持阶段级工具门控与读写分离并行调度。
- 实现结构化记忆系统，支持 `global / trip` 双 scope、`trip_id` 隔离、pending 确认、PII 脱敏、阶段相关三路检索和行程 episode 归档。
- 设计上下文压缩机制，在 LLM 调用前按 token 预算压缩历史消息和工具结果，降低长会话上下文压力。
- 实现质量守护层，包含硬约束校验、软评分、阶段转换质量门控和工具输入输出 guardrails。
- 接入 OpenTelemetry + Jaeger 追踪 Agent Loop、工具调用和阶段转换，提升复杂执行链路的可观测性。
- 使用 React + TypeScript 构建旅行规划工作台，支持聊天流、工具结果卡片、阶段进度、地图和日程可视化。
- 后端包含 75 个测试文件、543 个测试用例，覆盖 Agent、工具、状态、记忆、压缩和质量守护模块。

英文版：

**Travel Agent Pro — LLM-Powered Travel Planning Agent System**  
Sole designer & developer | Python, FastAPI, React, TypeScript

- Built a custom Agent Loop and phase router that decomposes travel planning into a 7-phase cognitive workflow with state management, backtracking, snapshot recovery, and SSE streaming.
- Implemented a 24+ tool travel domain system covering destination discovery, UGC search, flights, trains, accommodations, POIs, routes, weather, feasibility checks, and plan summaries, with phase-level tool gating and read/write parallel dispatch.
- Designed a structured memory system with `global / trip` scopes, `trip_id` isolation, pending confirmation, PII redaction, phase-aware retrieval, and trip episode archiving.
- Engineered context compaction around token budgets to reduce long-session prompt pressure before LLM calls.
- Added a quality guardrail layer with hard-constraint validation, soft scoring, phase-transition gates, and tool input/output checks.
- Integrated OpenTelemetry and Jaeger to trace Agent Loop execution, tool calls, and phase transitions.
- Built a React + TypeScript planning workbench with streaming chat, tool result cards, phase progress, maps, and itinerary visualization.
- Maintained 75 backend test files and 543 test cases across agent orchestration, tools, state, memory, compaction, and guardrails.

---

## 9. 完成升级后可写入简历的目标版

以下内容必须完成对应实现后再使用。

**目标版新增亮点：**

- 建立旅行规划 golden-case eval pipeline，覆盖多轮约束变更、预算、路线、开放时间、工具选择和回退场景，并在 CI 中运行 smoke eval。
- 实现 pass@k 稳定性评估，持续追踪任务完成率、硬约束通过率、工具选择准确率和状态写入准确率。
- 增加会话级 token、成本、延迟和工具调用统计，支持 Agent 行为回归分析和成本优化。
- 提供可复现 demo 环境、seed data 和 trace viewer，展示每轮规划的工具调用、状态变化、记忆命中和质量评分。
- 增强旅行约束知识库，将签证、交通、开放时间、季节风险等信息接入 validator 和 planner。
- 为核心旅行工具提供 MCP adapter，展示标准协议兼容能力，同时保留内部阶段门控和工具治理设计。

目标版简历条目示例：

- 设计旅行规划 Agent 评估体系，覆盖 30+ golden cases 和多轮约束变更场景，在 CI 中追踪任务完成率、硬约束通过率、工具选择准确率、pass@k 稳定性、token 成本和端到端延迟。
- 构建 Agent trace viewer，将 LLM 调用、工具 waterfall、state diff、memory hit、validator 结果和 context compression 事件可视化，用于复盘失败案例和定位规划质量回归。
- 引入旅行约束知识库，把签证、交通、开放时间、季节风险等高时效规则接入 planner 和 validator，并通过 retrieval eval 评估召回质量。

---

## 10. 面试话术建议

### 10.1 “为什么不用 LangChain？”

可以这样说：

> 这个项目的重点不是通用聊天，而是阶段化旅行规划。我需要精确控制阶段转换、工具门控、状态写入、上下文压缩和回退恢复。自研 Agent Loop 的好处是每个关键决策点都可观察、可测试、可拦截；代价是要自己维护循环和工具协议，但换来的是更适合垂类规划任务的控制力。

### 10.2 “怎么评估 Agent 质量？”

当前诚实回答：

> 目前项目已有运行时质量守护，包括硬约束校验、软评分、阶段转换 gate 和工具 guardrails。下一步我会把这些运行时检查固化为 golden-case eval pipeline，覆盖多轮旅行规划场景，追踪任务完成率、硬约束通过率、工具选择准确率、pass@k 稳定性、成本和延迟。

完成升级后回答：

> 我建立了一套旅行规划 golden-case eval。每个 case 包含多轮用户输入、期望阶段变化、状态断言、工具调用断言和硬约束检查。CI 会跑 smoke eval，完整 eval 会统计任务完成率、硬约束通过率、工具选择准确率、状态写入准确率、pass@k 稳定性、token 成本和延迟。

### 10.3 “记忆系统怎么设计？”

可以这样说：

> 记忆系统采用结构化 item，而不是简单 chat summary。检索分三路：core profile 是长期 global 用户画像，trip memory 只取当前 `trip_id` 下的本次旅行记忆，phase-domain 会按当前阶段过滤相关历史。写入是后台异步抽取，先生成候选，再经 policy 判断风险；低风险高置信可自动保存，高风险或冲突进入 pending，用户确认后才变 active。所有 PII 会在候选和存储阶段检测或脱敏，新行程回退时会轮转 `trip_id`，避免旧 trip 记忆污染新行程。

### 10.4 “怎么控制成本？”

当前诚实回答：

> 目前已有上下文压缩和按阶段切换模型，能减少长会话上下文压力。下一步会补会话级 token、延迟、工具调用和成本统计，这样才能定量比较不同 prompt、模型和工具策略的成本收益。

完成升级后回答：

> 我在每次 LLM 调用和工具调用上记录 token、模型、耗时、状态和错误码，并按 session 汇总成本。结合 eval pipeline，可以看到一次 prompt 或工具策略变更对质量、成本和延迟的影响。

---

## 11. 最终结论

Travel Agent Pro 已经不是普通 demo 型 Agent 项目。它的阶段状态机、自研 Agent Loop、结构化记忆、工具治理、上下文压缩和质量守护，已经具备较强的工程深度。

但要真正从 Agent 应用开发工程师简历中脱颖而出，下一步不要优先堆功能标签，而要补“证据链”：

1. 用 eval 证明规划质量。
2. 用 trace 证明执行过程可解释。
3. 用成本/延迟统计证明工程 tradeoff 可控。
4. 用 demo 环境证明项目可复现。
5. 用 memory UI 和 memory eval 放大已有记忆系统优势。

完成这些后，这个项目的叙事会从“我做了一个旅行 Agent”升级为：

> **我构建了一个可评估、可观测、可回退、可记忆、可约束验证的复杂旅行规划 Agent 系统，并能用数据证明每次架构改动对规划质量、成本和稳定性的影响。**
