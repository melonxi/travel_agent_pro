# Spec 4: Agent Trace Viewer

> **目标**：把可观测性从"Jaeger 后端"变为"面试现场可 demo"，一眼看清一轮规划的执行链路。
>
> **隔离边界**：后端新建 `backend/api/trace.py`（独立文件）+ `backend/main.py` 末尾一行路由注册。前端新建 `TraceViewer.tsx` 等文件 + `App.tsx` 右侧面板区域加 Tab。不碰 harness/、evals/、SessionSidebar.tsx、index.css。

---

## 1. 背景与动机

项目已有完整的可观测性基础：
- OpenTelemetry + Jaeger 覆盖 Agent Loop、工具调用、阶段转换、上下文压缩
- `SessionStats` 记录每次 LLM/工具调用的 token、耗时、成本
- SSE 事件流包含 tool_call / tool_result / state_update / context_compression

但 Jaeger 需要 Docker 运行，面试现场 demo 时不方便。前端也没有执行链路可视化。面试追问"失败怎么定位"时只能说"看 Jaeger"。

Trace Viewer 将执行链路直接渲染在前端右侧面板，与现有的 Phase3Workbench / Map / Timeline / BudgetChart 并列。

---

## 2. 后端：Trace API

### 2.1 端点定义

`GET /api/sessions/{session_id}/trace`

返回结构化 trace 数据，从 SessionStats + 消息历史中重建。

### 2.2 响应格式

```json
{
  "session_id": "sess_abc123",
  "total_iterations": 5,
  "summary": {
    "total_input_tokens": 15200,
    "total_output_tokens": 3400,
    "total_llm_duration_ms": 12500,
    "total_tool_duration_ms": 4200,
    "estimated_cost_usd": 0.082,
    "llm_call_count": 5,
    "tool_call_count": 12,
    "by_model": {
      "gpt-4o": { "calls": 3, "input_tokens": 9600, "output_tokens": 2100, "cost_usd": 0.048 },
      "claude-sonnet-4-20250514": { "calls": 2, "input_tokens": 5600, "output_tokens": 1300, "cost_usd": 0.034 }
    },
    "by_tool": {
      "web_search": { "calls": 3, "total_duration_ms": 2400, "avg_duration_ms": 800 },
      "update_plan_state": { "calls": 5, "total_duration_ms": 60, "avg_duration_ms": 12 }
    }
  },
  "iterations": [
    {
      "index": 1,
      "phase": 1,
      "llm_call": {
        "provider": "openai",
        "model": "gpt-4o",
        "input_tokens": 3200,
        "output_tokens": 450,
        "duration_ms": 2100,
        "cost_usd": 0.012
      },
      "tool_calls": [
        {
          "name": "web_search",
          "duration_ms": 800,
          "status": "success",
          "side_effect": "read",
          "arguments_preview": "query: 京都旅行攻略",
          "result_preview": "找到 5 条结果"
        },
        {
          "name": "update_plan_state",
          "duration_ms": 12,
          "status": "success",
          "side_effect": "write",
          "arguments_preview": "field: destination, value: 京都",
          "result_preview": "已更新"
        }
      ],
      "state_changes": [
        {
          "field": "destination",
          "before": null,
          "after": "京都"
        }
      ],
      "compression_event": null
    }
  ]
}
```

### 2.3 实现 — `backend/api/trace.py`

```python
from fastapi import APIRouter, HTTPException

trace_router = APIRouter()

@trace_router.get("/sessions/{session_id}/trace")
async def get_session_trace(session_id: str):
    ...
```

数据来源：
- **summary**：直接调用现有 `SessionStats.to_dict()`
- **iterations**：从 session 的消息历史中重建。遍历 messages，按 assistant 回复分组为 iteration。每个 iteration 内提取：
  - LLM 调用信息：从 `SessionStats.llm_calls` 中按顺序取
  - 工具调用信息：从 `SessionStats.tool_calls` + messages 中的 tool_call/tool_result 对提取
  - state_changes：从 messages 中的 `state_update` 类型消息提取，对比前后快照
  - compression_event：从 messages 中的 `context_compression` 类型消息提取

会话不存在时返回 404。会话无 stats 数据时返回空 iterations + 零值 summary。

### 2.4 路由注册 — `backend/main.py`

在 `create_app()` 函数内、`return app`（~L1632）之前追加：

```python
from api.trace import trace_router
app.include_router(trace_router, prefix="/api")
```

改动量：2 行。位置在 ~L1631（`return app` 之前），与 Spec 1 的 hooks 改动区域（~L390-402）相距 1200+ 行，git 自动合并无冲突。

### 2.5 `backend/api/__init__.py`

新建空文件，使 `backend/api/` 成为 Python package。

---

## 3. 前端：Trace Viewer

### 3.1 `frontend/src/types/trace.ts` — 新建

```typescript
export interface TraceIteration {
  index: number;
  phase: number;
  llm_call: {
    provider: string;
    model: string;
    input_tokens: number;
    output_tokens: number;
    duration_ms: number;
    cost_usd: number;
  } | null;
  tool_calls: TraceToolCall[];
  state_changes: StateChange[];
  compression_event: string | null;
}

export interface TraceToolCall {
  name: string;
  duration_ms: number;
  status: 'success' | 'error' | 'skipped';
  side_effect: 'read' | 'write';
  arguments_preview: string;
  result_preview: string;
}

export interface StateChange {
  field: string;
  before: unknown;
  after: unknown;
}

export interface TraceSummary {
  total_input_tokens: number;
  total_output_tokens: number;
  total_llm_duration_ms: number;
  total_tool_duration_ms: number;
  estimated_cost_usd: number;
  llm_call_count: number;
  tool_call_count: number;
  by_model: Record<string, { calls: number; input_tokens: number; output_tokens: number; cost_usd: number }>;
  by_tool: Record<string, { calls: number; total_duration_ms: number; avg_duration_ms: number }>;
}

export interface SessionTrace {
  session_id: string;
  total_iterations: number;
  summary: TraceSummary;
  iterations: TraceIteration[];
}
```

### 3.2 `frontend/src/hooks/useTrace.ts` — 新建

```typescript
export function useTrace(sessionId: string | null) {
  trace: SessionTrace | null
  loading: boolean
  error: string | null
  refresh(): Promise<void>  // 手动刷新
}
```

获取策略：
- 当 sessionId 变化时自动获取
- 收到 SSE `done` 事件后自动刷新一次（通过 props 或 context 传入 `refreshTrigger`）
- 不使用定时轮询（避免不必要请求）

### 3.3 `frontend/src/components/TraceViewer.tsx` — 新建

Props：
```typescript
interface TraceViewerProps {
  sessionId: string | null;
  refreshTrigger?: number;  // SSE done 时 increment，触发刷新
}
```

组件结构：

```
<TraceViewer>
  <SummaryBar />              // 顶部摘要栏
  <IterationList>             // 可滚动迭代列表
    <IterationRow>            // 一个 iteration
      <LLMCallBar />          // LLM 调用条形图
      <ToolCallList>          // 工具调用列表
        <ToolCallRow />       // 单个工具调用（水平条形图）
      </ToolCallList>
    </IterationRow>
  </IterationList>
  <StateDiffPanel />          // 点击 iteration 展开的 state diff
</TraceViewer>
```

#### SummaryBar

水平排列 5 个指标卡片：
- 总 Token：`{input_tokens + output_tokens}` 
- 总成本：`${cost_usd}` 
- 总耗时：`{llm_duration + tool_duration}ms`
- LLM 调用：`{count}` 次
- 工具调用：`{count}` 次

#### IterationRow

每个 iteration 渲染为一个可展开的行：

**收起状态**（默认）：
- 左侧：iteration 序号 + phase 标签
- 中间：LLM 调用条形图（宽度按 duration 占总 duration 比例缩放）+ model 名
- 右侧：token 数 + 成本

**展开状态**（点击后）：
- 工具调用子行列表
- State diff 面板

#### ToolCallRow

- 工具名称 + side_effect 标签（read 蓝色 / write 橙色）
- 耗时条形图（CSS width 按 duration 比例）
- status 颜色：success 绿、error 红、skipped 灰
- arguments_preview 和 result_preview 灰色小字
- 并行的 read 工具使用相同缩进，write 工具单独一行

#### StateDiffPanel

- 字段名 + before → after
- before=null, after 有值：绿色高亮（新增）
- before 和 after 都有值且不同：黄色高亮（修改）
- 值使用 JSON.stringify 展示，过长截断

### 3.4 `frontend/src/styles/trace-viewer.css` — 新建

遵循 Solstice 设计系统：
- 条形图使用 CSS `width` + `background: linear-gradient()`
- LLM provider 颜色：OpenAI `#10a37f`（绿）、Anthropic `#d4a574`（琥珀）
- 工具 status 颜色：success `var(--green)`, error `var(--red)`, skipped `var(--text-secondary)`
- side_effect 标签：read 蓝色、write 琥珀色
- state diff：新增绿色背景、修改黄色背景
- 整体暗色玻璃风格，与右侧面板其他组件一致

**不修改 `index.css`**。

### 3.5 `frontend/src/App.tsx` — 修改

在右侧面板区域（RightPanel 渲染处）增加 "Trace" Tab：

改动约 10 行：
1. 导入 `TraceViewer` 组件
2. 右侧面板 Tab 栏新增 "Trace" 按钮
3. 条件渲染 `<TraceViewer sessionId={currentSessionId} />`

改动位置：右侧面板渲染区域，与 Spec 3 改的 `SessionSidebar.tsx` 完全不同文件。

---

## 4. 文件清单

| 文件 | 改动类型 | 内容 |
|------|---------|------|
| `backend/api/__init__.py` | 新建 | 空文件 |
| `backend/api/trace.py` | 新建 | trace_router + get_session_trace 端点 |
| `backend/tests/test_trace_api.py` | 新建 | 端点测试 |
| `backend/main.py` | 修改 | `return app` 前追加 2 行路由注册（~L1631） |
| `frontend/src/types/trace.ts` | 新建 | SessionTrace 等类型定义 |
| `frontend/src/hooks/useTrace.ts` | 新建 | trace API 封装 |
| `frontend/src/components/TraceViewer.tsx` | 新建 | Trace Viewer 组件 |
| `frontend/src/styles/trace-viewer.css` | 新建 | 独立样式 |
| `frontend/src/App.tsx` | 修改 | 右侧面板加 Trace Tab（~10 行） |

**不碰的文件**：`SessionSidebar.tsx`、`index.css`、`harness/`、`evals/`、`tools/`、`memory/`。

---

## 5. 测试策略

### 5.1 后端测试 — test_trace_api.py

| 测试场景 | 输入 | 期望 |
|---------|------|------|
| 正常会话 | 有 stats 和 messages 的 session_id | 200，返回完整 trace JSON |
| 空会话 | 有 session 但无 stats | 200，iterations=[]，summary 全零 |
| 不存在的会话 | 无效 session_id | 404 |
| iterations 排序 | 多轮对话 | iterations 按时间顺序排列 |
| state_changes 提取 | messages 含 state_update | state_changes 正确反映字段变更 |

### 5.2 前端手动验证

| 场景 | 验证点 |
|------|--------|
| 选中会话 | Trace Tab 可点击，加载并显示 trace 数据 |
| SummaryBar | 5 个指标卡片数值正确 |
| IterationRow 收起 | 显示 iteration 序号 + model + token + 成本 |
| IterationRow 展开 | 显示工具调用列表 + state diff |
| 条形图比例 | 耗时长的条形图更宽 |
| provider 颜色 | OpenAI 绿色、Anthropic 琥珀色 |
| state diff 高亮 | 新增字段绿色、修改字段黄色 |
| 空 trace | 显示"暂无 trace 数据" |
| 刷新 | 对话结束后 trace 自动更新 |

### 5.3 类型检查

`cd frontend && npx tsc --noEmit` 通过。

---

## 6. 验收标准

1. `GET /api/sessions/{id}/trace` 返回正确结构化数据
2. `pytest backend/tests/test_trace_api.py` 全部通过
3. `pytest backend/` 全量回归无新增失败
4. `cd frontend && npm run build` 构建成功
5. Trace Viewer 在右侧面板正确渲染
6. SummaryBar 指标与 `/api/sessions/{id}/stats` 数据一致
7. 条形图 + state diff + 工具 waterfall 可视化正常
8. 不引入新的第三方前端依赖
9. 样式与 Solstice 设计系统一致
