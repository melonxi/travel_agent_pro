# Thinking / Reasoning Stream Spike Memo

**日期：** 2026-04-15
**作者：** Agent
**状态：** 调研完成，暂不启动实施

---

## 目的

评估是否在 Travel Agent Pro 中接入 LLM 原生的"扩展思考"（extended thinking / reasoning）能力，将推理过程实时流式传输到前端，替代或补充当前基于规则的 narration hint 系统。

---

## 1. 模型支持现状

### Claude (Anthropic)

| 模型 | 扩展思考支持 | 方式 |
|------|------------|------|
| claude-sonnet-4-20250514 | 是 | `thinking` 参数 + `thinking` content block |
| claude-3.5-sonnet | 否 | — |
| claude-3-opus | 否 | — |

- 启用方式：`messages.create(thinking={"type": "enabled", "budget_tokens": N})`
- 流式返回 `content_block_start` / `content_block_delta` / `content_block_stop`，type 为 `"thinking"`
- 思考内容为自然语言文本（非结构化）
- **Token 成本**：thinking tokens 按 output token 计费，budget 通常设 4096-16384，实际消耗视任务复杂度而定。对旅行规划任务，预估每轮增加 2000-6000 output tokens（~$0.02-0.06/轮，按 sonnet 4 价格 $15/M output）

### OpenAI (GPT-4o)

| 模型 | reasoning 支持 | 方式 |
|------|---------------|------|
| gpt-4o | 否 | — |
| o1 / o1-mini | 是（不可流式） | reasoning_tokens 在 usage 中报告，内容不可见 |
| o3 / o3-mini | 是（可流式） | `reasoning` role content blocks |

- gpt-4o 无原生 reasoning 支持
- o1 系列的 reasoning 是隐式的，无法获取推理文本
- o3 系列可以流式输出 reasoning，但项目当前未使用 o3

### 结论

当前项目使用的 `claude-sonnet-4-20250514` **支持扩展思考**，且可流式传输。GPT-4o 不支持。

---

## 2. Provider 层改造方案

### 新增 ChunkType

```python
class ChunkType(str, Enum):
    # ... existing
    REASONING_DELTA = "reasoning_delta"  # 新增
```

### AnthropicProvider 改造

```python
# anthropic_provider.py chat() 中
async for event in stream:
    if event.type == "content_block_start" and event.content_block.type == "thinking":
        # 标记进入 thinking block
        in_thinking = True
    elif event.type == "content_block_delta" and in_thinking:
        yield LLMChunk(
            type=ChunkType.REASONING_DELTA,
            content=event.delta.thinking,
        )
    elif event.type == "content_block_stop" and in_thinking:
        in_thinking = False
```

### SSE 层

```python
# main.py _run_agent_stream 中
if chunk.type == ChunkType.REASONING_DELTA:
    yield f"data: {json.dumps({'type': 'reasoning_delta', 'content': chunk.content})}\n\n"
```

### 预估工作量

- `anthropic_provider.py`：~30 行改动
- `llm/types.py`：+1 enum
- `main.py` SSE 分派：+3 行
- 前端 `ChatPanel.tsx`：+20 行（接收 + 分派到 ThinkingBubble 或新的 ReasoningPanel）
- 前端 UI 组件：见下方分析

---

## 3. UI 形态对比

### 方案 A：Accordion（内联折叠）

ThinkingBubble 内嵌一个可展开的 reasoning 区域：

```
[·] 思考中… [▶ 展开推理]
    └─ "用户想去京都 7 天，预算中等。
        我应该先搜索京都的热门景点..."
```

**优点：**
- 与现有 ThinkingBubble 自然融合
- 不占用额外屏幕空间
- 用户主动展开才看到，不影响主流体验

**缺点：**
- reasoning 文本可能很长（数千字），内联展开后会推开聊天内容
- 历史消息中的 reasoning 需要持久化或丢弃

### 方案 B：侧栏 / 抽屉

类似 Trace 面板的独立侧栏：

```
[主聊天区]  |  [推理面板]
            |  用户想去京都...
            |  我应该先搜索...
```

**优点：**
- 不干扰主聊天流
- 大文本量可滚动浏览
- 可与 Trace 面板复用布局

**缺点：**
- 需要额外 UI 入口（按钮 / Tab）
- 三栏布局 + 侧栏 = 空间更紧张
- 实现复杂度高

### 推荐

**方案 A（Accordion）**，理由：
- 与现有 narration hint 渐进增强，不破坏已有体验
- 开发量小（~50 行前端代码）
- 可通过 localStorage 记住用户偏好（是否默认展开）

---

## 4. 与 Compaction 的交互

关键问题：reasoning 文本是否计入上下文、是否参与压缩？

### 分析

- Anthropic 的 thinking blocks **不计入后续请求的上下文**——它们是一次性的流式输出，不会出现在 `messages` 列表中
- 因此 reasoning 文本**不会被压缩、不占用 context window**
- thinking tokens 的 `budget_tokens` 会影响 `max_tokens` 的可用空间（Anthropic 要求 `budget_tokens < max_tokens`）

### 影响

- `ContextManager` 无需改动
- `compaction.py` 无需感知 reasoning
- 唯一需要注意的是 `max_tokens` 设置——当前如果设了 4096，开启 thinking 后需要确保留足空间

---

## 5. 成本与性能影响

| 指标 | 当前（无 thinking） | 开启 thinking (budget=8192) |
|------|-------------------|---------------------------|
| 每轮 output tokens | ~500-2000 | ~2500-8000 |
| 每轮成本 | ~$0.01-0.03 | ~$0.04-0.12 |
| 首 token 延迟 | ~1-3s | ~3-8s（thinking 先完成） |
| 流式感知延迟 | 立即 | thinking chunk 立即流式 |

成本增加约 **3-4 倍**，但用户体验提升（能看到 agent "在想什么"）。

---

## 6. 建议

### 暂不启动实施，理由：

1. **当前 narration hint 已覆盖核心场景**：基于规则的 hint（Task 28/29）已经告诉用户 agent 在做什么，成本为零
2. **成本敏感**：旅行规划是多轮长对话场景，每轮 3-4 倍成本增加在批量使用时显著
3. **延迟增加**：thinking 会增加首 token 延迟，与"减少等待焦虑"的项目目标矛盾
4. **GPT-4o 不支持**：项目设计为多 LLM 切换，reasoning 只在 Claude 上可用，会造成体验不一致

### 建议的后续触发条件

在以下任一条件满足时重新评估：
- 用户反馈 narration hint 信息量不够
- Anthropic thinking 成本显著下降（如 <$5/M output tokens）
- 需要 reasoning 内容做 agent 可解释性审计
- OpenAI gpt-4o 也支持可见 reasoning output

---

*此 memo 仅做调研记录，不产生代码变更。*
