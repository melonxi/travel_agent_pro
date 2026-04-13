# Travel Agent Pro 失败案例分析

## 方法论

- 测试环境：生产配置（GPT-4o + Claude Sonnet 4）
- 测试方式：真实 API 调用，非 mock
- 测试时间：2026-04-12
- 运行元数据：model、token、cost、latency stats 已记录

## 失败模式分类法

| 失败类别 | 含义 | 示例 |
|---------|------|------|
| LLM 推理 | 模型理解/推理能力不足 | 无法识别特殊人群需求 |
| 工具数据 | 外部 API 返回数据不足或异常 | 无航班搜索结果 |
| 状态机 | 阶段转换/回退逻辑缺陷 | backtrack 未清理下游 |
| 约束传递 | 用户约束未被传递到下游决策 | 饮食约束未进入行程 |
| 设计边界 | 系统设计本身的合理限制 | 不支持多人差异化行程 |

## 场景总览

| # | 场景 | 结果 | 断言通过率 | 关键发现 |
|---|------|------|-----------|---------|
| 1 | 预算极紧 — 5天3000元日本自由行 | ✅ 成功 | 3/3 | 所有断言通过 |
| 2 | 特殊人群 — 带80岁老人去高海拔 | ⚠️ 部分成功 | 1/4 | [contains_text] text '健康' not found in responses |
| 3 | 不可解任务 — 500元马尔代夫5星7天 | ✅ 成功 | 3/3 | 所有断言通过 |
| 4 | 多轮变更 — 京都改成大阪 | ❌ 失败 | 0/1 | [state_field_set] destination=None, expected 大阪 |
| 5 | 约束组合 — 3人春节三亚+素食者 | ✅ 成功 | 2/2 | 所有断言通过 |
| 6 | 极端时间 — 明天就要飞纽约 | ❌ 失败 | 0/1 | [contains_text] text '签证' not found in responses |
| 7 | 模糊意图 — "最近很火的地方" | ❌ 失败 | 0/1 | [tool_called] tool web_search was not called |
| 8 | 贪心行程 — 5城5天 | ⚠️ 部分成功 | 1/2 | [contains_text] text '紧凑' not found in responses |

## 详细分析

### 场景 1: 预算极紧 — 5天3000元日本自由行

**输入**: 我想去日本东京玩5天，预算只有3000块人民币，帮我规划一下

**结果**: ✅ 成功

**断言**: 3/3 通过

**工具调用**: update_plan_state, xiaohongshu_search, web_search

**Agent 回复摘要**: 。...

**失败类别**: 设计边界（已覆盖）

**根因分析**: 这个场景说明现有“明显不可行预算”保护链是生效的：`backend/harness/feasibility.py` 的规则表会在预算/天数极不合理时给出强约束提醒，Phase 1 prompt 也明确要求不要在目的地未收敛前提前推进到机酒预订（`backend/harness/feasibility.py:33-67`, `backend/phase/prompts.py:42-49`）。

**修复状态**: 已验证

**面试话术**: 这个案例证明系统不是“用户一说订票就盲搜”，而是先做可行性判断，能把明显不合理需求拦在昂贵工具调用之前。

---

### 场景 2: 特殊人群 — 带80岁老人去高海拔

**输入**: 我想带我80岁的奶奶去九寨沟玩一周，预算2万

**结果**: ⚠️ 部分成功

**断言**: 1/4 通过

**工具调用**: update_plan_state, xiaohongshu_search

**失败详情**:

- [contains_text] text '健康' not found in responses
- [contains_text] text '替代' not found in responses
- [contains_text] text '医疗' not found in responses

**Agent 回复摘要**: ！...

**失败类别**: LLM 推理

**根因分析**: 系统识别到了“高海拔”这一显性风险，但没有稳定展开到“高龄 + 医疗准备 + 替代方案”三连提示。当前 Phase prompt 更强调目的地收敛和状态同步，没有把“特殊人群安全检查清单”写成硬规则，因此输出仍高度依赖模型临场推理（`backend/phase/prompts.py:4-16`, `backend/phase/prompts.py:42-49`）。

**修复状态**: 待修复

**面试话术**: 这类失败很适合展示“LLM 能感知风险，不代表它会完整执行安全 checklist”，所以要把关键高风险人群规则外显到系统层。

---

### 场景 3: 不可解任务 — 500元马尔代夫5星7天

**输入**: 我只有500块钱，想去马尔代夫住5星级酒店7天

**结果**: ✅ 成功

**断言**: 3/3 通过

**工具调用**: update_plan_state, web_search, xiaohongshu_search

**Agent 回复摘要**: ？...

**失败类别**: 设计边界（已覆盖）

**根因分析**: 这个场景再次验证了 budget feasibility guard 的价值：系统没有被“马尔代夫 + 500 元 + 5 星酒店”这种明显冲突的组合诱导去调用机酒搜索，而是停留在约束判断层（`backend/harness/feasibility.py:33-67`）。

**修复状态**: 已验证

**面试话术**: 与其把 LLM 调成更会说，不如先用便宜、确定性的 feasibility rule 把明显不可能的请求挡掉。

---

### 场景 4: 多轮变更 — 京都改成大阪

**输入**: 我想去东京和京都玩5天，预算15000

**结果**: ❌ 失败

**断言**: 0/1 通过

**工具调用**: update_plan_state, xiaohongshu_search

**失败详情**:

- [state_field_set] destination=None, expected 大阪

**Agent 回复摘要**: ！...

**失败类别**: 状态机

**根因分析**: 当前 backtrack 设计会在回退时调用 `plan.clear_downstream(from_phase=to_phase)`，然后把 phase 切回更早阶段（`backend/phase/backtrack.py:15-28`）。这意味着“中途改主意”在当前实现里更接近“回到目的地收敛重新确认”，而不是“直接把 destination 覆写成新城市”，所以 eval 期望的 `destination=大阪` 与现行状态机语义并不完全一致。

**修复状态**: 设计权衡

**面试话术**: 这个案例不是简单 bug，它暴露的是“回退语义”定义问题：系统到底应该保守清空，还是乐观覆写新目标。

---

### 场景 5: 约束组合 — 3人春节三亚+素食者

**输入**: 我们3个人春节想去三亚玩5天，预算1.5万，其中一个朋友是素食者

**结果**: ✅ 成功

**断言**: 2/2 通过

**工具调用**: web_search, xiaohongshu_search, update_plan_state

**Agent 回复摘要**: 。...

**失败类别**: 约束传递（已覆盖）

**根因分析**: 这个场景验证了多约束输入没有在早期收敛阶段丢失：素食约束被保留在文本输出中，且 `update_plan_state` 被正确调用，说明当前状态写入纪律至少能覆盖“多人 + 季节 + 饮食限制”的组合输入（`backend/phase/prompts.py:42-45`）。

**修复状态**: 已验证

**面试话术**: 多约束场景的价值不在于“答得多漂亮”，而在于能证明约束没有在 phase 切换时被吃掉。

---

### 场景 6: 极端时间 — 明天就要飞纽约

**输入**: 我明天就要飞纽约，帮我订个最便宜的机票和酒店，待一周

**结果**: ❌ 失败

**断言**: 0/1 通过

**工具调用**: update_plan_state, web_search, xiaohongshu_search

**失败详情**:

- [contains_text] text '签证' not found in responses

**Agent 回复摘要**: ！...

**失败类别**: LLM 推理

**根因分析**: 当前系统有预算/天数 feasibility rule，但没有“出发时间过近 → 强制检查签证/准备周期”的确定性规则；Phase 1/3 prompt 也没有把近期开程的签证提醒写成硬门槛。因此模型虽然进入了规划流程，却没有稳定产出“签证”提醒（`backend/harness/feasibility.py:33-67`, `backend/phase/prompts.py:17-33`）。

**修复状态**: 待修复

**面试话术**: 这类失败说明 rule-based gate 的边界很清晰：它擅长预算/天数，不会自动覆盖签证 lead time 这种时效性约束。

---

### 场景 7: 模糊意图 — "最近很火的地方"

**输入**: 想去那个最近很火的地方玩一下

**结果**: ❌ 失败

**断言**: 0/1 通过

**失败详情**:

- [tool_called] tool web_search was not called

**Agent 回复摘要**: ？...

**失败类别**: LLM 推理

**根因分析**: `ToolChoiceDecider` 只对 Phase 3 / Phase 5 做了特殊工具强制，Phase 1 没有“模糊意图必须先搜”的硬触发（`backend/agent/tool_choice.py:17-25`, `backend/agent/tool_choice.py:27-56`）。同时 Phase 1 prompt 还允许对极宽泛意图先用风格分类提问，而不是立刻搜索（`backend/phase/prompts.py:53-60`）。这导致模型可以只靠对话推进而完全不触发 `web_search`。

**修复状态**: 待修复

**面试话术**: 这个案例特别适合说明“工具可用 ≠ 工具一定被调用”，没有 deterministic trigger 的 Phase 1 很容易退化成纯聊天。

---

### 场景 8: 贪心行程 — 5城5天

**输入**: 我想5天玩遍东京、大阪、京都、奈良和神户，预算2万

**结果**: ⚠️ 部分成功

**断言**: 1/2 通过

**工具调用**: xiaohongshu_search, update_plan_state

**失败详情**:

- [contains_text] text '紧凑' not found in responses

**Agent 回复摘要**: 。...

**失败类别**: 设计边界

**根因分析**: 当前 feasibility 层只检查目的地级别的最低预算/最低天数，并没有“5 天 5 城”这种 itinerary density hard check（`backend/harness/feasibility.py:33-67`）。因此系统能继续推进并更新状态，但不会稳定地把“过于紧凑/不合理”外显成强提醒，更多依赖模型临场表达。

**修复状态**: 待修复

**面试话术**: 当系统缺少结构化的“行程密度校验”时，模型即使感觉不合理，也未必会稳定把冲突说透。

---

## 失败模式归类

- **LLM 推理（3 个）**：`failure-002`、`failure-006`、`failure-007`。共同点是模型“理解到一点”，但没有稳定补齐特殊人群 checklist、签证 lead time、或 Phase 1 工具触发。
- **状态机（1 个）**：`failure-004`。本质是 backtrack 语义与 eval 预期不一致：当前实现偏保守清空，而不是直接覆写新目的地。
- **设计边界（1 个失败 + 2 个成功覆盖）**：`failure-008` 暴露了缺少 itinerary density guard；`failure-001` / `failure-003` 则证明预算 feasibility rule 已经覆盖了明显不可行输入。
- **约束传递（1 个成功覆盖）**：`failure-005` 证明多人 + 饮食限制这类组合约束目前能保住，不是主要短板。

## 改进路线图

1. **把高风险 checklist 从 prompt 升级为规则**：为“高龄/儿童/慢病/高海拔/临近出发”增加 deterministic risk flags，避免只靠模型自由发挥。
2. **补 Phase 1 的工具触发策略**：对“极模糊目的地意图”增加最小搜索要求，至少命中一次 `xiaohongshu_search` 或 `web_search` 再进入澄清。
3. **重新定义 backtrack 语义**：明确“改目的地”是应当保守清空重新收敛，还是支持原地覆写；然后让 eval 与状态机保持同一语义。
4. **增加 itinerary density validator**：把“多城市/多区域/多大交通切换”的压缩度检查前移，避免把明显过载的行程只留给模型文案层处理。
