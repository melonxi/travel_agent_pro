from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RedFlag:
    id: str
    trigger: str
    repair: str

    def render(self) -> str:
        return f"- [{self.id}] 触发：{self.trigger}\n  修复：{self.repair}"


CORE_RED_FLAGS: tuple[RedFlag, ...] = (
    RedFlag(
        id="G-EVIDENCE",
        trigger="你把外部事实说成已确认、已验证或可用，但当前上下文没有工具结果或权威状态支撑。",
        repair="先用当前阶段允许的工具验证；无法验证时标注不确定，不写成确定结论。",
    ),
    RedFlag(
        id="G-STATE-AUTHORITY",
        trigger="你把用户未明确表达、工具未返回、或当前阶段未产出的内容写入权威状态。",
        repair="只写入用户明确输入、工具结果，或当前阶段允许生成的结构化产物。",
    ),
    RedFlag(
        id="G-CAPABILITY-BOUNDARY",
        trigger="你承诺当前可用工具列表之外的能力，或在用户推翻上游决策时继续局部修改。",
        repair="按当前工具列表回答；涉及上游决策推翻时走阶段回退。",
    ),
)


PHASE_RED_FLAGS: dict[int, tuple[RedFlag, ...]] = {
    1: (
        RedFlag(
            id="P1-1",
            trigger="目的地未确认就规划住宿、交通或逐日行程。",
            repair="回到目的地收敛，只比较 2-3 个候选或推动用户拍板。",
        ),
        RedFlag(
            id="P1-2",
            trigger="用户没主动给日期、人数或预算，却把对话变成问卷。",
            repair="只问一个直接服务于目的地筛选的问题；无必要时先给目的地候选。",
        ),
        RedFlag(
            id="P1-3",
            trigger="用户已明确目的地，却先研究攻略，没有先写入目的地状态。",
            repair="先写入目的地；用户明确给出的预算、人数、日期也同步写入。",
        ),
        RedFlag(
            id="P1-4",
            trigger="推荐目的地只靠常识或搜索标题，没有读取正文、评论或可靠来源。",
            repair="先读取实质内容或可靠来源，再给出候选比较。",
        ),
        RedFlag(
            id="P1-5",
            trigger="把推荐候选写成用户最终拍板的目的地。",
            repair="候选只用于自然语言比较；只有用户确认后才写入最终目的地。",
        ),
    ),
    3: (
        RedFlag(
            id="P3-1",
            trigger="生成逐日行程但没有写入行程状态。",
            repair="立即用逐日写入工具保存已完成天数；全量重排才整体替换。",
        ),
        RedFlag(
            id="P3-2",
            trigger="忽略已有逐日行程，覆盖用户已确认的天数。",
            repair="只生成缺失天数；修改已有天数时按单天替换。",
        ),
        RedFlag(
            id="P3-3",
            trigger="偏离已选骨架的区域、主题或核心取舍。",
            repair="回到已选骨架展开；重大新增或替换先征求用户或回退。",
        ),
        RedFlag(
            id="P3-4",
            trigger="声称路线、开放、天气可行，但没有工具验证。",
            repair="对会影响行程结构的移动、开放和天气风险先调用对应工具。",
        ),
        RedFlag(
            id="P3-5",
            trigger="DayPlan schema 不合规，导致前端或写入工具无法消费。",
            repair="修正 location、时间、category、cost 等字段后再提交。",
        ),
    ),
    4: (
        RedFlag(
            id="P4-1",
            trigger="在本阶段修改行程、住宿或交通，而不是回退。",
            repair="说明发现的问题；需要改上游决策时走阶段回退。",
        ),
        RedFlag(
            id="P4-2",
            trigger="天气、签证、服务建议没有工具结果支撑。",
            repair="先调用本阶段允许的天气、服务或 web 验证工具。",
        ),
        RedFlag(
            id="P4-3",
            trigger="checklist 是通用模板，没有基于实际行程个性化。",
            repair="逐项扫描已确认行程活动，生成对应准备事项和风险提醒。",
        ),
        RedFlag(
            id="P4-4",
            trigger="没有调用交付物生成工具，却声称正式交付完成。",
            repair="先提交正式行程和清单两份 markdown，再告知完成。",
        ),
        RedFlag(
            id="P4-5",
            trigger="只提交一份 markdown，或交付物已冻结仍尝试覆盖。",
            repair="同时提交两份交付物；已冻结时提示先回退再重生成。",
        ),
    ),
}


PHASE2_BASE_RED_FLAGS: tuple[RedFlag, ...] = (
    RedFlag(
        id="P2-BASE-1",
        trigger="你跳过当前子阶段，直接产出下游阶段内容。",
        repair="只完成当前子阶段产物；下游产物等系统切换子阶段后再生成。",
    ),
    RedFlag(
        id="P2-BASE-2",
        trigger="你把分析产物写进用户偏好或约束，而不是写进当前子阶段结构化产物。",
        repair="用户明确表达才写偏好或约束；你的分析写入当前子阶段产物。",
    ),
    RedFlag(
        id="P2-BASE-3",
        trigger="你在正文完整描述结构化产物，但没有调用对应状态写入工具。",
        repair="先调用当前子阶段对应的写入工具，再用简短文字同步用户。",
    ),
    RedFlag(
        id="P2-BASE-4",
        trigger="你试图手动维护 phase2_step，而不是通过产物状态让系统自动推进。",
        repair="写入关键产物即可；不要尝试直接改子阶段。",
    ),
)


PHASE2_STEP_RED_FLAGS: dict[str, tuple[RedFlag, ...]] = {
    "brief": (
        RedFlag(
            id="P2-BRIEF-1",
            trigger="信息已足够形成画像，还继续追问细枝末节。",
            repair="先形成可迭代画像并写入，后续再按反馈修正。",
        ),
        RedFlag(
            id="P2-BRIEF-2",
            trigger="用户只给模糊时间，你补成具体年月日。",
            repair="只记录天数或模糊窗口，并请用户确认具体日期。",
        ),
        RedFlag(
            id="P2-BRIEF-3",
            trigger="使用非标准画像 key，导致后续阶段无法稳定消费。",
            repair="使用 goal、pace、departure_city 等标准字段；其他内容写入专用字段。",
        ),
        RedFlag(
            id="P2-BRIEF-4",
            trigger="brief 未成型前调用交通、住宿、路线工具。",
            repair="先收束画像和硬约束；交通住宿留到后续子阶段。",
        ),
    ),
    "candidate": (
        RedFlag(
            id="P2-CAND-1",
            trigger="只在正文列候选池或短名单，没有写入结构化状态。",
            repair="先写入候选全集，再写入筛选后的短名单。",
        ),
        RedFlag(
            id="P2-CAND-2",
            trigger="shortlist 高潜力项没有正文阅读或 web 验证支撑。",
            repair="先读取实质内容或验证关键事实，再写入短名单。",
        ),
        RedFlag(
            id="P2-CAND-3",
            trigger="保留了与旅行画像或用户避免项冲突的候选。",
            repair="删除冲突候选，或标记为不建议并说明原因。",
        ),
        RedFlag(
            id="P2-CAND-4",
            trigger="同一轮写入 shortlist 后继续写 skeleton_plans。",
            repair="写入 shortlist 后停止；等系统切换到 skeleton 子阶段。",
        ),
    ),
    "skeleton": (
        RedFlag(
            id="P2-SKEL-1",
            trigger="没有搜索并读取真实 N 天攻略，就直接生成骨架。",
            repair="先读取 2-3 篇相关攻略，提炼区域分组、天数分配和节奏策略。",
        ),
        RedFlag(
            id="P2-SKEL-2",
            trigger="多套骨架只是顺序变化，没有实质取舍差异。",
            repair="让方案在节奏、覆盖范围或体验重心上形成清晰差异。",
        ),
        RedFlag(
            id="P2-SKEL-3",
            trigger="骨架缺少后续 Phase 3 依赖的关键字段。",
            repair="补齐稳定 id、name、days、区域、主题、locked/candidate POI 和取舍说明。",
        ),
        RedFlag(
            id="P2-SKEL-4",
            trigger="同一 POI 跨天重复出现在 locked_pois 或 candidate_pois。",
            repair="每个 POI 只归属最合适的一天；必要时移入 excluded 或 fallback。",
        ),
        RedFlag(
            id="P2-SKEL-5",
            trigger="用户选择方案后没有用骨架 id 锁定选择。",
            repair="用骨架的稳定 id 锁定选择，不能用展示名或自然语言代替。",
        ),
    ),
    "lock": (
        RedFlag(
            id="P2-LOCK-1",
            trigger="用户未确认就锁定交通或住宿。",
            repair="先给候选选项；只有用户明确选择后才锁定。",
        ),
        RedFlag(
            id="P2-LOCK-2",
            trigger="住宿区域没有对照已选骨架动线验证。",
            repair="用主要活动区域和通勤成本验证住宿区域后再推荐。",
        ),
        RedFlag(
            id="P2-LOCK-3",
            trigger="需要大交通搜索却拖到 Phase 3。",
            repair="在 lock 子阶段完成必要的大交通候选搜索。",
        ),
        RedFlag(
            id="P2-LOCK-4",
            trigger="给出交通或住宿选项但没有写入候选状态。",
            repair="先写入候选列表，再用简短文字让用户选择。",
        ),
        RedFlag(
            id="P2-LOCK-5",
            trigger="发现骨架不可行却硬锁住宿，没有写风险、备选或回退。",
            repair="记录风险和备选；严重不可行时回退到骨架调整。",
        ),
    ),
}


PHASE3_WORKER_RED_FLAGS: tuple[RedFlag, ...] = (
    RedFlag(
        id="W3-1",
        trigger="向用户提问、请求确认或给 2-3 个选项。",
        repair="独立做保守判断并继续完成当前单日任务。",
    ),
    RedFlag(
        id="W3-2",
        trigger="完成计划后没有通过 submit_day_plan_candidate 提交。",
        repair="把 DayPlan 通过唯一提交工具交给 Orchestrator。",
    ),
    RedFlag(
        id="W3-3",
        trigger="使用 forbidden_pois，造成跨天重复。",
        repair="删除 forbidden POI，改用当前日候选池或合理替代。",
    ),
    RedFlag(
        id="W3-4",
        trigger="为补字段编造坐标、票价或开放时间。",
        repair="无法确认的事实写入 notes；坐标和价格缺失时按工具结果或安全默认处理。",
    ),
    RedFlag(
        id="W3-5",
        trigger="围绕同一问题反复搜索，迟迟不提交保守可用方案。",
        repair="停止重复搜索，提交覆盖硬约束的保守 DayPlan。",
    ),
)


def build_active_red_flags(
    *,
    phase: int,
    phase2_step: str | None = None,
    worker: bool = False,
) -> tuple[RedFlag, ...]:
    if worker:
        return PHASE3_WORKER_RED_FLAGS

    flags: list[RedFlag] = list(CORE_RED_FLAGS)
    if phase == 2:
        step = phase2_step or "brief"
        flags.extend(PHASE2_BASE_RED_FLAGS)
        flags.extend(PHASE2_STEP_RED_FLAGS.get(step, PHASE2_STEP_RED_FLAGS["brief"]))
    else:
        flags.extend(PHASE_RED_FLAGS.get(phase, ()))
    return tuple(flags)


def render_red_flags(
    *,
    phase: int,
    phase2_step: str | None = None,
    worker: bool = False,
) -> str:
    flags = build_active_red_flags(
        phase=phase,
        phase2_step=phase2_step,
        worker=worker,
    )
    if not flags:
        return ""

    lines = [
        "## 当前阶段 Red Flags（高危失败信号）",
        "",
        "以下内容不是任务目标，也不是执行步骤。",
        "它们用于提醒你识别“正在走偏”的行为。",
        "如果命中某条，停止当前错误方向，并按“修复”动作收口。",
        "方括号内编号仅用于系统测试和追踪，不是额外指令。",
    ]
    lines.extend(flag.render() for flag in flags)
    return "\n".join(lines)
