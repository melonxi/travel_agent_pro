# Hostile questions（30 秒答法）

| # | 问题 | 30 秒答法 | 别踩坑 |
|---|------|-----------|--------|
| 1 | 为什么不用 LangGraph？ | 要精确控制阶段边界、Plan Writer 单写、tool 协议和失败恢复，所以用显式 Agent Loop。这些契约可以迁移到 LangGraph；不是为了反框架。 | 不要攻击框架生态 |
| 2 | 自己造 Loop 的维护成本？ | 换可控性。关键路径有 pytest + golden + trace grader。协议被框架锁死时改恢复语义往往更贵。 | 不要说「从零更高级」 |
| 3 | 黑板冲突是否 deterministic？ | 候选版本化 + 唯一性/约束拒绝规则；用 candidate store / uniqueness 测试固化。 | 不要只说「LLM 会处理」 |
| 4 | 并行如何控成本？ | 阶段工具裁剪、worker 超时、失败早停、只重派受影响天；stats 记 token/延迟。 | 不要夸「无限并行」 |
| 5 | 一周重做砍什么？ | 砍工具广度与 UI 细修；保留 Loop、Candidate/黑板、steering、eval/trace。 | 不要说「什么都重要」 |
| 6 | AI 辅助写了多少？ | 实现可加速；状态机、写路径、恢复语义和测试标准是我拍板，并能讲 trade-off。 | 不要否认工具，也不要说全是 AI |
| 7 | 2000+ tests 是否注水？ | collect ≈2054；对外强调 A0 核心 Runtime 路径 + 40 golden。CI 先跑核心子集保证可复现。 | 不要只甩数字 |
| 8 | 失败时用户看到什么？ | SSE 错误/状态事件、steering ack、质量门禁不过不冻结交付物；trace 可回放。 | 不要只讲日志给开发看 |
| 9 | 和 Chatbot+tools 差在哪？ | 长任务状态、阶段门禁、并行版本治理、中途控制、可评测恢复，不只是单轮 function call。 | 不要贬低所有 chatbot |
| 10 | 有生产用户吗？ | 没有。作品集原型。用测试、评测和故障注入证明可靠性，不装 DAU/SLA。 | 诚实是加分 |

## 与 DeepReview 怎么分工

- Travel Agent Pro：Runtime、长任务、并行/重规划、评测恢复  
- DeepReview：企业业务、人审闭环  

一句话：一个卖引擎可靠性，一个卖业务闭环。
