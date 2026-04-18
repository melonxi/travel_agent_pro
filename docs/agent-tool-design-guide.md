# Agent Tool 设计指南

> 汇总自 Anthropic、OpenAI、Google 三家官方工程博客与 API 文档（2025–2026）。
> 用于指导本项目 `backend/tools/*` 和任何新增 agent tool 的设计与评审。

---

## 0. 核心心态：为 agent 而设计，不是为开发者而设计

- **ACI（Agent–Computer Interface）≥ HCI**。Anthropic 明确指出，agent 的工具界面应当投入与人机界面同等的工程努力。
- **工具定义 = prompt 工程**。工具名、描述、入参、返回值、错误消息都会进入上下文，每一个字段都是 prompt 的一部分。
- **"向新员工解释"测试（Pass the intern test）**。只读你的工具定义，一个陌生开发者能否正确调用？如果不能，就把答案补进描述里。
- **模型易用性优先**。格式、命名、参数风格尽量贴近互联网上自然语料的样式，避免模型在陌生结构上浪费 token 推理。

---

## 1. 工具选择：少而锐，而不是多而全

| 原则 | 三家共识 |
|------|---------|
| 别包装底层 API | Anthropic：不是所有 API endpoint 都该成为工具 |
| 一轮可用工具数 | OpenAI：<20；Gemini：10–20；o3/o4-mini：<100 但 <20 arg/tool |
| 大库按需加载 | OpenAI 的 tool search、Gemini 的 dynamic tool selection |
| 合并常走的链路 | 若两三个 tool 总是连着调，合成一个复合 tool（例如 `schedule_event` 内建空闲时段查询） |
| 针对高影响工作流 | 先想 "agent 要完成什么任务"，再决定暴露哪些工具 |

**反模式**：
- `list_contacts` 一次倒出整张表 → 改 `search_contacts(query, filters)`。
- 为每个 CRUD 单独造一个工具 → 模型在选择时更易错。
- 把内部服务的全部字段都塞进入参 → agent 不知道该填什么。

---

## 2. 命名与命名空间

- **动词 + 对象**：`search_notes`、`create_plan`、`update_day_plan`。
- **服务/资源前缀**：`xiaohongshu_search`、`plan_daily_update`，降低大量工具下的混淆。
- 不允许空格、点号、横线（Gemini 硬约束）。统一 `snake_case`。
- 前缀 vs 后缀哪种更好？**用评估跑出来**，不要拍脑袋。
- 入参名要无歧义：`user_id` 优于 `user`；`iso_start_time` 优于 `start`。

---

## 3. 描述（Description）怎么写

一条好描述至少包含：

1. **用途一句话**：这个工具解决什么问题。
2. **何时使用 / 何时不用**：与相邻工具的边界。
3. **输入格式要求**：日期格式、ID 形态、单位。
4. **典型示例**：一到两条真实调用样例。
5. **边界情况**：空结果、分页、权限错误时的行为。

> OpenAI：Hosted + custom 工具混用时，**在开发者 prompt 里明确决策边界**：覆盖范围、置信度预期、回退策略。

---

## 4. 入参 Schema

- **Strict mode / Structured output 默认开启**（OpenAI：`strict: true`；Gemini：强类型 + enum）。
  - `additionalProperties: false`
  - 所有字段显式 `required`；可选字段用 `["string", "null"]`。
- **用 enum 把非法状态变成不可表达**（例如 `response_format: "detailed" | "concise"`）。
- **不要让模型填它不需要填的东西**：能从上下文推出的参数（user_id、session_id），由代码注入，而不是让模型生成。
- **防错设计（Poka-yoke）**：Anthropic 的经典例子 — 把相对路径改成绝对路径后，模型"完美无缺"。让错误使用更难。
- **单参数 20 个以下**（o3/o4-mini 的 in-distribution 上限）。

---

## 5. 返回值

- **语义 > 技术标识**：返回 `"赛里木湖-伊宁-霍尔果斯"` 比返回 `UUID 7f3a9...` 对 agent 更有用。UUID/hash 容易诱发幻觉。
- **支持详略切换**：给一个 `response_format` 枚举（`detailed` / `concise`），让 agent 自己控制 token 消耗。
- **分页 / 过滤 / 截断**：单次返回 token 建议控制在 ~25k 以内（Anthropic）；被截断时在尾部明确告诉 agent "还有更多，用 filter 缩小范围"。
- **格式选择**：XML / JSON / Markdown 在不同模型上性能差异显著，**跑评估决定**。
- **无返回值的工具也要回**：`send_email` 这类至少回 `{success: true}` 或错误原因。

---

## 6. 错误处理

错误消息是给模型看的 prompt，不是给开发者看的 stack trace。

**有效**：
```
Error: end_time 必须晚于 start_time。
当前: start_time=2026-04-20T10:00, end_time=2026-04-20T09:00
建议: 检查参数顺序，或确认是否跨日。
```

**无效**：
```
Error: Invalid parameter. 400.
```

- 带上具体字段、当前值、可操作的下一步。
- 幂等失败（重复创建）应返回已存在资源而非硬报错。
- 认证/权限错误要清楚告诉 agent 是"换账号"还是"别再试了"。

---

## 7. 并发 / 多次调用

- **假设模型一回合会叫多个工具**（OpenAI 默认 parallel tool calls）。若必须串行，显式 `parallel_tool_calls=false`。
- **结果不保证有序**：Gemini 3 每个 function call 有唯一 `id`，回结果时必须带上相同 id 做映射。
- **工具之间不共享内存**：如果 tool A 产出的状态 tool B 要用，要么通过返回值传递，要么在服务端维护 session 并由 agent 引用 id。

---

## 8. 上下文 / Token 预算

- 工具定义本身计入 system tokens。描述越长、工具越多，留给任务的上下文就越少。
- 优化次序：
  1. 删掉冗余工具
  2. 压缩描述（保留示例，去掉废话）
  3. 默认返回 concise，让 agent 主动要 detailed
  4. 真的太大时考虑 fine-tune 或 tool search

---

## 9. 安全与最小权限

- **Least privilege**：tool 只拿完成该任务所需的最小权限。
- **高风险操作二次确认**：删除、转账、外发消息等，要么工具内硬编码 dry-run，要么在 orchestrator 层加 human-in-the-loop。
- **不要把 secret 放进返回值**，agent 上下文会被记录/回传。
- **输入校验在服务端再做一次**，不要相信模型生成的参数已经合规。

---

## 10. 评估驱动开发（Eval-Driven）

三家都强调：工具设计不是写完就完，要闭环评估。

**流程**：
1. **生成 20+ 真实场景任务**，覆盖多步工具链。
   - 强任务："安排下周跟 Jane 讨论 Acme 项目的会议，附会议纪要。"
   - 弱任务："用 jane@acme.corp 发一个会议邀请。"（已经把所有参数喂给模型了，测不出选择能力）
2. **跑 agent 并打开 extended thinking / CoT**，观察推理路径。
3. **追踪指标**：
   - 任务成功率
   - 平均工具调用次数
   - 总 token 消耗
   - 错误类型分布
4. **用 Claude / GPT 分析失败 trace**，定位是描述歧义、schema 设计、还是返回格式问题。
5. **迭代顺序**：描述 → 参数名 → 返回结构 → 错误消息 → 工具拆分/合并。

> Anthropic 在 SWE-bench agent 的实践中，**优化工具花的时间多于优化整体 prompt**。

---

## 11. 本项目落地清单

针对 `backend/tools/` 现有工具，下一次评审时逐条打勾：

- [ ] 工具名是否 `动词_对象`，并有合适命名空间前缀
- [ ] 描述是否包含：用途、何时使用、何时不用、示例、边界情况
- [ ] JSON schema 是否 strict，enum 是否用足
- [ ] 入参里是否混入了本应由代码注入的 id / session
- [ ] 返回值是否用语义字段而非 UUID
- [ ] 是否支持 concise / detailed 切换
- [ ] 错误消息是否指向下一步可执行动作
- [ ] 是否有至少 10 条真实任务评估用例
- [ ] 一轮暴露给 agent 的工具数是否 ≤ 20
- [ ] 高风险操作是否有 dry-run 或 HITL 兜底

---

## 参考资料

- [Anthropic — Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic — Building Effective AI Agents](https://www.anthropic.com/research/building-effective-agents)
- [Anthropic — Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic — Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [OpenAI — Function calling guide](https://developers.openai.com/api/docs/guides/function-calling)
- [OpenAI — Using tools](https://developers.openai.com/api/docs/guides/tools)
- [OpenAI Cookbook — o3 / o4-mini Function Calling Guide](https://cookbook.openai.com/examples/o-series/o3o4-mini_prompting_guide)
- [Google — Function calling with the Gemini API](https://ai.google.dev/gemini-api/docs/function-calling)
- [Google — Using Tools with Gemini API](https://ai.google.dev/gemini-api/docs/tools)
- [Google Cloud — Introduction to function calling (Vertex AI)](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/multimodal/function-calling)
