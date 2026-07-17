# 修复交接:D3/D4 复审五个 P1 + 一个 P2(2026-07-15)

> **给接手实施的话**:这是对 `27c7d5c` 之上那批 D3/D4 修复的**二次修复计划**。外部独立审查确认了 5 个 P1 correctness bug + 1 个 P2,均已由本人对照 file:line 复核成立(见 §0)。本文档给出每个 bug 的**根因 → 修法 → 影响面 → 验证**,可直接实施。**先读 §1 的两条根因归并,很多 bug 同源,别逐个打补丁。**
>
> 基线 commit:`27c7d5c`(HEAD)。被修复的改动在工作区未提交。全量测试基线:`6 failed, 2034 passed`(6 个为预存失败,与本次无关)。前端 `npm run build` 当前通过。

---

## 0. 已确认的 bug 清单(全部复核成立,带证据)

| # | 严重度 | 位置 | 一句话 |
|---|---|---|---|
| B1 | P1 | `candidate_store.py:80-85` | 旧 accepted 候选复活:过滤 rejected 发生在选最新之前,最新被拒 → 回退交付旧版本而非缺天重派 |
| B2 | P1 | `orchestrator.py:1856-1867` + 各重派 attempt 常量 | attempt 号与执行顺序倒挂:取 max attempt 选到时间更早的结果 → MOVE/全局修复静默失效 |
| B3 | P1 | `orchestrator.py:1466-1481` | step7 steering 已 ack 未生效:`active_days=set()` 硬编码,刚跑完的天走 else 只挂 hint 不重派,旧结果随即交付 |
| B4 | P1 | `orchestrator.py:1514-1525` 之后各段 | 最后安全点后 steering 丢失:attempt=5/4/3 各段与提交阶段无 drain,`/steer` 仍返回 queued 但 finally 直接删队列 |
| B5 | P1 | `renegotiation.py:71` | 泛化活动名误合并:location.name 和 name 都登记,两地点 name 同为"博物馆参观"→ 归一成"博物馆"→ 次日误拒丢天 |
| B6 | P2 | `renegotiation.py:311-315` | MOVE 容量把候选池当已选活动:`_capacity_left` 减 candidates 过严 → 常见配置下 MOVE 恒降级 |

---

## 1. 根因归并(先看这个)

**根因 R1 — attempt 号被当"版本序号"用,但它不是**(制造 B1、B2)。
`load_latest_candidates` 用"先按 `accepted_only` 过滤、再取 max `attempt`"来选每天的交付版本。两个前提都错:
- attempt 常量在各阶段是**固定值**(主收集=1、step7=2、8b修复=3、再协商=4、steering=5),赋值顺序与**实际执行顺序相反**(执行顺序 1→2→5→4→3)。max attempt ≠ 最后执行。
- 过滤先于选择,导致"最新 attempt 被拒 → 回退到更早的 accepted",而不是"该天判定为缺失"。
- **约束**:attempt 号被 `worker_start_event_ids[(day, attempt)]` 用作 trace 关联键(19 处),不能随意改各阶段的常量值。

**根因 R2 — `active_days` 语义不完整 + drain 覆盖不全**(制造 B3、B4)。
`_drain_steering` 用 `day in done_days or day in active_days` 判断"是否需要重派"。但 step7 及后续重派段传的是 `active_days=set()`,把"刚跑完但用户想改""正在串行重试"的天全部漏判为 else(只挂 hint)。且最后一次 drain 之后的各重派段/提交阶段完全无 drain,队列里的 steering 直到 stream finally(`stream.py:394`)被静默丢弃。

**根因 R3 — POI 登记键来源过宽**(制造 B5)。
`_activity_poi_keys` 在已有稳定标识(poi_id/place_id/location.name)时,**仍**把自由文本 `activity.name` 派生的 canonical key 一并登记,导致不同地点因活动名撞车而误合并。

按 R1/R2/R3 三处根因修,比逐个打 6 个补丁更彻底。

---

## 2. 逐项修复方案

### R1 → 修 B1 + B2:改"选交付版本"的策略

**目标**:每天交付**最后一次被 accept 的**候选;若最后一次 attempt 被 reject,该天判为**缺失**(交给缺天重派/部分交付),而不是回退旧版本。

**推荐修法(加单调写入序号,不动 attempt 常量,避免碰 trace)**:
1. `candidate_store.py` 写盘 payload 增加一个单调递增字段。写盘无时间戳可用(且项目禁用 `Date.now()`/`time.time()` 一类不确定源需谨慎),改用**该 (session,run) 目录下已有候选数 + 1** 作为 `seq`,或直接复用"写入即最新"语义:因为 `os.replace` 每次覆盖 `day_{day}_attempt_{attempt}.json`,**同一 attempt 会覆盖**,不同 attempt 并存。真正需要的是"哪个 attempt 是这天最后写的"。
2. 最稳妥:写盘时给 payload 记 `seq`(读目录里 `day_{day}_attempt_*.json` 的现有最大 seq + 1);`load_latest_candidates` 选版本改为**按 `seq` 取最大**,而非 `attempt`。
3. `load_latest_candidates(accepted_only=True)` 的语义改为:**先按 seq 选出每天最后写入的候选,再判断它是否 accepted**——若最后写入的是 rejected,该天**不返回**(缺天),而不是回退到更早的 accepted。这是 B1 的核心:过滤与选择的顺序要反过来。

**伪代码**(candidate_store.load_latest_candidates):
```python
latest_by_day = {}
for path in sorted(run_dir.glob("day_*_attempt_*.json")):
    payload = json.loads(...)
    day = int(payload["day"])
    seq = int(payload.get("seq", payload.get("attempt", 0)))  # 兼容旧盘
    cur = latest_by_day.get(day)
    if cur is None or seq > int(cur.get("seq", cur.get("attempt", 0))):
        latest_by_day[day] = payload
# 选出每天"最后写入"后,再按 accepted_only 过滤
result = []
for day in sorted(latest_by_day):
    p = latest_by_day[day]
    if accepted_only and p.get("status") != "accepted":
        continue   # 最后一次不是 accepted → 该天缺失,不回退旧版本
    result.append(p)
return result
```

**影响面**:两个生产调用点 `orchestrator.py:1776`(step8)、`:1858`(8b)。语义从"取 max attempt 的 accepted"变为"取最后写入且 accepted"。**关键回归点**:step8 的 in-memory 兜底 `dayplans = artifact if artifact else [successes]`——B1 修好后,rejected 最新天会从 artifact 缺席,确认它是否应落入缺天重派而非 successes 兜底复活。

**验证**:构造"attempt=1 accepted → 后续重派 rejected"的注入测试,断言该天**缺失并触发缺天处理**,而非交付 attempt=1。现有测试用理想数据可能没覆盖这条。

---

### R2 → 修 B3 + B4:drain 的 active_days 补全 + 补齐 drain 点

**B3 修法**(`orchestrator.py:1466`):step7 重试的 drain 不能传 `active_days=set()`。retry_result 在 1444 已返回但 1481 才进 successes,这段"空窗"里目标天需被识别为可重派。方案二选一:
- 简单:把 step7 当前 task.day 显式并入 `active_days`(如 `active_days={task.day}`),让刚重试的天走 redispatch 分支而非只挂 hint。
- 更一致:把 `_drain_steering` 的判断从"done_days | active_days"扩展为"凡是已派发过(有 attempt 记录)的天,steering 一律走 redispatch",只有从未派发的天才挂 hint。

**B4 修法**(补 drain 点 + 收尾兜底):
- 在最后一次收集循环 drain 之后的各重派段(attempt=5 late-redispatch、attempt=4 再协商、attempt=3 8b 修复)循环内**各加一次 `_drain_steering`**,与 step7 内的 drain 对称。
- **收尾兜底(必须)**:orchestrator run 结束前(或 stream finally pop 队列前),对残留在队列里的 steering 做一次终结处理——至少发一个"未能在本 run 内应用,请重新发送"的 ack/notice,不能静默吞。当前 `stream.py:394` 的 `session.pop("_steer_queue")` 直接丢弃是 B4 的最后一环。
- 契约对齐:`/steer` 已返回 `queued`(不再是 `accepted`),语义上是"入队尽力应用"。B4 修好后要保证"queued 的消息要么被 drain 应用、要么被终结 ack",不存在无声黑洞。

**影响面**:`_drain_steering` 判断逻辑 + orchestrator 各重派段 + stream.py finally。注意 drain 是 generator yield ack,新增 drain 点要 `for ack in ...: yield ack`,别漏 yield。

**验证**:注入"attempt=2 worker 运行期间 put steering"→ 断言该天触发重派(而非旧结果交付);注入"最后 drain 之后 put steering"→ 断言收到终结 ack 而非静默丢弃。复用 `test_steering.py` 思路。

---

### R3 → 修 B5:登记键来源收窄

**修法**(`renegotiation.py:62-82` `_activity_poi_keys`):当 activity 已有稳定标识(poi_id / place_id,或非空 location.name)时,**不再**登记 `activity.name` 派生的 canonical key。只有在没有任何稳定标识时,才回退用 activity.name。
- 具体:`labels` 的构造改为——有 location.name 就 `labels = [location_name]`;否则 `labels = [activity.get("name")]`。poi_id/place_id 已在前面单独处理,优先级最高。
- 理由:activity.name 是自由文本(动词+泛化名),归一化后极易撞车;location.name/poi_id 才是地点身份。

**影响面**:仅 `_activity_poi_keys`。注意 seed_from_locked(`renegotiation.py:177`)仍用 locked POI 名——要确认 locked POI 名经 canonical 后能与 location.name 经 canonical 后匹配上(这是 #2 最初想解决的命名空间一致性)。若 locked 用 POI 名而 activity 只有 location.name,两者 canonical 结果需一致才能正确防重复。

**验证**:注入"两天不同 location.name 但相同泛化 activity.name"→ 断言两天**都被接受**(不误拒);注入"两天相同 location.name"→ 断言第二天被拒(防重复仍生效)。

---

### 修 B6(P2):`_capacity_left` 别把候选池当已选

**修法**(`renegotiation.py:311-315`):candidate_pois 是**备选池**,worker 实际只按 `candidate_activity_slots` 选其中一部分。容量判断不应减去全部 candidate 数。改为只减 locked(恢复原语义),或用 `max_core - locked - min(candidates, candidate_activity_slots)` 之类反映真实占用的口径。需确认 `candidate_activity_slots` 在 renegotiation 上下文是否可得;不可得则退回"只减 locked"。

**影响面**:仅 `_capacity_left` 及其调用点(SUGGEST_MOVE 目标天容量判断)。

**验证**:注入"目标天 locked=1、candidate=3、balanced max=4"的 MOVE → 断言 MOVE 被接受(而非降级 unresolved)。

---

## 3. 实施顺序与风险

1. **B5(R3)** 最独立、最安全,先做——只改一个函数,不碰控制流。
2. **B6** 次之,同样局部。
3. **B1+B2(R1)** 一起做——它们同源,分开改会互相干扰。这是**最高风险**:改选版本策略牵动 step8/8b 交付主链路,务必先补"最新 rejected → 缺天"的注入测试再改,并复核 in-memory 兜底不会复活旧版本。
4. **B3+B4(R2)** 最后做——涉及并发时序与多处 drain 点,改完要跑 steering 集成测试确认"已 ack 必生效或必终结"。

**每步单独验证再进下一步**,不要批量改完一起测。每个 bug 补一个**能真正触发它的**回归测试——现有测试因构造理想数据(activity.name=POI名、fake_worker 不写盘)绕过了这些路径,全绿不能证明修好。

## 4. 完成标准

- 6 个 bug 各有一个触发性回归测试,修复前失败、修复后通过。
- 全量 `python -m pytest backend/tests/ -q` = 6 failed(预存)+ 其余通过,无新增失败。
- 前端 `cd frontend && npm run build` 通过。
- 关键人工核对:B1"最新 rejected 不复活旧版本"、B3"step7 期间 steering 触发重派"、B4"run 尾部 steering 有终结 ack"——这三条最容易改成"看起来修了但没生效",需读代码确认而非只看测试绿。

## 5. 实施结果（2026-07-15）

- B1/B2：candidate artifact 增加 day 级单调 `seq`；先按 `seq` 选最后版本，再检查
  `accepted`。steering / 再协商重派前会作废旧版本，后续 worker 即使未提交 artifact
  也不会复活旧计划。
- B3/B4：step7 drain 将当前重试天视为 active，确保进入 attempt=5；attempt=5/4/3
  收口段和 Agent run 尾部均会 drain，无法应用的消息收到明确终结 ack。
- B5：有 `location.name` 时不再登记自由文本 `activity.name` 身份键。
- B6：MOVE 容量恢复为只计算 locked POI，candidate pool 不视为已占用活动。
- 新增 8 个触发性回归测试。定向回归 `67 passed`；全量后端
  `6 failed, 2042 passed`（6 个为既有失败）；前端构建通过。
