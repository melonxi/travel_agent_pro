# 面试口述稿（双故事）

计时目标：每个故事约 3 分钟。先 20 秒定位，再只深讲这两个故事。

## 开场 20 秒

这是一个以旅行规划为验证场景的长任务 Agent Runtime。重点不是查景点，而是：阶段化受控自治、并行 Worker 的版本治理、运行中 steering，以及用 Trace、golden case 和故障注入证明可靠性。没有真实生产用户，是可运行的作品集原型。

---

## 故事 1：并行 Worker 为什么会「静默丢天」

长任务 Phase 3 会并行给每天开 Worker。如果 Worker 直接写最终行程，某天失败或被约束否决时，很容易出现「日志里跑过了，最终计划却少一天」——也就是 silent day loss。

我们的做法是：Worker **不能**直接写最终 `TravelPlanState`。它只产出 **Candidate**，状态至少包括 accepted / rejected / superseded。共享黑板负责跨天 POI、预算和冲突约束。只有 **accepted** 版本才会提交进最终计划。如果局部重派失败，必须 **回滚到旧版本**，不能留下空洞。

工程上用 Candidate Store + Orchestrator 重派路径保证这一点；回归上有 steering/orchestrator 相关测试，例如重派失败恢复旧版本的用例。面试官若问「怎么证明」，我指向测试名和 `docs/evidence/fault-injection-report.md` 的 F2，而不是口述保证。

**可能下一刀：** 冲突规则是否 deterministic？→ 黑板/唯一性规则 + 对应 store 测试。成本？→ 只重派受影响天，不做整轮重跑。

---

## 故事 2：如何安全处理运行中用户指令

用户看并行精排时发现「第 3 天不对」，如果只能 cancel 再重来，长任务体验很差。我们加了 **mid-run steering**：`POST /api/chat/{session_id}/steer`。

消息先进入队列，不立刻硬插进 LLM 上下文。Agent Loop / Phase 3 orchestrator 只在 **安全边界** drain——尤其不能插在 assistant 的 tool_call 与 tool_result 之间，否则会破坏 tool 协议。对已完成或可定位的天，可以触发 **定向重派**；如果本轮处理不了（例如 run 已结束或处于不可再试阶段），必须给 **terminal ack** 或明确 HTTP 语义（例如无 active run 时 409），禁止静默丢消息。

证据：`tests/test_steering.py` 覆盖队列、409/429、终端 ack、Phase3 定向重派与重派失败回滚。

**可能下一刀：** 和 continue/cancel 的区别？→ cancel 停，continue 续，steer 是不中断的纠偏通道。

---

## 结束时一句话

旅行只是外壳；我真正想展示的是长任务 Agent 的状态边界、失败恢复和可评测性。
