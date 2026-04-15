# TODO

## 1. tool-self-repair

### 背景

当前 agent 系统已经具备将工具报错作为 `tool_result` 返回给模型的能力，但对"模型传了不受支持的工具参数"这类错误，仍然缺少足够强的自纠错支持。

已出现的实际案例：

- 工具：`xiaohongshu_search`
- 场景：模型调用 `search_notes`
- 输入包含：`max_results`
- 结果：由于工具函数签名未接收该参数，触发 Python `TypeError`
- 当前落到 agent 的错误类型：`INTERNAL_ERROR`

这类错误虽然会被回传给模型，但错误语义不够明确，模型未必能稳定完成下一轮自我修正。

### 待办项

- 在 `ToolEngine` 中识别类似 `unexpected keyword argument` 的异常
- 不要统一归类成 `INTERNAL_ERROR`
- 改为更明确的可恢复错误，例如：`INVALID_INPUT` / `UNSUPPORTED_PARAMETER`
- 在错误结果里附带不被支持的参数名
- 在 `suggestion` 中返回该工具允许的参数列表
- 最好明确到 operation 级别，例如 `xiaohongshu_search.search_notes` 支持哪些字段
- 评估是否需要在真正调用 Python 工具函数前，先根据工具 schema 做一次参数白名单校验
- 扫描其他工具的 schema 与 Python 函数签名是否一致，重点关注搜索类工具

### 目标

让 agent 在工具调用失败时，不只是"把错误返回给模型"，而是能够以更高概率驱动模型完成自我纠错并继续执行。

## 2. [DONE] openai_provider 错误分类：从 APIError 中恢复真实 HTTP 状态码

### 目标

让裸 `APIError`（讯飞等兼容网关常见）被准确归类为 TRANSIENT/RATE_LIMITED/BAD_REQUEST，而不是误报为 PROTOCOL_ERROR。

### 完成记录

- 完成日期：2026-04-15
- 分支：`fix/llm-error-classify`
- 改动：`llm/errors.py` 新增 `classify_opaque_api_error()`，两个 provider fallthrough 改调该函数
- 测试：`test_classify_opaque_api_error.py`（28+ 用例）、`test_anthropic_provider_classify.py`（6 用例）、`test_openai_provider.py` 已有用例更新

## 3. TraceViewer 迭代行折叠优化

### 背景

当前 `build_trace()`（`backend/api/trace.py:98-153`）为每个 LLM 调用创建一条独立的迭代行。在长对话中（如 289 次 LLM 调用），TraceViewer 右面板会产生大量冗余信息：

- 连续多行属于同一 agent phase，无工具调用，优先级/token/cost 完全相同
- 用户需要反复滚动才能找到有实际意义的迭代（带工具调用或阶段切换的行）
- 模型 `astron-code-latest` 不在 `_PRICING` 表（`backend/telemetry/stats.py:10-25`）中，导致所有行显示 0 tokens / <$0.001

### 待办项

#### 后端（`backend/api/trace.py`）
- 在 `build_trace()` 中识别**连续同 phase、无工具调用**的 LLM 调用序列
- 将这些序列合并为一个"折叠组"（`collapsed_group`），包含：组内调用数量、首尾时间戳、汇总 token/cost
- 保留每条原始记录作为 `children`，供前端展开时使用

#### 前端（`frontend/src/components/TraceViewer.tsx`）
- `IterationRow` 支持渲染折叠组：默认显示汇总行（如 "Phase: plan × 47 calls"），点击展开详情
- 折叠/展开动画与 Solstice 设计系统一致（glass morph + smooth transition）

#### 补充
- 将缺失的模型添加到 `_PRICING` 表，或在前端对 0 tokens 的行显示 "N/A" 而非 "0"
- 考虑对折叠组内无差异的列（priority, tools, cost）只在汇总行显示一次

### 目标

将 TraceViewer 的信噪比从"每个 LLM 调用一行"提升到"每个有意义阶段/工具调用一行"，大幅减少滚动和视觉噪声。
