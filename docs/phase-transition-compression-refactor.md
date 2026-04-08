# 阶段切换压缩机制重构

- **日期**：2026-04-08
- **范围**：`backend/context/manager.py`、`backend/agent/loop.py` 及相关测试
- **目标**：移除阶段切换时的 LLM 摘要调用，改为规则化抽取；裁剪富文本工具返回的 payload，降低上下文窗口压力

---

## 一、原设计与问题

### 1.1 原机制

阶段切换发生在 `AgentLoop._rebuild_messages_for_phase_change`（在同一次 HTTP 请求内完成的阶段推进）。原实现会在切换时：

1. 以 `llm_factory()` 再开一个 LLM 客户端。
2. 将前一阶段的消息序列送入该 LLM，生成一段自然语言摘要。
3. 把摘要以 `Role.SYSTEM` 再次注入 `messages`，构成 `[系统指令, 系统摘要, 用户消息]` 的格式。

### 1.2 问题清单

| 问题 | 具体表现 |
|---|---|
| **多一次 LLM 调用** | 每次阶段切换都要多消耗一次 token + 一次网络延迟；而阶段切换往往发生在用户最想立刻看到下一阶段回复的时刻，这笔开销是纯负收益。 |
| **摘要内容不稳定** | LLM 摘要可能漏掉关键的 `update_plan_state` 决策，或用自然语言重新措辞，导致下一阶段 LLM 误读状态。 |
| **格式容易漂移** | 不同次调用产出的摘要长度、风格都不一致，上层无法做断言/缓存。 |
| **多条 system 消息兼容性差** | 部分 provider 对多条 `system` 消息处理不同（有的拼接，有的只取首条），容易产生平台差异。 |
| **富文本工具 payload 未裁剪** | `web_search` / `xiaohongshu_search` 的原始 payload（正文、评论、snippet）会原封不动落进 `messages`，下一轮 LLM 调用时重复计费，并在阶段切换时被压缩层当成"历史上下文"二次消耗。 |

### 1.3 是否完全移除压缩？

**不能。** `ContextManager.should_compress` / `classify_messages` 仍被 `backend/main.py` 的 `on_before_llm` 钩子用于 token 溢出保护（超过阈值时裁剪普通历史消息）。本次重构只替换阶段切换链路的摘要生成，token 溢出链路保持不变。

---

## 二、新设计

### 2.1 规则化切换摘要

`ContextManager.compress_for_transition` 不再调用 LLM，改为按消息序列逐条渲染：

- **user**：原文保留为 `用户: {content}`。
- **assistant 文本**：取 `content.strip()`，超过 200 字截断 + `…`，行首加 `助手: `。
- **assistant tool_calls**：在后续遇到同 id 的 `ToolResult` 时合并渲染；`update_plan_state` 被特殊识别，输出 `决策: update_plan_state {field} = {value}` 的精准决策行。
- **tool result**：成功时 `工具 {name} 成功: {data_preview}`，失败时 `工具 {name} 失败: {error_code}:{error}`。
- 非字符串 value 使用 `_short_repr` 生成短预览，避免巨大 dict/list 把一行撑爆。

输出示例：

```
用户: 我想五一去东京
决策: update_plan_state destination = 东京
决策: update_plan_state dates = {'start': '2026-05-01', 'end': '2026-05-06'}
助手: 好的，已记录东京和日期。接下来我先给你几套行程骨架方案。
```

`llm_factory` 参数保留签名但改为 `del llm_factory`——不再调用，但上层调用点无需改签名。

### 2.2 assistant 角色注入

`_rebuild_messages_for_phase_change` 现在把摘要挂到 **assistant turn** 上，而不是再开一条 system：

```python
rebuilt.append(
    Message(
        role=Role.ASSISTANT,
        content=(
            f"以下是阶段 {from_phase} 的对话与工具调用回顾，"
            f"现在进入阶段 {to_phase}。\n{summary}"
        ),
    )
)
rebuilt.append(self._copy_message(original_user_message))
```

最终结构：`[system(阶段N指令), assistant(阶段N-1回顾), user(当前输入)]`。

**好处**：
- 单一 system 消息，provider 兼容性更强；
- 符合"模型自己的回顾"的语义，下一阶段 LLM 读起来不会困惑；
- 模型不会把回顾内容当成新指令来遵循（assistant 角色是"已发生的事"，不是"要做的事"）。

### 2.3 富文本工具 payload 裁剪

在 `agent/loop.py` 模块级新增：

```python
_WEB_SEARCH_SNIPPET_MAX = 220
_WEB_SEARCH_ANSWER_MAX = 400
_XHS_TEXT_MAX = 220
```

以及四个纯函数：

| 函数 | 行为 |
|---|---|
| `_truncate_text(value, limit)` | 仅对字符串生效；超长追加 `…`；非字符串原样返回。 |
| `_compact_web_search_data(data)` | 截断 `answer` 与 `results[*].content`，保留 `title` / `url` 原文；返回**新字典**，原数据不动。 |
| `_compact_xiaohongshu_data(data)` | 区分 `read_note`（截 `note.desc`）和 `get_comments`（逐条截 `content`）；未知 `operation` 直接返回 dict 的浅拷贝。 |
| `compact_tool_result_for_messages(tool_name, result)` | 只作用于 `status=success` 且 `data` 为 dict 的 web_search / xiaohongshu_search；其他情况**原样透传**同一个 `ToolResult` 实例，避免热路径上的多余分配。 |

在 `AgentLoop.run` 里，tool result 落入 `messages` 时经过裁剪层：

```python
messages.append(
    Message(
        role=Role.TOOL,
        tool_result=compact_tool_result_for_messages(tc.name, result),
    )
)
```

**关键约束**：**只裁剪落入 messages 的副本**，向 UI 流式下发的 `ToolResult` 仍然是原始完整数据。用户看到的内容零损失，省的是下一次喂给 LLM 时的 token。

---

## 三、代码改动总览

### 3.1 源码

- **`backend/context/manager.py`**
  - `imports`：新增 `ToolCall`、`ToolResult`。
  - `compress_for_transition`：重写为纯规则抽取。
  - 新增私有方法 `_render_tool_event(tool_call, result)` 与 `_short_repr(value)`。
- **`backend/agent/loop.py`**
  - 模块级新增 payload 压缩常量与 4 个工具函数。
  - `_rebuild_messages_for_phase_change`：摘要以 `Role.ASSISTANT` 注入，文案改为"以下是阶段 N 的对话与工具调用回顾"。
  - `run()`：tool result 追加到 `messages` 前先走 `compact_tool_result_for_messages`。

### 3.2 测试

| 文件 | 改动 |
|---|---|
| `tests/test_context_manager.py` | 删除旧的 LLM 路径测试，新增 4 条规则化测试：rule-based 基础行为、长上下文也不应调 LLM、长助手文本截断、工具成功/失败渲染。 |
| `tests/test_agent_loop.py` | 阶段切换后的消息断言改为匹配新格式与 `Role.ASSISTANT`。 |
| `tests/test_e2e_golden_path.py` | golden path 里对切换摘要的断言对齐新文案；`import Role`。 |
| `tests/test_phase_integration.py` | 巴厘岛用例同上。 |
| **`tests/test_loop_payload_compaction.py`（新建）** | 14 条单测，覆盖：`_truncate_text` 边界；web_search 长/短 payload、非 dict 透传、原始数据不被改写；xiaohongshu `read_note` / `get_comments` / 未知 operation；`compact_tool_result_for_messages` 的 web/xhs/其他工具/error 状态/非 dict data/短 payload 路径。 |

---

## 四、测试结果

### 4.1 定向回归（13 个关键测试文件）

```
tests/test_state_models.py              13 passed
tests/test_generate_summary.py            8 passed
tests/test_update_plan_state.py          18 passed
tests/test_appendix_issues.py             8 passed
tests/test_phase_router.py               21 passed
tests/test_phase_integration.py           6 passed
tests/test_phase34_merge.py              22 passed
tests/test_e2e_golden_path.py             1 passed
tests/test_backtrack_service.py           6 passed
tests/test_context_manager.py             9 passed
tests/test_agent_loop.py                 11 passed
tests/test_telemetry_agent_loop.py        4 passed
tests/test_telemetry_phase_context.py     6 passed
—————————————————————————————————————————————
                                        133 passed
```

### 4.2 新增 payload 压缩单测

```
tests/test_loop_payload_compaction.py    14 passed
```

### 4.3 全量 `tests/`

```
350 passed, 5 failed
```

5 个失败项已通过 `git stash` → 在 HEAD 状态下复测，**全部在本次重构之前就已失败**，属于与本次改动无关的预存在问题：

| 失败项 | 性质 |
|---|---|
| `test_flyai_client::test_search_flight_success` | FlyAI 子进程/集成预存在失败 |
| `test_flyai_client::test_nonzero_status` | 同上 |
| `test_flyai_client::test_empty_item_list` | 同上 |
| `test_xiaohongshu_search::test_xiaohongshu_search_tool_registration` | 工具描述字符串断言与当前描述不一致 |
| `test_error_paths::test_chat_backtrack_restores_new_destination_from_message` | 回溯链路的预存在问题 |

**结论：本次重构 0 回归。**

---

## 五、收益汇总

1. **延迟下降**：阶段切换路径去掉了一次 LLM 调用，端到端延迟下降一整个 LLM round trip。
2. **成本下降**：每次阶段切换省一次 prompt + completion token。
3. **确定性上升**：规则化摘要不会遗漏 `update_plan_state` 决策，`决策: ...` 行可被上层解析/校验。
4. **上下文窗口压力下降**：`web_search` / `xiaohongshu_search` 的 payload 在 `messages` 内被裁短（snippet 220、answer 400、xhs 文本 220），富搜索工具被连续调用时尤其显著。
5. **provider 兼容性更好**：切换摘要改走 assistant turn，避免多条 system 消息在不同 provider 上的语义差异。
6. **UI 体验零损失**：裁剪只作用于 messages 副本，UI 流仍然是完整 payload。

---

## 六、未触动与保留

- **token 溢出压缩链路**：`should_compress` / `classify_messages` / `on_before_llm` 钩子保持不变；本次只替换阶段切换的摘要生成。
- **`compress_for_transition` 签名**：`llm_factory` 参数保留并显式 `del`，避免调用点连带改动，方便未来需要重新引入 LLM 时回切。
- **soul.md / phase prompts**：未修改。

---

## 七、后续可选优化

1. **把裁剪阈值做成 phase 级配置**：当前硬编码在 `loop.py`，未来若某阶段明确需要更长 snippet（如 phase 3 阅读小红书正文），可按阶段下发。
2. **为 `_render_tool_event` 引入更多工具专属格式**：当前只对 `update_plan_state` 做特殊渲染，`search_flights` / `search_accommodations` 等核心工具可以同样有精简行。
3. **切换摘要加长度上限**：极端长 phase 内的 assistant/user 文本行数可以再封一个"最多 N 行"的护栏。
