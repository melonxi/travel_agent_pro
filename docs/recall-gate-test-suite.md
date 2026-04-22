# 用户画像召回门控 · 判定框架与测试集

本文档用于回答两个问题：

1. **什么时候需要召回长期用户画像（long-term profile memory）？** —— 给出一个可枚举、可判定的完整框架。
2. **当前 `recall_gate.apply_recall_short_circuit()` 的硬短路机制是否完善？** —— 用一套覆盖面测试集来检验。

当前被测对象：`backend/memory/recall_gate.py::apply_recall_short_circuit`

当前规则（仅枚举，便于对照）：

- `force_recall`（强制召回）：命中 `我是不是说过 / 按我的习惯 / 上次 / 之前 / 以前` 任一关键词。
- `skip_recall`（硬跳过）：同时命中 `(这次|本次|当前)` 和 `(预算|几号|出发|骨架|日期|酒店|航班|车次)` 两类关键词。
- 其余 → `undecided`，交给 LLM gate 判定。

---

## 一、判定框架：什么时候需要召回画像

召回"画像"指向长期记忆中关于用户的**偏好（preference）/ 约束（constraint）/ 过往体验（past trip experience）**，不包括**当前行程事实（current trip facts，写在 `TravelPlanState` 里的东西）**。

### 1.1 需要召回（应当触发 force_recall 或 needs_recall=True）

以下情形中，回答质量严格依赖长期画像，不召回就会出错或失去个性化：

**A. 用户显式要求调用过去信息**
- 直指过去："上次"、"之前"、"以前"、"我之前是不是"、"我是不是说过"。
- 指"我的习惯/常态"："按我的习惯"、"照旧"、"老规矩"、"还是跟平常一样"、"和之前那次一样"。
- 指"我的偏好画像"："按我的偏好"、"按我常规偏好"、"按我能接受的"。

**B. 用户要求个性化决策，但本轮未给足偏好**

判断要点：**谓词是"选 / 推荐 / 安排 / 订哪个 / 哪个更合适 / 帮我定"**，对象是**对个人口味敏感的维度**（酒店位置/档次、航班时段、交通方式、餐饮、节奏、同行人安排等）。

- "帮我选酒店" / "酒店订哪家合适"
- "航班怎么订合适" / "帮我订机票"
- "这几个目的地哪个更适合我"
- "行程节奏帮我排舒服点"
- "推荐几个餐厅" / "吃饭安排一下"

这些句子即使没有"以前 / 上次"等词，也必须召回，因为"合适/舒服/更适合我"这些谓词**本质依赖长期画像**。

**C. 当前选择可能触发长期约束或拒绝项**

当用户问某个即将产生推荐输出的决策（酒店 / 航班 / 车次 / 行程），画像中又存在硬约束（不住民宿、不坐红眼、避免转机、不接受共用卫浴、儿童优先、孕妇禁忌、素食、宠物、预算上限等），**必须召回**以避免输出被画像否决的方案。

这与 1.2 D 区分的关键是：**是否在做面向用户的推荐/安排动作**。

**D. 模糊代词 / 风格词 / 生活化措辞**

这些词本身在当前对话上下文里没有明确定义，必须靠画像兜底：

- "照旧"、"常规偏好"、"老样子"、"还跟以前那样"
- "别太折腾"、"别太累"、"轻松点"、"舒服点"
- "按我能接受的预算"、"按我的节奏"
- "像我平时喜欢的"、"像上次那样"、"像那次一样"
- "你懂的那种"

**E. 表面含"这次 / 酒店 / 航班"等事实性词，但实际在问偏好匹配**

典型反例：

- "这次酒店还是按我以前不住民宿的习惯吗？"
- "这次航班还是避开红眼吧？"
- "这次预算还按上次那个水平？"

→ 这些绝不能被"这次+酒店/航班/预算"模式硬跳过。判定核心是句子里是否同时出现"偏好/习惯/以前/还是/仍然/照旧"等画像信号。

### 1.2 不需要召回（应当硬跳过或 needs_recall=False）

判断要点：**问题可以由 `TravelPlanState` + 本轮上下文完整回答，不依赖用户长期偏好**。

**F. 纯粹询问当前行程事实**
- "这次预算多少？" / "现在预算是多少"
- "目的地定了吗？" / "现在目的地是哪？"
- "选的是哪个骨架？"
- "出发日期是几号？"
- "这次是几天？"
- "当前航班订的是哪一班？"

**G. 工具/系统元问题（与用户画像无关）**
- "你能做什么？"
- "继续" / "好的" / "嗯"
- "重新开始"

**H. 对上一轮 Agent 输出的确认 / 否认（不引入新偏好语义）**
- "可以"、"就这个"、"OK"
- "不行，换一个"（单独出现，不带偏好词）

### 1.3 交由 LLM gate 裁决（undecided）

当句子同时含有当前事实词和偏好信号，或语义需要轻量语义理解才能分辨 B/C/D 类时，硬短路应保持 `undecided`，让 LLM gate 承担最后判定。硬短路只处理**高置信**的两端。

### 1.4 设计原则（给硬短路的红线）

1. **force_recall 优先级必须高于 skip_recall**：句子里一旦出现 A/E 类信号，无论是否同时出现"这次+酒店"，都必须 force_recall。
2. **skip_recall 仅在纯事实问句场景生效**：需要满足"没有偏好/风格/习惯信号 + 没有推荐动词 + 命中事实关键词"。
3. **宁可 undecided，不要误 skip**：误 skip 会直接让画像失效并输出冲突方案；误 force_recall 只是多一次检索，代价低得多。

---

## 二、测试集：评估当前硬短路机制

下表每条用例格式：`输入 | 期望决策 | 期望 reason | 分类 | 当前实现通过？ | 备注`。

"当前实现通过？"一列基于 `apply_recall_short_circuit` 当前代码推算（`force_recall` 关键词：我是不是说过 / 按我的习惯 / 上次 / 之前 / 以前；`skip_recall`：`(这次|本次|当前)` ∩ `(预算|几号|出发|骨架|日期|酒店|航班|车次)`）。

### 2.1 force_recall（应当强制召回）

| # | 输入 | 期望决策 | 期望 reason | 分类 | 当前实现通过？ | 备注 |
|---|---|---|---|---|---|---|
| R1 | 我是不是说过不坐红眼航班？ | force_recall | explicit_profile_history_query | A | ✅ | baseline |
| R2 | 按我的习惯来 | force_recall | explicit_profile_history_query | A | ✅ | |
| R3 | 还是按我以前喜欢的节奏 | force_recall | explicit_profile_history_query | A | ✅ | "以前"命中 |
| R4 | 像上次那样安排 | force_recall | explicit_profile_history_query | A | ✅ | |
| R5 | 这次酒店还是按我以前不住民宿的习惯吗？ | force_recall | explicit_profile_history_query | E | ✅ | 关键：force 优先级必须高于 skip |
| R6 | 这次航班还是避开红眼吧，跟之前一样 | force_recall | explicit_profile_history_query | E | ✅ | "之前"命中 |
| R7 | 照旧安排就行 | force_recall | 画像风格词 | D | ✅ | |
| R8 | 老样子，别太折腾 | force_recall | 画像风格词 | D | ✅ | |
| R9 | 老规矩就行 | force_recall | 画像风格词 | D | ✅ | |
| R10 | 按我常规偏好来 | force_recall 或 undecided | — | D | ✅ | STYLE 词 "常规偏好" 现命中 P1 force_recall |
| R11 | 按我能接受的预算安排 | force_recall 或 undecided | — | D | ⚠️ | 含"预算"事实词但语义是画像；当前 undecided，可接受 |
| R12 | 像我平时喜欢的那种 | force_recall | 画像风格词 | D | ✅ | |
| R13 | 安排得像我平时喜欢的 | force_recall | 画像风格词 | D | ✅ | |

### 2.2 undecided（应当交给 LLM gate）

| # | 输入 | 期望决策 | 分类 | 当前实现通过？ | 备注 |
|---|---|---|---|---|---|
| U1 | 帮我选酒店 | undecided | B | ✅ | 需 LLM 判定 profile_preference_recall |
| U2 | 航班怎么订合适 | undecided | B | ✅ | |
| U3 | 这几个目的地哪个更适合我 | undecided | B | ✅ | |
| U4 | 行程排舒服点 | undecided | B | ✅ | |
| U5 | 推荐几个餐厅 | undecided | B | ✅ | |
| U6 | 别太累 | undecided | D | ✅ | 风格词但无事实词，交 LLM |
| U7 | 这次按我常规偏好安排 | undecided | D+ | ✅ → force_recall | 行为已变更：STYLE 词 "常规偏好" 现命中 P1 force_recall |
| U8 | 还是按我常规偏好来 | undecided | D | ✅ → force_recall | 行为已变更：STYLE 词 "常规偏好" 现命中 P1 force_recall |

### 2.3 skip_recall（应当硬跳过）

| # | 输入 | 期望决策 | 期望 reason | 分类 | 当前实现通过？ | 备注 |
|---|---|---|---|---|---|---|
| S1 | 这次预算多少？ | skip_recall | current_trip_fact_question | F | ✅ | baseline |
| S2 | 当前预算是多少 | skip_recall | current_trip_fact_question | F | ✅ | |
| S3 | 本次出发是几号？ | skip_recall | current_trip_fact_question | F | ✅ | |
| S4 | 这次选的是哪个骨架 | skip_recall | current_trip_fact_question | F | ✅ | |
| S5 | 这次订的航班是哪一班 | skip_recall | current_trip_fact_question | F | ✅ | |

### 2.4 ⚠️ 反例：当前 skip_recall 可能**误伤**的场景

这是测试集的核心价值——暴露 skip_recall 过宽的风险。

| # | 输入 | 期望决策 | 分类 | 当前实现？ | 结论 |
|---|---|---|---|---|---|
| X1 | 这次酒店订哪里？ | undecided（需交 LLM 判断是否召回约束） | C | ✅ | 已修复：P2 RECOMMEND 命中，降级为 undecided |
| X2 | 这次航班怎么订？ | undecided | C | ✅ | 已修复：P2 RECOMMEND 命中（"怎么订"）|
| X3 | 这次酒店帮我推荐一个 | undecided | B+C | ✅ | 已修复：P2 RECOMMEND 命中 |
| X4 | 这次车次选哪趟合适？ | undecided | C | ✅ | 已修复：P2 RECOMMEND 命中 |
| X5 | 当前酒店换一家吧 | undecided | C | ✅ | 已修复：P2 RECOMMEND 命中（"换一家"）|
| X6 | 本次日期能不能调整得轻松点 | undecided | C+D | ✅ | 已修复：P1 force_recall（STYLE 词 "轻松点" 命中）|

**建议修复方向（供后续实现参考，本文档只列出预期行为）**：
- 在 skip_recall 生效前再做一次反向检查：若句子含"选 / 推荐 / 订哪 / 哪家 / 哪趟 / 哪个 / 合适 / 舒服 / 轻松 / 换 / 帮我"等推荐动词/风格词，则降级为 undecided。
- 扩充 force_recall 关键词表：`照旧 / 老样子 / 老规矩 / 常规偏好 / 平时喜欢 / 像那次 / 像上次`。

### 2.5 边界与对抗用例

| # | 输入 | 期望决策 | 备注 |
|---|---|---|---|
| B1 | （空字符串） | undecided (reason=needs_llm_gate) | 现已覆盖于分支，但需要显式测试 |
| B2 | "   " （仅空白） | undecided | 同上 |
| B3 | 这次 | undecided | 单独一个"这次"不应命中 skip |
| B4 | 预算 | undecided | 单独一个事实词不应命中 skip |
| B5 | 这次以前定过酒店吗 | force_recall | ✅ "以前"必须优先，即使有"这次+酒店" |
| B6 | 上次那个酒店这次还订吗 | force_recall | ✅ "上次"必须优先 |
| B7 | 这次预算和上次一样 | force_recall | ✅ "上次"必须优先，不能因"这次+预算"而 skip |
| B8 | 你能做什么 | undecided | G 类，交 LLM gate（或未来加系统元短路） |
| B9 | 继续 | undecided | H 类 |
| B10 | OK 就这个 | undecided | H 类 |

---

## 三、使用方式

1. 把本文档 2.1–2.5 的表格作为评估基线。
2. 实施脚本化评估：对每条用例调 `apply_recall_short_circuit(input)`，断言 `decision` 与 `reason` 符合"期望"列；对"当前实现？"标 ❌ 的用例，允许先 xfail，修复后转绿。
3. 修复优先级建议：**先堵误 skip（X1–X6 + B5–B7）**，因为它会直接让画像失效；其次补 force_recall 关键词（R7–R13）。
4. 对于 `undecided` 用例，建议在 LLM gate 层再搭一个独立测试集，验证 `profile_preference_recall / profile_constraint_recall / current_trip_fact` 的分类准确率。

---

## 四、关联文件

- 实现：`backend/memory/recall_gate.py`
- 既有单元测试：`backend/tests/test_recall_gate.py`
- 召回管线下游：`backend/memory/recall_query.py`、`backend/memory/retrieval_candidates.py`、`backend/memory/recall_reranker.py`
- 策略/格式化：`backend/memory/policy.py`、`backend/memory/formatter.py`

---

## 五、重设计后状态（2026-04-23）

本测试套件对应实现已在 `feature/recall-gate-redesign` 分支完成重构：

- Layer 1: `backend/memory/recall_signals.py`
- Layer 2: `backend/memory/recall_gate.py::apply_recall_short_circuit`（P1–P6）
- Layer 3: `decide_memory_recall` tool 保持不变

**行为变更补记**（对 Spec §3 表格的补充）：

- `还是按我常规偏好来` / `这次按我常规偏好安排` / `还是按我常规偏好来` 类：
  STYLE 词 `常规偏好` 现命中 P1，统一走 force_recall。测试用例 U7/U8 的期望已同步更新。
- `怎么订` 归入 RECOMMEND 词表，修正 X2 漏洞。

所有 2.1 / 2.3 / 2.4 / 2.5 表格中列出的用例均有对应 pytest 参数化覆盖；
2.2 undecided 用例保留给 LLM gate，不在硬短路强断言范围。
