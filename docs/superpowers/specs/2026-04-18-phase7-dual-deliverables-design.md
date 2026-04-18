# Phase 7 双文档交付设计

日期：2026-04-18
状态：待审阅
关联计划草稿：`docs/superpowers/plans/phase7-fluttering-hollerith.md`

---

## 1. 背景与问题

当前 Phase 7 结束时，系统只通过 `generate_summary` 产出一段结构化摘要文本。它能帮助用户快速回看方案，但不能作为正式交付物保存，也没有稳定的下载入口。

这带来三个实际问题：

1. 用户离开当前会话后，缺少可直接带走的最终文档。
2. 摘要文本不是交付导向格式，无法自然承载完整旅行计划和出发前清单。
3. Phase 7 没有“冻结产物”概念，用户和系统都无法区分“正式交付版本”和“仍在聊天中的中间内容”。

本设计的目标是把 Phase 7 的结束动作升级为一次正式交付：生成两份可下载 Markdown 文件，并在生成成功后冻结，只有回退后重新跑到 Phase 7 才允许更新。

---

## 2. 目标

### 2.1 本次要实现的能力

- 在 Phase 7 成功结束时一次性交付两份 Markdown 文档：
  - `travel_plan.md`
  - `checklist.md`
- 两份文档都提供下载链接。
- 文档生成成功后冻结，不允许在当前状态上重复覆盖。
- 当用户回退到更早阶段时，清除已有交付物；重新跑完 Phase 7 后才允许重新生成。

### 2.2 明确不做

- 不做页面内 Markdown 预览。
- 不新增独立的“交付物事件”SSE 协议。
- 不引入后端 markdown renderer；两份文档都由 LLM 直接生成。
- 不新增预订号、票号、订单号等结构化状态字段。
- 不把交付物历史版本纳入版本管理；当前只保留“当前正式版本”。

---

## 3. 用户决策与约束

以下约束已经明确，后续实现必须遵守：

- **交付形态**：只提供下载链接，不做站内预览。
- **生成策略**：采用 `LLM 双文档直出`，由 Phase 7 最终工具一次提交两份 Markdown。
- **冻结策略**：一旦生成成功就冻结；只有回退后重新跑 Phase 7 才能更新。
- **交付时机**：只在 Phase 7 完成 Gate 通过后生成，不提前暴露半成品。

---

## 4. 方案比较与选择

### 4.1 方案 A：LLM 双文档直出

Phase 7 末尾由模型一次生成 `travel_plan_markdown` 和 `checklist_markdown`，工具校验后由后端保存和冻结。

优点：

- 改动路径短，最贴近现有 `generate_summary` 位置。
- 不需要引入新的渲染层和模板系统。
- `checklist.md` 与 `travel_plan.md` 的写作风格可保持一致。

缺点：

- 文档内容一致性比“状态渲染”方案弱，需要 prompt 约束和工具校验兜底。

### 4.2 方案 B：后端渲染 travel plan + LLM 生成 checklist

`travel_plan.md` 从状态确定性渲染，`checklist.md` 由 LLM 生成。

优点：

- 旅行计划和状态的一致性最强。

缺点：

- 需要新增 renderer、模板约束和额外测试面。
- 与本次“快速补上正式交付能力”的目标不匹配。

### 4.3 方案 C：后端全模板生成

两份文档都转成结构化数据后由后端模板生成。

优点：

- 最可控、最稳定。

缺点：

- 需要重做 Phase 7 工具契约和提示词，投入明显更大。

### 4.4 选择

本次选择 **方案 A：LLM 双文档直出**。

原因：

- 它满足“只提供下载链接”和“冻结正式交付物”这两个核心需求。
- 它复用现有 Phase 7 的最终工具节点，改动集中。
- 它把复杂度控制在 prompt、tool schema、保存链路和前端显示四个局部点上，适合作为第一版正式交付能力。

---

## 5. 最终设计

### 5.1 交付物模型

`TravelPlanState` 新增 `deliverables` 字段，用于描述当前 session 是否已有正式交付物。

建议结构：

```python
deliverables = {
    "travel_plan_md": "travel_plan.md",
    "checklist_md": "checklist.md",
    "generated_at": "2026-04-18T22:30:00+08:00",
}
```

约束：

- 该字段只表示“当前正式版本”的存在与文件名，不记录历史版本。
- 当字段为 `None` 时，表示当前 session 尚无正式交付物。
- 当字段非空时，表示交付物已经冻结。

### 5.2 文件落盘位置

两份文件统一存放到：

`backend/data/sessions/<session_id>/deliverables/`

文件名固定为：

- `travel_plan.md`
- `checklist.md`

不允许动态文件名。这样可以避免路径注入、前后端对齐复杂化和历史版本管理膨胀。

### 5.3 Phase 7 最终工具契约

`generate_summary` 改造为 Phase 7 的最终交付工具。它仍保留现有“结束阶段”的语义，但入参和返回值升级为双文档提交。

建议输入：

```json
{
  "plan_data": {
    "destination": "东京",
    "dates": { "start": "2026-05-01", "end": "2026-05-05" }
  },
  "travel_plan_markdown": "# 东京 5 日旅行计划\n\n## 第 1 天\n- 浅草寺",
  "checklist_markdown": "# 东京出发前清单\n\n- [ ] 护照和酒店确认单"
}
```

建议输出：

```json
{
  "summary": "已生成并冻结 travel_plan.md 与 checklist.md",
  "travel_plan_markdown": "# 东京 5 日旅行计划\n\n## 第 1 天\n- 浅草寺",
  "checklist_markdown": "# 东京出发前清单\n\n- [ ] 护照和酒店确认单"
}
```

工具职责边界：

- **Prompt 负责**让模型写出两份文档。
- **Tool 负责**做参数校验、冻结校验和基本结构校验。
- **main.py 负责**持久化、状态写入和最终冻结。

### 5.4 Prompt 约束

`PHASE7_PROMPT` 需要从“生成摘要”调整为“提交正式交付物”。必须强调：

1. 最终必须调用 `generate_summary`，并提交两份完整 Markdown。
2. `travel_plan_markdown` 必须基于当前状态中已确认的信息：
   - `destination`
   - `dates`
   - `daily_plans`
   - `accommodation`
   - `selected_transport`
3. `checklist_markdown` 必须基于本轮 Phase 7 真实搜到的信息生成，尤其是：
   - `check_weather`
   - `search_travel_services`
   - 必要时的 `web_search`
4. 不能编造：
   - 票号、预订号、订单号
   - 未确认的价格
   - 未搜索到的链接
   - 未验证的天气或政策信息
5. 如果已有冻结交付物，不应再次尝试生成；应明确提示需要先回退。

### 5.5 Tool 校验规则

`generate_summary` 至少执行以下校验：

- `travel_plan_markdown` 必填，且为非空字符串。
- `checklist_markdown` 必填，且为非空字符串。
- 当前 `plan.deliverables` 已存在时，直接返回错误：
  - `error_code = "DELIVERABLES_FROZEN"`
- `travel_plan_markdown` 必须包含：
  - 一级标题
  - 至少一个逐日 section
- `checklist_markdown` 必须包含：
  - 一级标题
  - 至少一个列表项或 checklist 项

这里的结构校验只做轻量格式约束，不做复杂语义理解。目标是过滤明显错误输入，而不是把工具演化成文档解析器。

### 5.6 冻结与解冻规则

冻结规则：

- 当 `plan.deliverables` 为 `None` 时，允许生成。
- 当 `plan.deliverables` 非空时，禁止重复生成和覆盖。

解冻规则：

- 只有用户回退到更早阶段时，系统才清理：
  - `plan.deliverables`
  - `deliverables/` 目录下的正式交付文件
- 清理完成后，重新进入 Phase 7 并成功调用 `generate_summary`，才会形成新一轮正式交付。

这保证“正式交付物”与“当前已锁定计划状态”一一对应，避免同一状态下被反复覆盖。

### 5.7 主链路时序

成功路径：

1. Phase 7 完成信息收集与查漏。
2. LLM 调用 `generate_summary(plan_data, travel_plan_markdown, checklist_markdown)`。
3. Tool 校验参数、结构和冻结状态。
4. `main.py` 接收成功返回后：
   - 保存 `travel_plan.md`
   - 保存 `checklist.md`
   - 更新 `plan.deliverables`
   - `state_mgr.save(plan)`
5. 现有 `state_update` SSE 把新 plan 推给前端。
6. 前端根据 `plan.deliverables` 显示两个下载链接。

失败路径：

- 任一步失败都不更新 `plan.deliverables`。
- 不产生半冻结状态。
- 不显示下载链接。

### 5.8 下载接口

新增接口：

`GET /api/sessions/{session_id}/deliverables/{filename}`

约束：

- `filename` 只能是：
  - `travel_plan.md`
  - `checklist.md`
- session 必须存在且未被 soft delete。
- 文件不存在返回 404。

响应要求：

- `Content-Type: text/markdown; charset=utf-8`
- `Content-Disposition: attachment`

该接口只承担下载职责，不承担预览格式化职责。

### 5.9 前端展示

前端不新增预览页和专用事件类型，只做最小展示：

- `TravelPlanState` 类型新增 `deliverables`
- 当 `plan.deliverables` 存在时，在计划面板显示两个下载链接
- 会话切换、刷新页面、恢复历史 session 时，只要加载到的 `plan.deliverables` 非空，就立即回显链接

不新增 `deliverable_ready` SSE 事件。原因是现有 `state_update` 已能覆盖这个场景，额外协议只会增加前后端分叉状态。

---

## 6. 对现有系统的影响

### 6.1 后端

需要修改的核心点：

- `backend/tools/generate_summary.py`
- `backend/main.py`
- `backend/state/models.py`
- `backend/state/manager.py`
- `backend/phase/backtrack.py` 或其调用链

### 6.2 前端

需要修改的核心点：

- `frontend/src/types/plan.ts`
- 计划视图所在组件（展示下载链接）
- session 切换后的 plan 回显路径

### 6.3 文档

实现完成后需要更新 `PROJECT_OVERVIEW.md`，反映：

- Phase 7 结束后可生成并冻结双 Markdown 交付物
- 新增下载 API
- `TravelPlanState.deliverables` 的存在

---

## 7. 风险与缓解

### 风险 A：LLM 生成的 `travel_plan.md` 与状态不一致

原因：

- 本方案不做后端渲染，而是让模型直接写文档。

缓解：

- Prompt 明确禁止编造未确认信息。
- Tool 做基础结构校验。
- 文档冻结前仍由当前 plan 状态驱动生成，避免脱离上下文。

### 风险 B：用户生成成功后又想微调文案

原因：

- 冻结策略不允许当前状态下重复覆盖。

缓解：

- 产品规则上明确：想更新正式交付物，先回退再重跑 Phase 7。
- 保持规则简单可理解，不引入“手动解冻”分支。

### 风险 C：保存一半导致状态和文件不一致

原因：

- 文件写盘和 `plan.deliverables` 更新是多步操作。

缓解：

- 以“先写文件，后写状态”为顺序。
- 任一步失败都不写入 `plan.deliverables`。
- 避免前端读到存在元数据但文件缺失的状态。

### 风险 D：前端展示和真实文件状态脱节

原因：

- 如果只靠前端本地状态更新，可能出现短暂不一致。

缓解：

- 继续以服务端 `state_update` 和会话重载后的 `plan` 为唯一真相。

---

## 8. 验收标准

1. 在无冻结交付物的 Phase 7 session 中，成功调用 `generate_summary` 后会生成两份 Markdown 文件并写入 `plan.deliverables`。
2. 前端在收到更新后的 plan 后显示两个下载链接。
3. 页面刷新或切换回该 session 时，下载链接仍然存在。
4. 当 `plan.deliverables` 已存在时，再次调用 `generate_summary` 会得到 `DELIVERABLES_FROZEN` 错误。
5. 执行回退后，`plan.deliverables` 被清空，磁盘中的正式交付文件被删除。
6. 回退后重新跑完 Phase 7，可以再次成功生成新的正式交付物。
7. 下载接口只允许下载白名单文件，且对不存在或已删除 session 正确返回错误。

---

## 9. 实现边界结论

本次设计把 Phase 7 的末尾从“生成一段摘要”升级为“提交并冻结两份正式交付文档”。它故意选择了实现成本更低的 `LLM 双文档直出` 路线，而不是更重的后端渲染方案。

这不是最终形态，但它是一个边界清晰、改动集中、可测试、可逐步迭代的第一版：

- 先把正式交付物能力补上
- 先把冻结/解冻语义立住
- 先把下载入口稳定下来

如果后续验证发现 LLM 直出的 `travel_plan.md` 一致性不足，再单独设计“状态渲染版 travel plan”替换它，而不需要推翻本次的冻结机制和下载链路。
