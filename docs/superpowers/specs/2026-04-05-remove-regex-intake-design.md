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

**逐 tool call 的 phase 检测逻辑：**

在每次迭代中，tool calls 逐个执行。**每执行完一个 tool call 后**立即检测 `plan.phase` 是否变化。如果变了：

1. **立即中断当前批次** — 跳过该迭代中剩余未执行的 tool calls。
2. 判断是否为 backtrack（见下方 backtrack 专节），走不同的上下文处理路径。
3. 若为正常前进：调用 LLM 压缩当前 messages 为摘要（通过 `context_manager.compress_for_transition`）。
4. 重建 messages：
   - `[0]` = 新的 system message（soul + 新 phase prompt + 最新 plan 状态 + 用户画像）
   - `[1]` = 前序阶段摘要（system 角色）
   - `[2]` = 用户原始消息（loop 开始前保存）
5. 刷新 tools 为新 phase 的工具集。
6. 继续下一次迭代（LLM 在新上下文中重新决策）。

**为什么必须逐 tool call 检测：** LLM 一次可产出多个 tool calls。如果第一个 tool call 触发了 phase 变化，后续 tool calls 可能在错误的 phase 下执行（使用不该可用的工具，或写入已被清除的字段）。逐 tool call 检测确保 phase 变化后立即生效。

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

### 5. Backtrack 时的上下文处理

Backtrack 与正常 phase 前进的上下文处理路径不同。当 `update_plan_state(field="backtrack")` 触发回退时：

1. **立即中断当前批次** — 同正常 phase 变化。
2. **不压缩前阶段对话** — backtrack 是"推翻之前的决策"，如果将回退前的对话压缩为摘要注入新上下文，LLM 可能从摘要中"恢复"被清除的状态（如被清除的目的地、日期等），违背回退意图。
3. **硬上下文边界** — 重建 messages 时只包含：
   - `[0]` = 新的 system message（soul + 回退目标 phase 的 prompt + 回退后的 plan 状态）
   - `[1]` = system 消息，说明"用户从 phase X 回退到 phase Y，原因：..."
   - `[2]` = 用户原始消息
4. plan 中的 `preferences` 和 `constraints` 被保留（`clear_downstream` 不清理它们），这些是用户偏好，应贯穿全程。
5. 刷新 tools 为回退目标 phase 的工具集。
6. 设置 `needs_rebuild = True`（保持与现有机制兼容）。

### 6. Phase 切换场景示例

**示例基于当前 `infer_phase` 的真实逻辑：**

```python
# phase/router.py — infer_phase 规则
if not plan.destination:
    if plan.preferences: return 2   # 有偏好但没目的地 → phase 2
    return 1                         # 什么都没有 → phase 1
if not plan.dates: return 3          # 有目的地没日期 → phase 3
if not plan.accommodation: return 4  # 有日期没住宿 → phase 4
if len(daily_plans) < total_days: return 5
return 7
```

**注意：budget 不参与 phase 推进。**

#### 场景 A：用户说"五一去东京5天，预算2万"

```
迭代 1:
  messages[0] = phase 1 prompt（灵感顾问）
  tools = phase 1 工具集
  LLM 产出 3 个 tool calls:
    [1] update_plan_state(destination="东京")
        → 执行 → hook 触发 infer_phase → destination 有值, dates 无 → phase 1→3
        → 检测到 phase 变化 → 中断批次（跳过 tool call [2] 和 [3]）
        → LLM 压缩对话 → 重建 messages（phase 3 prompt）→ 刷新 tools

迭代 2:
  messages[0] = phase 3 prompt（行程节奏规划师）
  tools = phase 3 工具集
  LLM 调用 update_plan_state(dates={"start":"2026-05-01","end":"2026-05-06"})
  → hook 触发 phase 3→4（有目的地+日期，无住宿）
  → LLM 压缩对话 → 重建 messages（phase 4 prompt）→ 刷新 tools

迭代 3:
  messages[0] = phase 4 prompt（住宿区域顾问）
  tools = phase 4 工具集
  LLM 调用 update_plan_state(budget={"total":20000,"currency":"CNY"})
  → hook 触发 infer_phase → 仍为 phase 4（budget 不影响 phase）→ 无变化
  LLM 输出文本回复，询问住宿偏好，循环结束
```

#### 场景 B：用户说"想去海边放松一下"

```
迭代 1:
  messages[0] = phase 1 prompt（灵感顾问）
  tools = phase 1 工具集
  LLM 调用 update_plan_state(preferences={"key":"氛围","value":"海边放松"})
  → hook 触发 infer_phase → destination 无, preferences 有 → phase 1→2
  → LLM 压缩对话 → 重建 messages（phase 2 prompt）→ 刷新 tools

迭代 2:
  messages[0] = phase 2 prompt（目的地推荐专家）
  tools = phase 2 工具集
  LLM 没有足够信息确定目的地，输出文本推荐 2-3 个候选
  循环结束
```

#### 场景 C：用户在 phase 4 说"不想去这里了，换个目的地"

```
迭代 1:
  messages[0] = phase 4 prompt（住宿区域顾问）
  tools = phase 4 工具集
  LLM 调用 update_plan_state(field="backtrack", value={"to_phase":2,"reason":"用户想换目的地"})
  → BacktrackService 执行：清除 destination, dates, accommodation, daily_plans
  → 检测到 phase 变化（4→2）且为 backtrack
  → 中断批次 → 不压缩（硬边界）→ 重建 messages（phase 2 prompt + 回退说明）→ 刷新 tools

迭代 2:
  messages[0] = phase 2 prompt（目的地推荐专家）
  tools = phase 2 工具集
  LLM 看到回退说明 + 用户原始消息，重新推荐目的地
  循环结束
```

## 测试影响

- `test_e2e_golden_path.py`：移除对 `apply_trip_facts` 提取结果的断言，改为验证 agent loop 执行后 plan 状态由 mock LLM 通过 `update_plan_state` 正确写入。
- 其他测试不受影响。

## 性能影响

- 移除一次同步正则提取（可忽略）。
- 每次 phase 切换增加一次 LLM 压缩调用。
- 最坏情况：用户一口气说完所有信息 → phase 1→3→4 连续跳转 → 2 次额外 LLM 压缩调用（注意：按真实 `infer_phase` 逻辑，destination 直接跳到 3，不经过 2）。压缩输入短小，延迟可接受。
- 逐 tool call 检测可能导致 LLM 产出的多个 tool calls 被中断，未执行的 tool calls 需要 LLM 在下一迭代重新决策，增加迭代次数。

## 不变的部分

- 前端 SSE 事件流格式不变。
- `update_plan_state` 工具参数和行为不变。
- `phase_router.infer_phase` 逻辑不变。
- `BacktrackService.execute` 的状态清理逻辑不变（`clear_downstream`）。
- `parse_dates_value` / `parse_budget_value` 保留供 `update_plan_state` 使用。

## 改变的部分（相对 backtrack 现有机制）

- 现有 backtrack 通过 `needs_rebuild` 延迟到下一次 chat 请求才重建 agent。新设计中，backtrack 在当前 agent loop 内立即生效（中断批次 + 硬上下文边界 + 刷新 tools）。
- `needs_rebuild` 标志仍然保留，用于兜底场景（如 fallback 关键词回退）。
