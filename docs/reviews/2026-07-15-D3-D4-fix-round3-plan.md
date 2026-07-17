# 修复交接:D3/D4 round2 复审——1 个新引入 P1 + 2 个 MEDIUM(2026-07-15)

> **给接手实施的话**:这是对 `2026-07-15-D3-D4-fix-round2-plan.md` 那批修复的**第三轮复审结论**。round2 计划里的 6 个 bug(B1–B6)**修复方向全部正确、已确认到位**。但复审发现 round2 的修法**新引入了一个性质更严重的 P1 丢天 bug**(B2"主动作废旧候选"缺回滚兜底),外加 2 个 MEDIUM(B5 收窄的反向回归、本轮顺带新增的边界校验误拒)和 3 个 LOW。本文档只规划这一轮要修的项。
>
> 基线:round2 修复在工作区未提交(HEAD=`27c7d5c`)。全量测试 `6 failed, 2042 passed`(6 个预存)。前端构建通过。**先读 §1 的 P1,它是本轮唯一必修项,且最容易只补一处漏两处。**

---

## 0. 本轮要修的清单

| # | 严重度 | 位置 | 一句话 |
|---|---|---|---|
| C1 | **P1** | `orchestrator.py:1367 / 1548 / 1683`(三个重派段)+ 失败分支 | 重派前乐观作废旧候选,重派失败时不回滚 → 该天 step8 缺席 + coverage_gap 仅 warning 不触发兜底 → **静默丢天** |
| C2 | MEDIUM | `renegotiation.py:74` | POI 收窄判据是"location.name 非空"而非"能产出有效 key" → 泛化/异写 location.name 时丢弃 activity.name 里的真实 POI → 跨天重复漏拦 |
| C3 | MEDIUM | `renegotiation.py:246` | 新边界校验 `first_start/last_end` 取列表首末活动,依赖时序排序但未排序 → 乱序/跨午夜活动误拒合法日 |
| C4 | LOW | `stream.py:347/355` | `except Exception` 抓不到 `CancelledError/GeneratorExit` → 取消/断连路径 steering 走静默 clear 无终结 ack |
| C5 | LOW | `stream.py:297` vs `356` | cancelled 子路径先 yield done 再 yield 终结 ack,客户端可能漏读 |
| C6 | LOW | `steering.py:23` | `MAX_STEER_QUEUE_SIZE=64` 常量未被引用,`stream.py:92` 硬编码 64,日后改常量不生效 |

**round2 的 B1–B6 不用再动**,已确认修好:B1 seq 选版本、B2 主动作废(但见 C1)、B3 step7 active_days、B4 收尾终结 ack(正常+普通异常路径)、B5 收窄生效、B6 容量。

---

## 1. C1(P1,必修):重派失败静默丢天

### 触发链(已核实,完整闭合)

1. 用户对某天下 steering,或 worker 上报 SUGGEST_MOVE/OVERLOADED → 触发重派。
2. 重派前该天被乐观作废:`candidate_store.update_latest_candidate_status(day, status="rejected", reason="superseded by ...")` + `self._blackboard.release_day(day)` + 从 `successes` 移除。三处:
   - steering redispatch:`orchestrator.py:1367`
   - late steering redispatch:`orchestrator.py:1548`
   - 骨架再协商:`orchestrator.py:1683`
   - (8b repair 段也有 `release_day` + 作废语义,同源)
3. 重派 worker **失败**(`rd_result.success=False`):`JSON_EMIT_FAILED` / 超时 / 再次不可行——**这在 Phase 3 是常见失败,不是极端情况**。
4. 失败分支(如再协商段 `orchestrator.py:1776-1787`)只把 `worker_statuses[idx]["status"]="failed"`,**不回滚 rejected 标记、不把该天恢复进 successes**。
5. step 8 汇总:`load_latest_candidates(accepted_only=True)`(`:1828`)→ 该天磁盘最新是 rejected(或 attempt=N 无 accepted 文件)→ 被跳过 → `dayplans` 缺席。in-memory 兜底 `dayplans = artifact if artifact else [successes]`(`:1833-1840`)也救不回:successes 已移除该天,且只要其他天有 artifact 就不走 successes 分支。
6. `_global_validate` 检出缺天:`coverage_gap`,但 **severity=`"warning"`**(`orchestrator.py:859-862`)。
7. 8b 重派入口只认 error:`error_issues = [i for i in issues if i.severity == "error"]`(`:1881`),local_days/serial_days 都从 error_issues 派生 → **coverage_gap 是 warning,不触发任何兜底重派**。
8. 结果:该天**永久缺席,静默丢失**。

### 为什么比原 bug 更糟

原 B1 是"最新被拒 → 回退旧版本"(用户至少拿到旧行程)。C1 是"再协商本意改进某天 → 一次重派失败反而把这天从交付里彻底抹掉"(用户啥都没有)。**乐观作废 + 无回滚 + coverage_gap 不阻断**三者叠加。

### 根因

B2 的修法(重派前主动作废旧候选,让 seq/accepted_only 选到新版本)默认"重派一定成功"。作废是乐观的,但重派会失败,失败路径缺一个回滚/兜底闭环。三个重派段(steering / late-steering / 再协商)+ 8b repair 都是同一模式,**必须一起修,别只补一处**。

### 修法(三选一,建议 A 或 C)

- **A(最稳,推荐):重派失败即回滚作废**。失败分支里调 `update_latest_candidate_status(day, status="accepted", ...)` 把上一版本恢复为 accepted(恢复旧版本总比丢天好),并把旧 result 恢复进 successes。需要在作废前把"旧 accepted 候选的 attempt/seq"记下来以便回滚(或直接把最近一次 accepted 版本重新标 accepted)。
- **B:coverage_gap 升级为可兜底**。把再协商/steering 重派失败导致的缺天,在 `_global_validate` 里升为 `severity="error"`(或单独 issue_type),使 8b 的 error_issues 兜底重派能捡起它。风险:8b 兜底本身也可能失败,可能死循环,需配熔断。
- **C:失败天并入 step-8 后的缺天串行兜底**。在 step 8 之后、最终 handoff 之前,对"作废了但重派失败"的天做一次保守串行兜底(用旧候选或降级占位),确保 dayplans 天数完整。

**共性要求**:无论哪个方案,三个重派段 + 8b repair 的失败分支都要覆盖到。建议抽一个 `_handle_redispatch_failure(day, prev_accepted_ref)` 统一处理,避免四处各写一遍又漏。

### 验证(必须补触发性回归测试)

- 构造"某天触发再协商 → 作废旧候选 → attempt=4 worker 返回 success=False"的注入测试,断言:该天**最终出现在交付 dayplans 里**(回滚旧版本或兜底占位),而非缺席。
- 对 steering redispatch(attempt=5)、late-steering、8b repair 各补一个同构测试——它们是不同代码段,一个测试覆盖不了。
- 现有测试用"重派必成功"的理想数据,**没有覆盖重派失败路径**,全绿不能证明 C1 修好。

---

## 2. C2(MEDIUM):POI 收窄的反向回归——该拦的没拦

### 问题

`renegotiation.py:74`:
```python
labels = [location_name] if isinstance(location_name, str) and location_name.strip() else [activity.get("name")]
```
判据只是"location.name 非空字符串",不判它能否产出有效 canonical key。两种真实漏拦:

1. **location.name 非空但泛化/不可用**:location.name="自由活动"/"市区"(非空)→ labels 只含它 → `canonical_poi_key` 返回 None(泛化名集合)→ 该活动登记**零个 key**。而 activity.name 里可能承载真实 POI("浅草寺")被完全丢弃 → 跨天重复检测对该活动失效。
2. **location.name 异写、activity.name 恰好匹配 locked**:seed 按 locked 串("浅草寺")登记 canonical;worker 的 location.name 是变体("浅草观音寺"/英文名)canonical 不等于锁定名,而 activity.name="浅草寺" 本可命中 → 却因分支丢弃 activity.name → **同一 POI 两天都过**。

### 修法

判据从"location.name 非空"改为"location.name 能产出有效 canonical key":location.name 产不出 key(None)时**回退用 activity.name**。伪代码:
```python
labels = []
loc_key = canonical_poi_key(location_name)
if loc_key:
    labels = [location_name]
else:
    labels = [activity.get("name")]
```
或更稳:两者都尝试但优先 location.name,只有 location.name 无效时才用 activity.name。注意别退回到 round2 之前"两者都登记"的老 bug(那会误合并)。

### 验证

- 注入"location.name='自由活动'(泛化)、activity.name='浅草寺'(真实 POI),两天都用"→ 断言第二天被拒(防重复生效)。
- 保留 round2 的 `test_same_generic_activity_name_does_not_merge_distinct_locations`(两个不同 location.name + 相同泛化 activity.name)仍两天都接受,确认没退回老 bug。

---

## 3. C3(MEDIUM):边界校验对乱序/跨午夜误拒

### 问题

`renegotiation.py:246` 新校验 `if first_start and last_end and _time_cmp(first_start, last_end) > 0: return False`(判"首活动晚于末活动")。但 `first_start` 取 activities **列表首个**活动 start、`last_end` 取**列表末个**活动 end(`:222-225`),**依赖 activities 已按时间排序**,代码里没有排序:
- **乱序**:activities=[20:00 活动, 09:00 活动] → first_start=20:00 > last_end(第二个的 end,如 10:00)→ 误判非法拒绝合法日。
- **跨午夜**:末活动 22:00→次日 01:00,end_time 写 "01:00"(非 25:00 约定)→ `_time_cmp("09:00","01:00")>0` → 误拒合法夜间行程。

后果:`try_accept_dayplan` 返回 False → 合法 dayplan 被拒 → 触发重派/降级(且叠加 C1 时,这个误拒的重派若失败又会丢天)。

### 修法

- 校验前对 activities 按 start_time 排序,再取真正的最早 start 和最晚 end;或用 `min(start_times)` / `max(end_times)` 而非列表首末。
- 跨午夜:要么明确契约(end < start 视为跨午夜,+1440 处理),要么这个校验对"末活动 end < 首活动 start"的情况放行而非拒绝(宁可漏报不误报,因为它只是防御性校验)。
- 这个校验是本轮**顺带新增**的(不在 round2 的 B1–B6 内),若拿不准跨午夜语义,保守做法是放宽:仅当能确定同日时序矛盾才拒。

### 验证

- 注入乱序 activities → 断言不被误拒。
- 注入跨午夜(end="01:00" < start="09:00")→ 断言不被误拒。
- 保留 round2 的 `test_invalid_day_boundary_is_rejected`(单活动 start>end)仍拒。

---

## 4. C4–C6(LOW,可选)

- **C4**(`stream.py:347/355`):`except Exception` 不接 `CancelledError/GeneratorExit`,取消/断连时 close 被跳过、走静默 clear。缓解:GeneratorExit 下本无法 yield、断连客户端也收不到 ack,实际影响窄。若要修:在外层 `try/finally` 之间加 `except (asyncio.CancelledError, GeneratorExit)` 或把 close 的"标记队列已终结"逻辑移进 finally(注意 finally 里不能 yield,只能记状态/log)。
- **C5**(`stream.py:297` vs `356`):cancelled 子路径先 done 后 ack,顺序调整为 ack 先于 done,或该路径不发终结 ack(取消场景用户已放弃)。
- **C6**(`steering.py:23`):`stream.py:92` 的 `asyncio.Queue(maxsize=64)` 改用 `MAX_STEER_QUEUE_SIZE` 常量,消除漂移。一行改动。

---

## 5. 实施顺序与完成标准

1. **C1 先做**(唯一 P1)。抽 `_handle_redispatch_failure` 统一处理三个重派段 + 8b repair 的失败回滚/兜底。**先补"重派失败"注入测试(修复前失败),再改**。
2. **C2、C3** 次之,都在 `renegotiation.py`,局部。
3. **C4–C6** 视排期,LOW。

**完成标准**:
- C1 有 4 个触发性回归测试(steering/late-steering/再协商/8b 各一,重派失败 → 断言天不丢),修复前失败、修复后通过。
- C2、C3 各有触发性回归测试。
- 全量 `python -m pytest backend/tests/ -q` = 6 failed(预存)+ 其余通过,无新增失败。
- 前端 `npm run build`、Ruff、`git diff --check` 通过。
- **人工核对(最容易改成"看起来修了但没生效")**:C1 的"重派失败 → 该天最终在交付里",必须读代码确认失败分支真的回滚/兜底了,而不是只看测试绿——现有测试用理想数据绕过了失败路径。

---

## 6. 实施结果（2026-07-15）

- **C1（方案 A + 8b 内联恢复）**：`candidate_store` 新增 `get_latest_candidate` /
  `restore_candidate_as_latest`（重新标 accepted 并 bump 到最新 seq，保持 B1 的
  "先选最后写入再查 accepted" 语义不变）。orchestrator 新增 `_RedispatchRollback` +
  `_invalidate_day_for_redispatch`（作废前记旧 accepted attempt 与旧成功结果）+
  `_handle_redispatch_failure`（磁盘恢复 + successes 放回 + 黑板重登记），三个重派段
  （steering→step7 attempt=2、late-steering attempt=5、再协商 attempt=4）共用一张
  回滚表，重派成功即弹出、失败或未实际重派由收口 sweep 兜底。8b 修复段在
  `_run_repair_worker` 释放黑板前记录旧 accepted attempt，`_apply_repair_result`
  失败分支恢复磁盘口径并把保留的 in-memory 旧 dayplan 重新登记黑板。
- **C2**：`_activity_poi_keys` 判据从 "location.name 非空" 改为 "location.name 能产出
  canonical key"，产不出时回退 activity.name；round2 的防误合并测试保持通过。
- **C3**：边界校验改取真实最早 start / 最晚 end（允许乱序）；end 早于 start 且落在
  凌晨（≤06:00，`_CROSS_MIDNIGHT_END_MAX_MIN`）按跨午夜折算次日；单活动 18:00→17:00
  仍拒。
- **C4**：`stream.py` 显式 `except (CancelledError, GeneratorExit): raise`；
  `clear_run_steering` teardown 时 drain 残留引导并记 warning，不再静默吞。
- **C5**：cancelled 子路径终结 ack 先于 `done` 事件发出。
- **C6**：`stream.py` 队列容量改用 `MAX_STEER_QUEUE_SIZE` 常量。
- 测试：C1 补 4 个触发性回归（steering / late-steering / 再协商 / 8b 各一），已用
  "临时禁用回滚" 做红灯验证（4 个全部失败），恢复修复后全绿；C2、C3 各补触发性
  单测（泛化 location 回退去重、乱序不误拒、跨午夜不误拒）。round2 的
  `test_failed_redispatch_does_not_revive_old_accepted_artifact` 断言的正是 C1 要修的
  丢天行为，已改写为 `test_failed_renegotiation_redispatch_restores_previous_version`
  （被动复活的 loader 语义仍由 candidate_store 单测覆盖）。
- 附带：为满足 `stream.py` <400 行的结构约束，把中断恢复上下文块抽到
  `stream_runtime.apply_continuation_context`。
- 验证：全量后端 `6 failed, 2048 passed`（6 个预存）；定向 73 passed；前端
  `npm run build` 通过；`git diff --check` 通过；Ruff 无新增（8 个 F401 为预存，
  HEAD 上为 9 个）。
- 后续（2026-07-16）：6 个预存失败定位为测试硬编码行程日期（2026-07-01/07-10）过期，
  被 `past_date` guardrail 拦截 `save_day_plan`/`check_weather` 所致，与业务代码无关。
  已把相关测试日期改为 `date.today()+timedelta` 动态生成，全量 `2054 passed, 0 failed`。
