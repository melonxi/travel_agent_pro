# 工具结果的上下文管理：行业实践与项目启发

> 调研时间：2026-04
> 范围：Codex CLI、Claude Code、OpenCode、Manus、Hermes Agent、OpenClaw 等主流 Agent 的工具结果处理策略，及对 Travel Agent Pro 的改进启发

---

## 1. 核心问题

Agent 调用工具后，工具返回的结果会塞入 LLM 的上下文窗口。随着对话轮次增加，这些结果不断堆积，带来两个问题：

1. **硬上限**：上下文 token 超过模型窗口限制，API 直接拒绝请求
2. **软退化**：即使未触及上限，上下文越长，模型的注意力越分散（context rot），召回准确率下降

各行各业 Agent 的解法，核心差异在于：**工具结果用的是"全文塞入"、"截断保留"、还是"占位符替换"？历史对话用的是"物理删除"、"标记隐藏"、还是"LLM 摘要"？**

---

## 2. 各 Agent 的工具结果处理策略

### 2.1 Codex CLI（OpenAI）—— 一次性 LLM 摘要 + 物理删除

**核心思路**：把整个对话交给 LLM 写"交接摘要"，然后用摘要替换全部历史。

**工具结果处理**：直接物理删除，既不截断也不留占位符。

**具体机制**：

- 两条压缩路径：本地路径（`compact.rs`，客户端调 LLM 生成摘要，兼容任何模型）和远程路径（`compact_remote.rs`，调 OpenAI 内部 `responses/compact` 端点）
- 摘要提示词只有 4 个要点：当前进度与关键决策、重要约束与用户偏好、剩余 TODO、继续工作的关键数据
- **用户消息原文永久保留**，所有助手回复和工具调用/结果替换为一个结构化摘要
- 如果摘要后空间仍不够，从最早的消息开始"砍头"截断

```
压缩前：26 条消息，~15,400 tokens
压缩后：4 条消息 — 用户消息 #2 + #10 原文 + 摘要 + 最后一条用户消息
```

**优缺点**：

- 优点：直观，"交接文档"概念通俗易懂
- 缺点：一次性全删不可逆；如果摘要遗漏了关键细节，信息永久丢失

---

### 2.2 Claude Code（Anthropic）—— 三层渐进式"精准遗忘"

这是目前最精细的上下文管理方案。

#### Layer 1：工具结果占位符替换（零 LLM 成本）

最轻量的一层。在每次 LLM 调用前自动执行，不需要 LLM 参与：

- 保留最近 N 次工具调用（默认 keep=3）的完整结果
- 超出保护窗口的旧工具结果 → 替换为 `[Old tool result content cleared]`
- **保留 `tool_use` 记录**：模型知道自己调过什么工具、传了什么参数，只是结果被清掉了
- 需要时可以重新调用工具获取

```
压缩前：
  assistant: [tool_use] read_file("src/auth.ts")
  tool:      [40K tokens 文件内容]

压缩后：
  assistant: [tool_use] read_file("src/auth.ts")      ← 保留
  tool:      "[Old tool result content cleared]"       ← 占位符
```

**这是最值得借鉴的设计**：实现了"选择性失忆"——模型记得自己读过什么书，只是忘了具体内容，需要时可以再翻一遍。

#### Layer 2：Cache 友好策略

不直接修改压缩逻辑，而是**只从消息序列尾部修剪**，保持前缀不变：

- Anthropic API 支持 Prompt Cache：如果消息前缀与上次请求一致，可复用计算结果
- Claude Code 宁可少删一点，也要保证前缀稳定性，最大化缓存命中率
- 对于长时间任务（如重构整个模块），这能节省显著的 API 成本

#### Layer 3：9 节结构化 LLM 摘要（最后手段）

当 Layer 1 + 2 仍不够时触发：

- 触发阈值：`effective_context_window - 13,000 tokens`
- 优先尝试 Session Memory Compact（利用已有结构化信息替代 LLM 调用）
- LLM 摘要严格按照 9 个固定节生成：用户原始意图、核心技术概念、文件和代码、遇到的错误及修复、问题解决逻辑链、所有用户消息摘要、TODO 项、当前工作、建议下一步
- 摘要后自动重读最近编辑的文件（最多 5 个，总预算 50K tokens）
- 重新注入工具和 skill 定义

---

### 2.3 OpenCode（开源）—— 标记隐藏 + LLM 摘要

两层"步进式治理"：

#### Step 1：Prune（标记隐藏，非物理删除）

- 前提：pruning 能释放 > 20K tokens 才值得执行
- 保护最近 40K tokens 作为安全缓冲
- `skill` 类型工具输出永不修剪
- 保护最近 2 轮用户对话的完整内容
- **关键设计**：不做物理删除，而是打上 `compacted = Date.now()` 时间戳，逻辑上"隐形"但数据仍在数据库——为未来的历史回溯留了后门

#### Step 2：LLM 5 节摘要

- 使用隐藏的专用 agent（不打扰用户当前交互）生成摘要
- 摘要结构：目标、进展、关键决策、待办、下一步
- 压缩后自动重播用户最后一条消息，保证 agent 的最新记忆点在用户最新指令上
- 遵循用户语言：用户用中文沟通，摘要也用中文

**对比 Codex/Claude Code 的不可逆压缩，OpenCode 的非破坏性设计是最"开发者友好"的**。

---

### 2.4 Hermes Agent —— 四阶段智能压缩

目前看到的最激进的压缩方案：

#### Phase 1：工具修剪（零成本）

与 Claude Code Layer 1 相同——旧工具输出替换为 `[Old tool output cleared to save context space]`。

#### Phase 2：保护关键内容

- 系统提示和首次交互神圣不可侵犯
- 最近 ~20K tokens 受保护

#### Phase 3：结构化摘要

不是简单的"请总结"，而是用固定模板生成**交接文档**：

```
## 目标
[用户想做什么]
## 约束与偏好
[用户偏好、编码风格、约束]
## 进展
### 已完成
[完成的工作 — 文件路径、命令、结果]
### 进行中
[当前工作]
## 关键决策
[重要技术决策及原因]
## 相关文件
[读过、修改过、创建过的文件]
```

#### Phase 4：增量更新

**杀手级特性**：压缩器存储上一次的摘要。后续压缩时基于上次摘要增量更新，而非从头开始。这使得上下文可以跨多次压缩持续累积，而不是逐次退化。

---

### 2.5 Manus —— CodeAct 架构

Manus 不使用传统 JSON function calling，模型直接写 Python 代码执行：

- 工具结果通过代码执行沙箱返回
- **结果处理由代码逻辑控制**，不是直接塞入上下文
- 多 Agent 协作时，子 Agent 的输出作为结构化摘要返回给父 Agent，而非完整内容
- 天然避免了 JSON function calling 的结构膨胀问题

---

### 2.6 OpenClaw —— 引用层提案（未落地）

社区提出了 Context Heap 方案（Issue #26498），但被关闭为 "not planned"：

- 引入 `ref` 引用层：大内容存在外部堆中，上下文只保留 `{ ref, summary }`
- 新工具 `context_fetch(ref, query?)`：需要时再拉取完整内容
- 分三个阶段逐步落地：子 Agent 输出 → 大文件/大响应 → 历史对话

**思路有价值但尚未被采纳实现**。

---

## 3. 核心对比

| 维度 | Codex CLI | Claude Code | OpenCode | Hermes | Manus |
|------|-----------|-------------|----------|--------|-------|
| 工具结果处理 | 物理删除 | 占位符替换 | 时间戳隐藏 | 占位符替换 | 代码逻辑控制 |
| 压缩层次 | 单层 | 三层 | 两层 | 四层 | 多层 |
| 可逆性 | 不可逆 | 不可逆 | 可逆（数据还在） | 增量更新 | 部分可逆 |
| Cache 优化 | 无 | 深度集成 | 无 | 无 | N/A |
| LLM 调用需求 | 每次压缩必调 | 仅 Layer 3 | 仅 Step 2 | Phase 3/4 | 视情况 |
| 用户消息保留 | 原文保留 | 摘要保留 | 摘要+最后一条重播 | 增量摘要 | 结构化摘要 |

---

## 4. Travel Agent Pro 现状分析

### 4.1 现有压缩机制

项目已实现两层压缩（`backend/agent/compaction.py` + `backend/context/manager.py`）：

1. **`compact_messages_for_prompt`**：在 `before_llm_call` hook 中按 token 预算渐进压缩
   - 优先压搜索类工具结果（信息密度低）
   - 根据使用率分 moderate/aggressive 两档
   - 对 `web_search` 和 `xiaohongshu_search` 有定制截断逻辑（条数限制 + 文本截断）
   - 对其他工具结果**不压缩**

2. **`compress_for_transition`**：阶段转换时的规则驱动摘要（无额外 LLM 调用）
   - 用户消息保留原文
   - 助手回复截断到 200 字符
   - `PLAN_WRITER_TOOL_NAMES` 状态写工具渲染为"决策"行
   - 其他工具渲染为"成功/失败"一行摘要

### 4.2 现有不足

1. **截断而非替换**：即使 aggressive 模式下，小红书搜索结果仍保留 5-8 条/200-300 字，只是缩短了，没有用占位符替代
2. **保护窗口缺失**：没有"最近 N tokens 不碰"的概念，可能误压缩当前步骤的工作上下文
3. **覆盖范围窄**：只有 web_search 和 xiaohongshu_search 有定制压缩，search_flights / search_accommodations / get_poi_info / calculate_route 等同样可能产生大返回值
4. **缺少增量摘要**：compress_for_transition 是规则驱动的全量重构，没有 Hermes 那样的"基于上次摘要增量更新"
5. **没有 Cache 友好设计**：system prompt 每轮重建，未考虑 Anthropic Prompt Cache 的前缀稳定性优化

---

## 5. 改进建议（按优先级排序）

### P0：搜索型工具结果 → 占位符替换

**核心改动**：对保护窗口之外的搜索类工具结果，从"截断保留"改为"占位符替换"。

**当前**：
```python
# aggressive 模式下仍保留 5 条搜索结果，每条 200 字
{"results": [{"title": "...", "content": "截断到200字的..."}, ...x5]}
```

**建议**：
```python
# 保护窗口之外，整条 tool_result 替换为占位符
content = "[工具结果已压缩。如需重新查看，请再次调用该工具]"
# 保留 tool_use 记录，模型知道自己做过什么搜索
```

**分类策略**：

| 类别 | 工具 | 压缩策略 |
|------|------|----------|
| 搜索型（可重获） | web_search, xiaohongshu_search, quick_travel_search | 保护窗口外 → 占位符 |
| 查询型（可重获摘要） | search_flights, search_accommodations, get_poi_info, check_weather | 保护窗口外 → 保留关键价格/时间，删除详情 |
| 状态写入（不可压缩） | PLAN_WRITER_TOOL_NAMES 全部 | 永不替换，但已写入状态的结果可用"决策: tool_name args"摘要 |
| 路线型（中间态） | calculate_route, check_availability, check_feasibility | 保护窗口外 → 保留结论，丢弃步骤细节 |
| 生成型（不可重获） | generate_summary, assemble_day_plan | 永不压缩 |

**扩展点**：现有 `compact_tool_result_for_prompt` 的 `tool_name` 分支结构天然支持这种扩展。

### P1：保护最近 K tokens 不压缩

**核心改动**：设置保护窗口，最近 ~20K tokens 的内容绝不压缩/替换。

```python
RECENT_PROTECT_TOKENS = 20_000

def compact_with_protection(messages, prompt_budget, ...):
    # 从后往前累计 token，划定保护边界
    protected_start = find_protected_boundary(messages, RECENT_PROTECT_TOKENS)
    # 只对 protected_start 之前的消息做压缩/替换
```

**为什么适合本项目**：Phase 3 有 4 个子步骤（brief → candidate → skeleton → lock），当前步骤的搜索结果和写状态结果必须完整保留，但 brief 步骤在进入 candidate 后价值大幅降低。

### P2：扩展工具压缩分类表

**核心改动**：为 flights/accommodations/POI/route 等工具增加定制压缩逻辑。

```python
# compaction.py 中扩展
COMPACT_STRATEGIES = {
    "placeholder": {"web_search", "xiaohongshu_search", "quick_travel_search"},
    "key_fields": {"search_flights", "search_accommodations", "get_poi_info", "check_weather"},
    "conclusion_only": {"calculate_route", "check_availability", "check_feasibility"},
    "never": {"generate_summary", "assemble_day_plan", *_PLAN_WRITER_NAMES},
}
```

- `placeholder`：保护窗口外整体替换
- `key_fields`：保留价格/时间/名称等关键字段，删除描述性文本
- `conclusion_only`：只保留最终结论（如"可行"/"距离 15km"），删除过程数据
- `never`：永不压缩

### P3：阶段转换时主动写记忆

**核心改动**：在 Phase 转换 hook 中，让 Agent 主动将关键决策写入记忆，而非仅依赖规则摘要。

**当前**：`compress_for_transition` 是纯规则驱动的文本重构。
**建议**：在 Phase 转换时增加一步：

```
Phase 3 skeleton → lock 转换时：
  1. 自动将 candidate_pool 摘要 + shortlist 选择理由 + skeleton 选择理由写入 trip_memory
  2. 然后旧搜索结果可以用占位符替换（因为关键信息已经存在记忆中）
```

这样新的 system prompt 可以从记忆中重建上下文，而不依赖完整的对话历史。

### P4：Cache 友好的 System Prompt 排列

**核心改动**：将 `build_system_message` 输出调整为前缀稳定、后缀可变的结构。

```
稳定前缀（每次请求相同，命中 Prompt Cache）：
  soul.md + 当前阶段指引 + 工具使用硬规则

可变后缀（每轮变化）：
  当前规划状态 + 记忆上下文 + 最新对话
```

**收益**：大量使用 Anthropic 供应商时可复用 KV Cache，显著降低成本和延迟。
**代价**：需要调整 `build_system_message` 的拼接顺序，确认 Anthropic API 支持 system prompt 拆分为多块。

---

## 6. 行业趋势总结

1. **从"全塞入上下文"到"精准遗忘"**：占位符（Claude Code / Hermes）和非破坏性隐藏（OpenCode）是主流方向，物理删除（Codex CLI）是最简单但最不可逆的方案
2. **分层治理优于一次性压缩**：Claude Code 的三层体系（零成本占位符 → Cache 优化 → LLM 摘要）是最合理的渐进策略，大部分情况只用 Layer 1 就够
3. **可重新获取的数据不应长期占用上下文**：搜索结果、文件内容等可重获的工具返回，一旦被消费（写入状态/记忆），就应被释放
4. **保护窗口是刚需**：最近的工作上下文（~20K tokens）不可压缩，否则会破坏 Agent 的"心流"状态
5. **非破坏性设计优于物理删除**：OpenCode 的时间戳标记、Hermes 的增量摘要，都是为了保留回溯的可能性
6. **LLM 摘要是最后手段而非首选**：成本高、有损、不可逆，应仅在零成本压缩不够时才触发