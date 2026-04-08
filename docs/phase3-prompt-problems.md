# Phase 3 提示词问题清单

整理时间：2026-04-08
最后更新：2026-04-09（修复后状态标注）

本文档只记录当前 `backend/phase/prompts.py` 中 phase 3 提示词相关的问题，不包含修复实现。重点不是评价文案是否"好听"，而是确认它是否和运行时机制、状态模型、工具暴露、前端消费方式真正对齐。

涉及文件：
- `backend/phase/prompts.py`
- `backend/context/manager.py`
- `backend/phase/router.py`
- `backend/state/models.py`
- `backend/tools/engine.py`
- `backend/tools/update_plan_state.py`
- `backend/agent/loop.py`
- `frontend/src/components/Phase3Workbench.tsx`
- `frontend/src/types/plan.ts`

## 结论概览

phase 3 当前最大的问题不是提示词长度，而是它把自己写成了一个"严格依赖结构化状态的子阶段状态机"，但运行时并没有把这些结构化状态完整地喂回给模型，也没有把关键字段 schema 钉死。结果是：

- 提示词要求模型严格按状态推进，但模型常常看不见前一轮真正写入了什么。
- 提示词要求模型写结构化产物，但很多字段没有稳定 schema，只能依赖模型自由发挥。
- 提示词把 `phase3_step` 当作主控制杆，但实现里它本质上是衍生状态。
- 系统不得不依赖 repair 机制，在模型已经偏航之后再补救。

---

## 1. Phase 3 看不到自己真正依赖的状态内容

严重程度：高
**状态：✅ 已修复**

修复内容：
- Phase 3 的 `candidate` / `skeleton` / `lock` 子阶段现在会注入 `trip_brief` 完整内容
- `skeleton` / `lock` 子阶段会注入 shortlist 摘要和 preferences/constraints
- `lock` 子阶段会注入已选骨架的完整内容
- 只有 `brief` 子阶段仍保持 count-only（因为此时产物尚在生成中）

修复文件：`backend/context/manager.py`

---

## 2. `skeleton_plans` 缺少稳定 schema，`selected_skeleton_id` 存在悬空风险

严重程度：高
**状态：✅ 已修复**

修复内容：
- 提示词中定义了骨架最小结构化字段：`id`（唯一英文 ID）、`name`（显示名称）、`days`、`tradeoffs`
- 明确要求 `selected_skeleton_id` 必须精确等于骨架的 `id` 字段
- `infer_phase3_step_from_state` 新增悬空校验：当 `selected_skeleton_id` 无法匹配到任何骨架时，回退到 `skeleton` 阶段
- `_find_selected_skeleton()` 使用精确匹配（`id` 或 `name`），不再做模糊子串匹配
- `update_plan_state` 工具描述中强调 `selected_skeleton_id` 必须精确匹配

修复文件：`backend/phase/prompts.py`、`backend/state/models.py`、`backend/tools/update_plan_state.py`

---

## 3. 提示词默许"先说人话，下一轮再补状态"，导致 repair 机制成为常态

严重程度：高
**状态：✅ 已修复**

修复内容：
- 将"同一轮或紧接着的下一轮"改为"必须在同一轮"
- 明确强调"不允许'先说后补'"
- 添加"replace full lists when refreshing"指导，防止 append 造成重复

修复文件：`backend/phase/prompts.py`

---

## 4. `phase3_step` 在提示词中被过度神化，但实现里它只是衍生状态

严重程度：中高
**状态：✅ 已修复**

修复内容：
- 提示词中移除了所有 `update_plan_state(field="phase3_step", value="...")` 的手动更新指示
- 新增说明："`phase3_step` 由系统根据产物状态自动推导，不需要手动维护"
- 每个子阶段的状态写入部分改为"写入关键产物后，系统会自动推进子阶段"
- `loop.py` 的 repair 消息中也移除了 `phase3_step` 手动更新指示
- `update_plan_state` 工具描述中标注"phase3_step 由系统自动推导，通常不需要手动写入"

修复文件：`backend/phase/prompts.py`、`backend/agent/loop.py`、`backend/tools/update_plan_state.py`

---

## 5. `trip_brief` 和候选/骨架字段的 key 太自由，下游消费不稳定

严重程度：中高
**状态：✅ 已修复**

修复内容：
- 提示词中定义了 `trip_brief` 推荐字段名清单：`goal`、`pace`、`departure_city`、`must_do`、`avoid`、`budget_note`
- 明确禁止使用 `from_city`、`depart_from`、`出发地` 等自创字段名
- 骨架方案定义了最小结构化字段（见问题 2 修复）

修复文件：`backend/phase/prompts.py`

---

## 6. prompt 强调"严格按子阶段推进"，但工具边界与上下文缺口会迫使模型跳步

严重程度：中
**状态：✅ 已修复**

修复内容：
- 将"严格按该子阶段推进"改为产物驱动的语言："它反映的是系统根据已形成产物自动推断的子阶段位置"
- 模型的职责明确为"在合适时机写入关键产物"，系统会自动推进
- 整体表述从"严格线性纪律"转变为"产物驱动的推进机制"

修复文件：`backend/phase/prompts.py`

---

## 7. lock 子阶段的"完成标志"和真实 phase 切换条件不完全一致

严重程度：中
**状态：✅ 已修复**

修复内容：
- 完成标志拆分为"必须满足"（dates、selected_skeleton_id、accommodation）和"建议满足"（risks/alternatives、transport_options）
- 必须满足条件与 `PhaseRouter.infer_phase()` 的真实切换条件完全对齐
- 新增警告：`search_flights` 和 `search_trains` 是 Phase 3 专属工具，离开 Phase 3 后不再可用，应在锁定住宿前尽量完成大交通搜索

修复文件：`backend/phase/prompts.py`

---

## 8. 当前 phase 3 更像"右侧工作台填充器"，而不是"真正面向规划决策的协议"

严重程度：中
**状态：⚠️ 部分改善（设计层面）**

说明：
- 通过定义 `trip_brief` 和 `skeleton_plans` 的最小稳定 schema、强化 `selected_skeleton_id` 的精确匹配机制、以及 Phase 3 子阶段上下文注入，产物现在更接近"可可靠引用和复用"而不仅仅是"能展示"
- 但从根本上讲，这是一个架构层面的长期命题，需要在 `update_plan_state` 中增加真正的 schema 校验才能完全解决
- 当前阶段的改善已经显著提升了 Phase 3 → Phase 5 的数据传递可靠性

---

## 已确认问题与推断风险的边界

以下问题已通过代码修复确认解决：

- ✅ phase 3 runtime context 按子阶段注入完整内容（不再是全阶段 count-only）
- ✅ `phase3_step` 改为系统自动推导，prompt 不再要求手动维护
- ✅ `skeleton_plans` 定义最小 schema，`selected_skeleton_id` 悬空时回退到 skeleton 阶段
- ✅ lock 完成标志与真实 phase 切换条件对齐
- ✅ `trip_brief` 定义推荐字段名清单
- ✅ 强制"同一轮写入"，不允许"先说后补"
- ✅ 大交通工具仅限 Phase 3 的警告已添加
- ✅ repair 消息中移除 phase3_step 手动更新指示

以下仍属于长期改进方向：

- `update_plan_state` 缺少真正的 schema 校验层
- 前端对任意 key 的容错展示策略仍较松
- Phase 3 产物的序列化/反序列化没有类型安全保证

---

## 建议的后续处理顺序

当前 8 个问题中 7 个已修复、1 个部分改善。后续如需继续加固：

1. 在 `update_plan_state` 中增加 `trip_brief` 和 `skeleton_plans` 的最小 schema 校验
2. 前端 `Phase3Workbench` 对缺少标准字段名的产物给出 fallback 提示
3. 考虑在 `TravelPlanState` 中给 `trip_brief` 增加 TypedDict 定义
