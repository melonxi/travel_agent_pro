# 移除正则预提取，统一由 LLM 驱动字段写入

## 背景

当前 `main.py` 的 chat 端点在进入 agent loop 之前，通过 `apply_trip_facts` 用正则表达式从用户消息中提取 destination、dates、budget 三个字段并直接写入 plan。这存在两个问题：

1. **只覆盖 3 个字段** — 住宿、景点、人数、节奏偏好等信息即使用户说了也不会被提取，造成信息丢失。
2. **phase 跳转过早** — 正则提取后立即触发 `check_and_apply_transition`，可能跳过中间 phase，导致 agent loop 以错误的 phase prompt 启动。

## 目标

- 移除 `apply_trip_facts` 的预提取机制，所有字段提取统一由 LLM 通过 `update_plan_state` 工具完成。
- 在 agent loop 内部实现 phase 感知：当 `update_plan_state` 触发 phase 变化时，同一轮对话内立即刷新 system prompt、plan 状态和工具集。
- phase 切换时，通过 LLM 压缩前阶段对话为摘要，作为新阶段的背景上下文。

## 设计

### 1. Agent Loop 改造（`agent/loop.py`）

**新增依赖注入：**

```python
class AgentLoop:
    def __init__(self, llm, tool_engine, hooks, max_retries,
                 phase_router,        # 获取新 phase 的 prompt 和 tools
                 context_manager,     # 重建 system message
                 plan,                # plan 引用，感知 phase 变化
                 llm_factory,         # 创建压缩用 LLM 实例
                 memory_mgr,          # 加载用户画像
                 user_id):            # 用户 ID
```

**迭代间 phase 检测逻辑：**

在每次迭代中执行完所有 tool calls 后，检测 `plan.phase` 是否发生变化。如果变了：

1. 调用 LLM 压缩当前 messages 为摘要（通过 `context_manager.compress_for_transition`）。
2. 重建 messages：
   - `[0]` = 新的 system message（soul + 新 phase prompt + 最新 plan 状态 + 用户画像）
   - `[1]` = 前序阶段摘要（system 角色）
   - `[2]` = 用户原始消息（loop 开始前保存）
3. 刷新 tools 为新 phase 的工具集。
4. 继续下一次迭代。

**original_user_message：** 在 loop 开始前从 messages 中提取最后一条 user 消息并保存，作为每次上下文重建的固定锚点。

### 2. 上下文压缩（`context/manager.py`）

新增 `compress_for_transition` 方法：

```python
async def compress_for_transition(
    self,
    messages: list[Message],
    from_phase: int,
    to_phase: int,
    llm_factory: Callable,
) -> str:
```

- 输入：当前 messages 列表、源 phase、目标 phase、LLM 工厂函数。
- 行为：将 messages 中除 system 外的对话内容交给 LLM，要求生成简要摘要，保留用户偏好、约束和关键决策。
- 输出：自然语言摘要字符串。
- LLM 配置：复用项目统一的 `config.llm` 配置（通过 `llm_factory` 创建实例）。

### 3. main.py 改动

**移除：**
- 删除 `from state.intake import apply_trip_facts` 导入。
- 删除 chat 端点中的 `apply_trip_facts(plan, req.message)` 调用及后续的 `check_and_apply_transition`。

**修改 `_build_agent`：**
- 签名增加 `user_id` 参数。
- 传入 `phase_router`、`context_manager`、`llm_factory`、`memory_mgr`、`user_id` 给 AgentLoop。

### 4. state/intake.py 处置

- `apply_trip_facts` 和 `extract_trip_facts` 不再被调用，标记为废弃。
- `parse_dates_value` 和 `parse_budget_value` 保留 — 它们仍被 `update_plan_state.py` 使用（LLM 传入字符串值时的解析）。
- `_extract_destination` 和 `_extract_budget_text` 可废弃，暂不删除。

### 5. Phase 切换场景示例

用户输入："五一去东京5天，预算2万"

```
迭代 1:
  messages[0] = phase 1 prompt（灵感顾问）
  tools = phase 1 工具集
  LLM 调用 update_plan_state(destination="东京")
  → hook 触发 phase 1→2
  → LLM 压缩对话 → 重建 messages（phase 2 prompt）→ 刷新 tools

迭代 2:
  messages[0] = phase 2 prompt（目的地推荐专家）
  tools = phase 2 工具集
  LLM 调用 update_plan_state(dates={"start":"2026-05-01","end":"2026-05-06"})
  → hook 触发 phase 2→3
  → LLM 压缩对话 → 重建 messages（phase 3 prompt）→ 刷新 tools

迭代 3:
  messages[0] = phase 3 prompt（行程节奏规划师）
  tools = phase 3 工具集
  LLM 调用 update_plan_state(budget=20000)
  → hook 触发 phase 3→4
  → LLM 压缩对话 → 重建 messages（phase 4 prompt）→ 刷新 tools

迭代 4:
  messages[0] = phase 4 prompt（住宿区域顾问）
  tools = phase 4 工具集
  LLM 输出文本回复，循环结束
```

## 测试影响

- `test_e2e_golden_path.py`：移除对 `apply_trip_facts` 提取结果的断言，改为验证 agent loop 执行后 plan 状态由 mock LLM 通过 `update_plan_state` 正确写入。
- 其他测试不受影响。

## 性能影响

- 移除一次同步正则提取（可忽略）。
- 每次 phase 切换增加一次 LLM 压缩调用。
- 最坏情况：用户一口气说完所有信息 → 3 次连续 phase 跳转 → 3 次额外 LLM 压缩调用。压缩输入短小，延迟可接受。

## 不变的部分

- 前端 SSE 事件流格式不变。
- `update_plan_state` 工具参数和行为不变。
- `phase_router.infer_phase` 逻辑不变。
- backtrack 机制不变。
- `parse_dates_value` / `parse_budget_value` 保留供 `update_plan_state` 使用。
