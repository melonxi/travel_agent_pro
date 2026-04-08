# Travel Agent Pro — 面试官视角的深度分析与演进蓝图

> 本文档基于对当前项目源码的深度审计 + 2025-2026 年 Agent 系统工程最佳实践研究编写。
>
> **核心叙事框架**：不是"我缺什么"，而是"我在当前阶段选择优化什么、有意识地推迟什么、以及在什么触发条件下演进"。

---

## 一、项目定位与设计目标

### 1.1 这个项目优化了什么

Travel Agent Pro 是一个 **全栈 AI 旅行规划系统**，设计目标明确：

| 优化维度 | 选择 | 理由 |
|---------|------|------|
| 深度理解 vs 快速原型 | 🔧 **从零手写 Agent Loop**（非 LangChain） | 理解 agent 核心机制比依赖框架更重要 |
| 认知建模 vs 自由对话 | 🧠 **7 阶段认知决策流** + 显式状态机 | 旅行规划是有结构的，确定性阶段转换比 LLM 自述进度更可靠 |
| 可观测性 vs 最小实现 | 🔭 **OpenTelemetry + Jaeger 全链路追踪** | Agent 系统必须可调试，这不是事后添加 |
| 测试覆盖 vs 快速迭代 | 🧪 **375+ 后端测试** | 非确定性系统更需要确定性的测试保护网 |
| SSE 流式 vs 轮询 | 📡 **SSE 实时传输** | 旅行规划对话长，用户需要实时看到 agent 的思考和工具调用过程 |

### 1.2 当前运行边界（诚实声明）

| 边界 | 当前状态 | 有意推迟的原因 |
|------|---------|--------------|
| 用户规模 | 单用户本地开发/演示 | MVP 阶段聚焦 agent 核心机制 |
| 状态持久化 | 文件系统 JSON | 足够验证 backtrack 和快照逻辑 |
| 认证/鉴权 | 无 | 单用户场景不需要 |
| 并发模型 | 单 session 顺序处理 | Agent 对话本质上是串行的 |
| SSE 容错 | 无自动重连 | 本地网络可靠，问题在扩展阶段才显现 |

> **面试话术要点**：每个"缺失"都有对应的推迟理由。面试官想听的不是"我没想到"，而是"我知道这个需要，但在当前阶段有意识地推迟了"。

### 1.3 面试官的第一反应

> **"这个候选人不是调包侠。"**

面试官看到手写 Agent Loop，第一个正面信号是：候选人选择了 **hard mode**。这意味着候选人理解 Think-Act-Observe 循环的本质，而不是停留在 `langchain.agents.create_tool_calling_agent()` 的层面。

但紧接着，面试官会追问三个关键方向：

| 面试官关注点 | 他想确认你知道什么 |
|---|---|
| "agent 跑飞了怎么办？" | 你是否理解 **circuit breaker、token budget、max iterations** |
| "工具调用失败怎么办？" | 你是否理解 **重试策略、降级方案、结构化错误传递** |
| "怎么知道 agent 的输出质量好不好？" | 你是否理解 **eval harness 与单元测试的区别** |

---

## 二、核心工程亮点 & 设计 Trade-off

### 2.1 五个打动面试官的工程亮点

#### 亮点 A: Agent Loop 的工程深度（agent/loop.py — 667 行）

这不是一个 toy loop。它包含五个值得展开的工程决策：

- **Payload compaction**：web_search、xiaohongshu 等富结果截断至 220 char/snippet。完整数据推到前端展示，压缩副本进 LLM 上下文 —— 一份数据两种用途。
- **冗余状态更新检测**（`_should_skip_redundant_update`）：LLM 经常重复写入相同状态，检测后跳过可节省 1-2 次无用 tool call。
- **阶段转换双重检测**：plan.phase 直变 + `phase_router.check_and_apply_transition()` 后置校验。
- **Backtrack 时的消息重建**：保留原始用户意图 + 规则压缩历史为结构化决策记录。
- **Keepalive ping**：长工具执行时发送心跳，防 SSE 超时断连。

#### 亮点 B: 确定性阶段转换 —— 关键 Trade-off

> **设计决策**：PhaseRouter 用状态完整度推断阶段（规则驱动），而非让 LLM 自述进度。

这是项目中最重要的 trade-off：

| | 规则驱动（当前选择） | LLM 驱动 |
|---|---|---|
| 优点 | 可预测、可测试、零额外 token | 更灵活、能处理边界情况 |
| 缺点 | 规则覆盖不全时会卡住 | LLM 对自身进度判断不可靠 |
| 适用场景 | 阶段边界清晰的结构化任务 | 边界模糊的开放式任务 |

**面试话术**: *"我选择确定性阶段转换，是因为旅行规划的阶段边界是明确的 —— 有没有目的地、有没有日期、有没有行程骨架。不确定的部分（工具选择、对话生成）交给 LLM，确定的部分用规则保证。"*

#### 亮点 C: 4 层上下文组装 + 压缩迭代故事

上下文管理体现了 prompt engineering 功底：
1. **Soul 层**：`soul.md` 永久人格约束
2. **Time 层**：当前日期时间 + 时区
3. **Phase Prompt 层**：500+ 行阶段专属指令
4. **Runtime State 层**：计划状态 + 可用工具 + 用户偏好

**🔥 一个真实的迭代故事（面试时讲这个）**：

> "上下文压缩我经历了一次重大重构。V1 用 LLM 调用做对话摘要，发现三个问题：（1）每次压缩额外花 2-3 秒延迟；（2）摘要质量不稳定，有时丢失关键偏好；（3）压缩本身消耗 token，讽刺地加剧了 context window 压力。
>
> V2 改为规则驱动：标记含偏好信号的消息为 must_keep，其他消息提取为结构化决策记录。压缩变成零成本、确定性操作。通过 Jaeger trace 对比两个版本，V2 的端到端延迟降低了 20%+，且用户关键意图保留率更高。"

这个故事同时展示了：迭代能力、可观测性驱动的决策、trade-off 分析。

#### 亮点 D: 可观测性是 Day 1 设计

OpenTelemetry 集成到每个关键路径（9 个 telemetry 测试文件验证）：
- Agent loop iterations 带 phase 属性
- 每次 LLM 调用记录 provider/model/token 数
- 每次工具执行记录 input/output/status/error_code
- 阶段转换记录 from_phase/to_phase + plan snapshot
- 上下文压缩决策事件

**这不是装饰。** 上面的压缩重构故事就是靠 trace 数据支撑的。

#### 亮点 E: 测试工程的深度

375+ 测试不是堆数量，而是分层覆盖：

| 测试层 | 代表文件 | 行数 | 验证什么 |
|--------|---------|------|---------|
| 核心循环 | test_agent_loop.py | 1025 | Think-Act-Observe 完整周期 |
| 阶段集成 | test_phase_integration.py | 696 | 各阶段工具链正确性 |
| 错误路径 | test_error_paths.py | 411 | 缺失 session、预算超标、时间冲突 |
| 遥测验证 | 9 个 telemetry 测试 | 500+ | Span 属性和事件结构正确 |
| 端到端 | test_e2e_golden_path.py | 333 | Phase 1 → 多阶段完整对话 |

### 2.2 关键约束与已知边界

> 以下不是"缺陷清单"，而是"当前阶段有意识的 scope boundary"。面试时主动说出这些，比被面试官问出来强 10 倍。

| 约束 | 当前状态 | 推迟原因 | 什么时候必须解决 |
|------|---------|---------|----------------|
| **Token Budget** | token 估算 `len//3`，无每次运行预算 | MVP 阶段单用户调试，成本不敏感 | 当部署给多用户 / 接入计费时 |
| **Tool Timeout / Circuit Breaker** | 工具执行无超时、无熔断 | 本地开发环境外部 API 稳定 | 当接入真实第三方 API（航班/酒店） |
| **Eval Harness** | 有 validator + judge，无系统化评估 | 初期聚焦功能完备，评估是下一阶段 | 当 prompt 迭代频繁、需要量化质量变化时 |
| **Input/Output Guardrails** | soul.md 定义约束，无代码级执行 | 单用户信任场景 | 当面向公众用户 |
| **LLM 重试 / 回退** | 无指数退避，无 provider 切换 | OpenAI SDK 内置基础重试 | 当可用性 SLA 要求 > 99% |
| **状态持久化** | 文件系统 JSON | 足够验证 backtrack + snapshot 逻辑 | 当并发 session > 100 |
| **Human-in-the-Loop** | 无审批门 | 当前无真实交易操作 | 当涉及支付/预订确认 |
| **认证/速率限制** | 无 | 单用户本地开发 | 当部署为 web 服务 |

**面试话术**: *"这些我都清楚需要做。之所以当前没做，不是不知道，而是在 MVP 阶段我选择把精力投入到 agent 的核心认知机制上 —— 阶段路由、上下文管理、可观测性。当从 demo 到 production 时，第一批要加的是 token budget、tool hardening 和 eval harness。"*

---

## 三、工程成熟度评估：哪些说明深度，哪些说明下一阶段需求

基于 2025-2026 年 production agent 架构研究（EkaivaKriti 7 层架构、Fordel Studios 生产模式、iBuidl 失败模式、AgentPatterns Eval Harness）：

| 维度 | 当前成熟度 | 说明了什么 | 下一阶段触发条件 |
|------|-----------|----------|----------------|
| **Agent Loop** | ⭐⭐⭐⭐ | 深入理解 agent 核心机制 | 多用户 → 加 token budget + 降级路径 |
| **工具系统** | ⭐⭐⭐⭐ | 12 工具 + 阶段路由 + schema 自动生成 | 真实 API → 加 timeout + circuit breaker |
| **阶段路由** | ⭐⭐⭐⭐ | 状态机思维，确定性 > 灵活性 | 复杂场景 → 考虑混合（规则 + LLM 判断） |
| **上下文管理** | ⭐⭐⭐⭐ | 4 层组装 + 规则压缩 + 偏好保留 | 长对话 → 精确 token 计数 + 任务闭合标记 |
| **可观测性** | ⭐⭐⭐⭐ | Day 1 集成 OTel，不是事后添加 | 多用户 → 加 Prometheus metrics + 业务 KPI |
| **测试工程** | ⭐⭐⭐⭐ | 375+ 测试，分层覆盖 | Prompt 迭代加速 → 加 eval harness |
| **错误处理** | ⭐⭐ | 基础 try-catch，依赖 SDK 重试 | SLA 要求 → 指数退避 + 回退 provider |
| **Guardrails** | ⭐⭐ | soul.md 定义约束，无代码执行 | 公众用户 → input/output/action 三层 |
| **记忆系统** | ⭐⭐ | 跨 session JSON 偏好 | 个性化深度 → 分层 + 衰减 + 反馈循环 |
| **状态持久化** | ⭐⭐ | 文件系统 + 快照 | 并发 > 100 → SQLite/PostgreSQL |
| **成本控制** | ⭐ | 无 | 任何计费场景 → token budget + 模型分级 |
| **安全** | ⭐ | 无认证/限流 | Web 部署 → auth + rate limit |

---

## 四、面向不同产品阶段的演进蓝图

> 不是一个"究极目标"，而是按场景触发的条件演进。每项改进都绑定具体的失败模式和预期收益。

### 4.1 演进后的架构总览

```
                    ┌─────────────────────────────────────┐
                    │         Guardrail Gateway            │
                    │  Input Validation · Rate Limit · Auth│
                    └──────────────┬──────────────────────┘
                                   │
                    ┌──────────────▼──────────────────────┐
                    │         Agent Orchestrator           │
                    │  State Machine · Token Budget ·      │
                    │  Circuit Breaker · Cost Tracker      │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
    ┌─────────▼────────┐ ┌────────▼────────┐ ┌────────▼────────┐
    │  Planning Engine  │ │  Execution Engine│ │  Evaluation     │
    │  Phase Router     │ │  Tool Dispatcher │ │  Engine         │
    │  Context Manager  │ │  w/ Timeout      │ │  Harness +      │
    │  Memory Manager   │ │  Circuit Breaker │ │  Benchmark      │
    └──────────────────┘ └─────────────────┘ └─────────────────┘
              │                    │                    │
    ┌─────────▼────────────────────▼────────────────────▼────────┐
    │                    Observability Layer                      │
    │  OTel Traces · Prometheus Metrics · Business KPIs · Alerts │
    └────────────────────────────────────────────────────────────┘
              │
    ┌─────────▼──────────────────────────────────────────────────┐
    │                    Persistence Layer                        │
    │  SQLite/PostgreSQL · Session Store · Memory DB · Snapshots │
    └────────────────────────────────────────────────────────────┘
```

### 4.2 场景 A: 从 Demo 到可评估系统（当 prompt 迭代加速时）

**触发条件**：prompt 改动频繁，但无法量化每次改动的影响。
**核心失败模式**：上周的优化可能让 20% 的场景变差，但你不知道。

#### 4.2.1 Eval Harness（最高优先级）

```
eval/
├── harness.py          # 评估运行器
├── scenarios/          # 测试场景 YAML
│   ├── phase1_intent_parsing.yaml
│   ├── phase3_option_comparison.yaml
│   ├── phase5_constraint_validation.yaml
│   └── edge_cases.yaml
├── judges/
│   ├── trajectory_judge.py    # 验证工具调用序列
│   ├── quality_judge.py       # LLM-as-Judge 评分
│   └── cost_judge.py          # 成本效率评估
└── reports/
    └── benchmark_YYYYMMDD.json
```

**场景定义示例**：
```yaml
- id: abstract_beach_intent
  input: "我想去一个能看海的地方放松一下"
  expected_tools: [search_destinations]
  expected_state:
    phase: 3
    preferences: [海边, 放松]
  max_tool_calls: 5
  max_tokens: 10000
```

**与现有 validator/judge 的关系**：validator 是运行时硬约束检查（行程冲突、预算超标），eval harness 是离线批量评估（agent 行为变化趋势）。两者互补，不替代。

#### 4.2.2 Token Budget Manager

**触发条件**：多用户场景 / 接入计费。
**失败模式**：复杂场景连续 10 次 LLM 调用，单次规划成本不可预测。

```python
@dataclass
class TokenBudget:
    max_tokens_per_run: int = 100_000
    warning_threshold: float = 0.8
    critical_threshold: float = 0.95
    consumed: int = 0

    def consume(self, tokens: int) -> BudgetStatus:
        self.consumed += tokens
        ratio = self.consumed / self.max_tokens_per_run
        if ratio >= self.critical_threshold:
            return BudgetStatus.CRITICAL  # 优雅退出
        if ratio >= self.warning_threshold:
            return BudgetStatus.WARNING   # 切换更便宜模型
        return BudgetStatus.OK
```

**Trade-off**：精确 token 计数（tiktoken）vs 近似计数（len//4）。OpenAI 用 tiktoken 精确计数；Anthropic 用 API 返回的 usage.input_tokens。当前 len//3 的粗估只在 MVP 阶段可接受。

#### 4.2.3 Tool Execution Hardening

**触发条件**：接入真实第三方 API。
**失败模式**：外部 API 超时 → agent 挂起；API 报错 → LLM 把错误当结果推理。

```python
# tools/hardening.py
class ToolExecutionPolicy:
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_backoff_base: float = 1.5
    circuit_breaker_threshold: int = 5     # 窗口内最大调用次数
    circuit_breaker_window: int = 120      # 秒
    idempotency_guard: bool = True         # 检测相同参数的重复调用
```

在 `ToolEngine.execute()` 中添加：
- `asyncio.wait_for(tool_fn(**args), timeout=policy.timeout_seconds)`
- `CircuitBreaker` 按工具名记录调用频率
- `IdempotencyGuard` 检测连续相同参数调用
- 结构化错误信封 `{ok, data, error, meta}` 替代裸字符串

#### 4.2.3 Eval Harness

```
eval/
├── harness.py          # 评估运行器
├── scenarios/          # 测试场景 YAML
│   ├── phase1_intent_parsing.yaml
│   ├── phase3_option_comparison.yaml
│   ├── phase5_constraint_validation.yaml
│   └── edge_cases.yaml
├── judges/
│   ├── trajectory_judge.py    # 验证工具调用序列
│   ├── quality_judge.py       # LLM-as-Judge 评分
│   └── cost_judge.py          # 成本效率评估
└── reports/
    └── benchmark_YYYYMMDD.json
```

**场景定义示例**：
```yaml
# scenarios/phase1_intent_parsing.yaml
- id: abstract_beach_intent
  input: "我想去一个能看海的地方放松一下"
  expected_tools: [search_destinations]
  expected_state:
    phase: 3
    preferences: [海边, 放松]
  max_tool_calls: 5
  max_tokens: 10000

- id: specific_destination
  input: "我要去东京玩5天，预算1万"
  expected_tools: [update_plan_state]
  expected_state:
    destination.name: 东京
    budget.total: 10000
  max_tool_calls: 3
```

**核心价值**：每次 prompt 改动后跑 `pytest eval/` 或 `python -m eval.harness --suite=all`，用数据量化 agent 行为变化。这是从 "感觉变好了" 到 "准确率从 78% 提升到 85%" 的跨越。

#### 4.2.4 基础 Input Guardrail

**触发条件**：面向公众用户。
**失败模式**：prompt injection 让 agent 忽略 soul.md 约束。

```python
# guardrails/input_guard.py
class InputGuard:
    """检测 prompt injection 和越界请求"""

    INJECTION_PATTERNS = [
        r"ignore\s+(previous|above|all)\s+instructions",
        r"you\s+are\s+now\s+a",
        r"system\s*:\s*",
        r"<\|.*?\|>",
    ]

    async def check(self, message: str) -> GuardResult:
        for pattern in self.INJECTION_PATTERNS:
            if re.search(pattern, message, re.IGNORECASE):
                return GuardResult(blocked=True, reason="potential_injection")
        return GuardResult(blocked=False)

# guardrails/output_guard.py
class OutputGuard:
    """验证 agent 输出不含 PII、确保事实来自工具"""

    async def check(self, response: str, tool_results: list) -> GuardResult:
        # 检测 PII（电话、身份证、信用卡）
        # 检测幻觉事实（价格/时间未出现在 tool_results 中）
        ...
```

### 4.3 场景 B: 从单用户到可靠服务（当 SLA 要求出现时）

**触发条件**：多用户 / 可用性承诺 / 团队内部工具化。

#### 4.3.1 分层记忆系统

```
Memory Architecture:
├── Working Memory   → 当前会话上下文（已有）
├── Summary Memory   → 已完成任务的结构化摘要（部分有）
├── Artifact Memory  → 工具返回的关键数据片段（新增）
├── Preference Memory → 用户长期偏好 + 衰减权重（增强）
└── Episodic Memory  → 历史旅行经验 + 满意度反馈（增强）
```

关键改进：
- **时间衰减**：6 个月前的偏好权重 × 0.5
- **满意度反馈闭环**：trip_history 的 satisfaction_rating 影响未来推荐权重
- **偏好聚类**：从历史中推断用户旅行 persona（海滩型 / 文化型 / 冒险型）

#### 4.3.2 LLM Provider Resilience

```python
# llm/resilience.py
class ResilientProvider:
    """带回退、重试、降级的 LLM 调用层"""

    primary: LLMProvider       # gpt-4o
    fallback: LLMProvider      # claude-sonnet
    budget_model: LLMProvider  # gpt-4o-mini

    async def chat(self, messages, tools, budget: TokenBudget):
        if budget.status == BudgetStatus.WARNING:
            provider = self.budget_model  # 自动降级
        else:
            provider = self.primary

        for attempt in range(3):
            try:
                return await asyncio.wait_for(
                    provider.stream_chat(messages, tools),
                    timeout=30.0
                )
            except (RateLimitError, TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)  # 指数退避
                    provider = self.fallback  # 切换提供商
                else:
                    raise AgentDegradedError("所有 LLM 提供商不可用")
```

#### 4.3.3 Human-in-the-Loop 审批门

```python
# agent/approval_gate.py
REQUIRES_APPROVAL = {
    "confirm_booking": ApprovalLevel.REQUIRED,
    "update_plan_state": ApprovalLevel.OPTIONAL,  # 仅当涉及预算变更时
}

class ApprovalGate:
    async def check(self, tool_name: str, args: dict) -> ApprovalDecision:
        level = REQUIRES_APPROVAL.get(tool_name, ApprovalLevel.NONE)
        if level == ApprovalLevel.REQUIRED:
            return ApprovalDecision(
                needs_approval=True,
                display=self._format_approval_card(tool_name, args),
                timeout_seconds=300,
            )
        return ApprovalDecision(needs_approval=False)
```

前端展示审批卡片，用户点击确认后 agent 继续。

#### 4.3.4 Observability 增强

当前 OTel traces 已经很好，再增加：

```python
# Prometheus metrics（关键业务指标）
agent_runs_total = Counter("agent_runs_total", "Total agent runs", ["phase", "status"])
agent_run_duration = Histogram("agent_run_duration_seconds", "Agent run duration")
agent_tokens_used = Counter("agent_tokens_used", "Tokens consumed", ["model", "phase"])
agent_tool_errors = Counter("agent_tool_errors", "Tool execution errors", ["tool", "error_code"])
agent_phase7_reached = Counter("agent_phase7_reached", "Completed travel plans")
```

关键 dashboard：
- **完成率**: Phase 7 到达率（当前无法衡量）
- **成本/plan**: 每个完整规划的平均 token 成本
- **工具健康度**: 各工具的成功率 / P95 延迟
- **用户满意度**: 基于 memory 中 satisfaction_rating 的趋势

### 4.4 场景 C: 从工具到产品（面试时口头描述即可）

> 以下不需要实现代码，但面试时能说出来，展示你的架构视野。

| 维度 | 进化方向 |
|------|---------|
| **状态持久化** | SQLite → PostgreSQL + Redis 缓存，支持水平扩展 |
| **多 Agent 协作** | Orchestrator-Worker 分离：主 agent 规划，子 agent 独立执行航班/酒店/景点搜索 |
| **实时数据管道** | 价格/天气/航班变动实时推送，触发行程自动调整 |
| **个性化模型** | 基于用户历史数据 fine-tune 推荐排序模型 |
| **多模态交互** | 支持语音输入、图片识别（"帮我找类似这张照片的地方"） |
| **CI/CD Pipeline** | eval harness 集成到 GitHub Actions，PR 自动跑 benchmark regression |

---

## 五、面试话术指南

### 5.1 90 秒 Elevator Pitch

> "Travel Agent Pro 是我从零构建的 AI 旅行规划系统。核心设计理念是**把确定的交给规则，不确定的交给 LLM**：7 阶段认知流用状态完整度驱动阶段转换，12 个领域工具通过阶段感知路由按需提供，4 层上下文组装确保 LLM 始终有正确的指令和数据。
>
> 我选择手写 Agent Loop 而非用 LangChain，是为了直接面对 context window 管理、阶段转换、错误恢复这些核心问题。后端 375+ 测试 + OpenTelemetry 全链路追踪确保系统可调试、可迭代。"

### 5.2 技术深度展示（按面试官追问方向）

#### Q: "为什么不用 LangChain/LangGraph？"

> "我想深入理解 agent 的核心机制。LangChain 会隐藏很多关键决策：context window 管理、tool call 错误处理、阶段转换逻辑。手写 loop 让我直接面对这些问题。比如我发现 web_search 返回的富文本会快速填满 context window，所以我实现了 payload compaction —— 完整数据推送到前端展示，压缩副本进入 LLM 上下文。这类优化在框架里很难做到。"

> "当然，如果是生产项目，我会评估用 LangGraph 来获得内置的 checkpointing 和 human-in-the-loop。但对于学习和展示理解深度，手写是更好的选择。"

#### Q: "阶段路由是怎么工作的？为什么不让 LLM 决定？"

> "PhaseRouter 用 **状态完整度** 推断阶段，不依赖 LLM 判断。比如：没有目的地 → Phase 1，有目的地但没日期/住宿 → Phase 3，有 skeleton 但没 daily plans → Phase 5。这是 deliberate design choice —— 阶段转换应该是 **确定性** 的，因为 LLM 对自身进度的判断不可靠。不确定的部分交给 LLM（工具选择、对话生成），确定的部分用规则保证。"

#### Q: "怎么保证 agent 输出质量？"

> "当前有三道防线：（1）Harness Validator 做硬约束检查 —— 时间冲突、预算超标、天数不匹配；（2）LLM Judge 做软质量评分 —— 节奏、地理动线、主题连贯性、个性化匹配；（3）375+ 测试覆盖核心路径。"

> "但我认识到还缺一个关键环节：**Eval Harness**。目前的测试验证的是代码逻辑正确性，不是 agent 行为质量。下一步我计划建立 100+ 场景的 benchmark suite，每次 prompt 改动后自动跑，用数据追踪准确率、成本、延迟的变化趋势。"

#### Q: "如果要支持生产环境，你会怎么改？"

> "**三个优先级最高的改动**：
>
> 1. **Token Budget + Cost Control** —— 每次 run 设置 token 上限，达到阈值自动降级到更便宜的模型，防止成本失控。
> 2. **Tool Execution Hardening** —— 给每个工具加 timeout、circuit breaker、幂等检测。当前如果外部 API 挂了，agent 会无限等待。
> 3. **Eval Harness** —— 建立可重复的评估体系，让 agent 质量变化可量化。
>
> 中期还需要：状态持久化从文件系统迁移到 SQLite/PostgreSQL、添加认证和速率限制、前端 SSE 重连机制。"

#### Q: "上下文压缩是怎么实现的？"

> "我经历了一个迭代过程。最初用 LLM 调用来做对话摘要，但发现这引入了额外延迟和成本，而且摘要质量不稳定。后来改为 **规则驱动** 的压缩策略：
>
> - 分类：标记含偏好信号（预算、不要、必须）的用户消息为 must_keep
> - 压缩：其他消息提取为结构化决策记录（如 '决策: update_plan_state destination = 巴黎'）
> - 保留：原始 system message + must_keep + 压缩摘要 + 最近 4 条消息
>
> 这让压缩变成零成本、确定性的操作，同时保留了用户的关键意图。"

### 5.3 展示成长性的关键句式

面试官最想听到的不是"我做了什么"，而是"我怎么思考 trade-off"：

- *"我最初用了 X 方案，发现 Y 问题后改为 Z"* → 展示迭代能力
- *"当前实现是 X，但如果要 production-grade 需要 Y，因为 Z"* → 展示认知边界
- *"我选择了规则驱动而非 LLM 驱动，因为在这个场景下确定性比灵活性更重要"* → 展示判断力
- *"这个系统目前没有 eval harness，这是我认为最大的 gap"* → 展示自我批判能力

---

## 六、落地优先级（如果你有时间在面试前做改动）

### Phase A: 面试前必做（1-2 天）

| 改动 | 工作量 | 价值 |
|------|-------|------|
| Token Budget Manager（基础版） | 4h | 展示成本意识 |
| Tool timeout + circuit breaker | 4h | 展示工程韧性 |
| 基础 Input Guardrail | 2h | 展示安全意识 |
| Eval Harness 骨架 + 10 个场景 | 6h | 展示评估思维 |

### Phase B: 面试后加分（1 周）

| 改动 | 工作量 | 价值 |
|------|-------|------|
| LLM provider 回退 + 指数退避 | 4h | 生产韧性 |
| 状态持久化迁移到 SQLite | 8h | 可扩展性 |
| SSE 重连 + 错误边界 | 4h | 前端韧性 |
| Prometheus metrics + dashboard | 6h | 可观测性完善 |

### Phase C: 持续进化（2-4 周）

| 改动 | 工作量 | 价值 |
|------|-------|------|
| Human-in-the-Loop 审批门 | 1 周 | 安全控制 |
| 分层记忆 + 时间衰减 | 1 周 | 个性化质量 |
| Benchmark suite 100+ 场景 | 2 周 | 评估体系成熟 |
| Docker 容器化 + CI/CD | 1 周 | 工程完整性 |

---

## 七、一句话总结

**这个项目展示了你对 Agent 核心机制的深入理解和扎实的工程能力。它不是一个调包演示，而是一个有明确设计哲学的系统：确定性优先的阶段管理、可观测性驱动的迭代、分层的上下文工程。当前的约束（token budget、tool hardening、eval harness）不是认知盲区，而是 MVP 阶段有意识的 scope 选择。**

**面试的核心策略：不要防守（"我还没做 X"），而要进攻（"我在 Y 阶段选择优化 Z，因为..."）。展示 trade-off 思维比展示功能完备更重要。**

> *"The best agent engineers don't just build agents that work. They build agents that fail gracefully, cost predictably, and improve measurably. — And they know when each of those qualities matters."*
