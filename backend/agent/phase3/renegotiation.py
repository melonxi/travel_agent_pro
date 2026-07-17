"""D3: Phase 3 纵向再协商 + 共享黑板数据结构。

Orchestrator 单写：再协商只改骨架副本；黑板在收集候选时查表即拒。
Worker 只读快照，不直接写权威状态。
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from math import ceil
from typing import Any

from agent.phase3.worker_prompt import max_core_activities_for_pace

logger = logging.getLogger(__name__)

REPLAN_KINDS = frozenset({"INFEASIBLE_DAY", "OVERLOADED", "SUGGEST_MOVE"})
MAX_RENEGOTIATE_PER_DAY = 1
# 活动 end < start 且 end 落在此点之前（凌晨）视为跨午夜，而非时序矛盾。
_CROSS_MIDNIGHT_END_MAX_MIN = 6 * 60

_GENERIC_ACTIVITY_NAMES = frozenset(
    {
        "早餐",
        "午餐",
        "晚餐",
        "用餐",
        "自由活动",
        "自由时间",
        "休息",
        "交通",
        "乘车",
        "返回酒店",
        "酒店入住",
        "办理入住",
        "退房",
    }
)
_POI_ACTION_PREFIX_RE = re.compile(
    r"^(?:参观|游览|前往|探访|到访|打卡|漫步|逛逛|逛|抵达|体验)"
)
_POI_ACTION_SUFFIX_RE = re.compile(r"(?:参观|游览|打卡|体验)$")
_POI_PUNCT_RE = re.compile(r"[\s\-—_·•,，。.!！?？:：;；'\"“”‘’（）()【】\[\]]+")


def canonical_poi_key(value: Any) -> str | None:
    """Build a stable-enough POI key from current string-only skeleton data."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in _GENERIC_ACTIVITY_NAMES:
        return None
    normalized = _POI_PUNCT_RE.sub("", text).lower()
    normalized = _POI_ACTION_PREFIX_RE.sub("", normalized)
    normalized = _POI_ACTION_SUFFIX_RE.sub("", normalized)
    if not normalized or normalized in _GENERIC_ACTIVITY_NAMES:
        return None
    return normalized


def _activity_poi_keys(activity: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (canonical key, display label), preferring provider IDs/location."""
    keys: list[tuple[str, str]] = []
    for id_key in ("poi_id", "place_id"):
        raw_id = activity.get(id_key)
        if isinstance(raw_id, str) and raw_id.strip():
            keys.append((f"id:{raw_id.strip()}", raw_id.strip()))
    location = activity.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None
    # activity.name is free-form copy (for example "博物馆参观"). Once a stable
    # location label exists, registering that copy as another identity key can
    # merge unrelated POIs which happen to share the same activity wording.
    # "Stable" means the label yields a canonical key: a non-empty but generic
    # location.name (自由活动/市区) must fall back to activity.name, or the real
    # POI carried there escapes cross-day dedup entirely.
    labels = (
        [location_name]
        if canonical_poi_key(location_name)
        else [activity.get("name")]
    )
    for label in labels:
        key = canonical_poi_key(label)
        if key:
            display = str(label).strip()
            aliases = [key]
            if display not in aliases and display not in _GENERIC_ACTIVITY_NAMES:
                aliases.append(display)
            for alias in aliases:
                if all(existing_key != alias for existing_key, _ in keys):
                    keys.append((alias, display))
    return keys


@dataclass(frozen=True)
class ReplanRequest:
    """Worker 上报的结构化再协商请求。"""

    day: int
    kind: str
    reason: str
    move_poi: str | None = None
    to_day: int | None = None

    @classmethod
    def from_tool_arguments(
        cls, *, day: int, arguments: dict[str, Any] | None
    ) -> "ReplanRequest":
        args = arguments or {}
        kind = str(args.get("kind") or "INFEASIBLE_DAY").strip().upper()
        if kind not in REPLAN_KINDS:
            kind = "INFEASIBLE_DAY"
        reason = str(args.get("reason") or "").strip() or "worker 判定骨架不可行"
        # 兼容 P1-6 旧字段 suggestion：若未给 kind 但写了 suggestion，仍作 INFEASIBLE
        move_poi = args.get("move_poi")
        to_day = args.get("to_day")
        move_poi_s = (
            str(move_poi).strip()
            if isinstance(move_poi, str) and move_poi.strip()
            else None
        )
        to_day_i: int | None = None
        if isinstance(to_day, int) and to_day > 0:
            to_day_i = to_day
        elif isinstance(to_day, str) and to_day.strip().isdigit():
            parsed = int(to_day.strip())
            if parsed > 0:
                to_day_i = parsed
        if kind == "SUGGEST_MOVE" and (not move_poi_s or to_day_i is None):
            # 缺字段时降级，避免 orchestrator 误改骨架
            kind = "INFEASIBLE_DAY"
            if not reason.endswith("(SUGGEST_MOVE 字段不全)"):
                reason = f"{reason} (SUGGEST_MOVE 字段不全)"
        return cls(
            day=day,
            kind=kind,
            reason=reason,
            move_poi=move_poi_s,
            to_day=to_day_i,
        )

    def to_error_detail(self) -> str:
        parts = [f"[{self.kind}]", self.reason]
        if self.kind == "SUGGEST_MOVE" and self.move_poi and self.to_day:
            parts.append(f"建议将 {self.move_poi} 移到第 {self.to_day} 天")
        return " ".join(parts)


@dataclass(frozen=True)
class SkeletonAmendment:
    kind: str
    description: str
    source_day: int
    target_day: int | None = None
    poi: str | None = None


@dataclass
class RenegotiationOutcome:
    """再协商决策结果：骨架副本改动 + 需重派天 + 无法自动解的请求。"""

    skeleton_copy: dict[str, Any]
    affected_days: set[int] = field(default_factory=set)
    amendments: list[SkeletonAmendment] = field(default_factory=list)
    unresolved: list[ReplanRequest] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


@dataclass
class Phase3Blackboard:
    """Orchestrator 单写、Worker 只读的共享黑板。

    - poi_registry: canonical POI key → 认领天（编译期 locked + 运行时已接受候选）
    - budget_ledger: 天 → 已提交活动成本
    - day_boundaries: 天 → (首活动开始, 末活动结束)
    """

    poi_registry: dict[str, int] = field(default_factory=dict)
    poi_labels: dict[str, str] = field(default_factory=dict)
    budget_ledger: dict[int, float] = field(default_factory=dict)
    day_boundaries: dict[int, tuple[str | None, str | None]] = field(
        default_factory=dict
    )
    total_budget: float | None = None
    precommitted_cost: float = 0.0  # 交通 + 住宿等已锁定成本

    def seed_from_locked(self, locked_pois_by_day: dict[int, list[str]]) -> None:
        for day, pois in locked_pois_by_day.items():
            for poi in pois:
                key = canonical_poi_key(poi)
                if key:
                    aliases = {key, poi.strip()}
                    for alias in aliases:
                        if alias and alias not in self.poi_registry:
                            self.poi_registry[alias] = day
                            self.poi_labels[alias] = poi

    def snapshot_forbidden_for_day(self, day: int) -> list[str]:
        return [
            self.poi_labels.get(key, key)
            for key, owner in self.poi_registry.items()
            if owner != day
        ]

    def try_accept_dayplan(
        self, day: int, dayplan: dict[str, Any]
    ) -> tuple[bool, str | None]:
        """收集候选时查表即拒。成功则登记，失败返回 (False, reason)。"""
        activities = dayplan.get("activities") or []
        if not isinstance(activities, list):
            activities = []

        poi_keys: list[tuple[str, str]] = []
        day_cost = 0.0
        first_start: str | None = None
        last_end: str | None = None
        first_start_min: int | None = None
        last_end_min: int | None = None
        for act in activities:
            if not isinstance(act, dict):
                continue
            for key_and_label in _activity_poi_keys(act):
                if key_and_label not in poi_keys:
                    poi_keys.append(key_and_label)
            try:
                day_cost += float(act.get("cost") or 0)
            except (TypeError, ValueError):
                pass
            st = act.get("start_time")
            et = act.get("end_time")
            st_min = _time_to_min(st) if isinstance(st, str) and st else None
            et_min = _time_to_min(et) if isinstance(et, str) and et else None
            if st_min is not None and (
                first_start_min is None or st_min < first_start_min
            ):
                first_start_min = st_min
                first_start = st
            if et_min is not None:
                adjusted = et_min
                if (
                    st_min is not None
                    and et_min < st_min
                    and et_min <= _CROSS_MIDNIGHT_END_MAX_MIN
                ):
                    # 结束落在凌晨且早于开始 → 跨午夜活动，按次日计
                    adjusted = et_min + 24 * 60
                if last_end_min is None or adjusted > last_end_min:
                    last_end_min = adjusted
                    last_end = et

        # POI 认领：已被其他天登记的 POI 拒绝
        for key, label in poi_keys:
            owner = self.poi_registry.get(key)
            if owner is not None and owner != day:
                return (
                    False,
                    f"POI '{label}' 已被第 {owner} 天认领，请换其他景点",
                )

        # 预算台账：跨天累计 + 已锁定交通住宿
        if self.total_budget is not None:
            other = sum(c for d, c in self.budget_ledger.items() if d != day)
            projected = other + day_cost + self.precommitted_cost
            if projected > self.total_budget:
                over = projected - self.total_budget
                return (
                    False,
                    f"跨天预算超支 ¥{over:.0f}（累计含本日 ¥{projected:.0f} "
                    f"/ 预算 ¥{self.total_budget:.0f}），请减少本日活动费用",
                )

        # 边界锚点：取真实最早开始/最晚结束（activities 可能乱序），仅当能确定
        # 同日时序矛盾才拒；跨午夜结束已在上面按次日折算，不误拒夜间行程。
        if (
            first_start_min is not None
            and last_end_min is not None
            and first_start_min > last_end_min
        ):
            return (
                False,
                f"本日边界非法：首活动 {first_start} 晚于末活动 {last_end}",
            )
        prev = self.day_boundaries.get(day - 1)
        if prev and prev[1] and first_start:
            if _time_cmp(first_start, prev[1]) < 0:
                # 跨天日期不同时时间不可直接比；仅当同日角色冲突时提示。
                # 多日行程中 day N 与 N-1 的 clock 可重叠（不同日历日），跳过严格拒绝。
                pass

        # 接受：登记
        for key, label in poi_keys:
            self.poi_registry[key] = day
            self.poi_labels[key] = label
        self.budget_ledger[day] = day_cost
        self.day_boundaries[day] = (first_start, last_end)
        return True, None

    def release_day(self, day: int) -> None:
        """重派某天前释放其认领与台账。"""
        to_drop = [poi for poi, owner in self.poi_registry.items() if owner == day]
        for poi in to_drop:
            del self.poi_registry[poi]
            self.poi_labels.pop(poi, None)
        self.budget_ledger.pop(day, None)
        self.day_boundaries.pop(day, None)


def _time_to_min(t: str) -> int | None:
    """解析 HH:MM 为分钟；不可解析时返回 None。"""
    try:
        parts = t.strip()[:5].split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        return None


def _time_cmp(a: str, b: str) -> int:
    """粗略比较 HH:MM；不可解析时返回 0。"""
    ma, mb = _time_to_min(a), _time_to_min(b)
    if ma is None or mb is None:
        return 0
    return (ma > mb) - (ma < mb)


def _day_entry(skeleton: dict[str, Any], day: int) -> dict[str, Any] | None:
    days = skeleton.get("days")
    if not isinstance(days, list) or day < 1 or day > len(days):
        return None
    entry = days[day - 1]
    return entry if isinstance(entry, dict) else None


def _list_field(day_entry: dict[str, Any], key: str) -> list[str]:
    raw = day_entry.get(key)
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if isinstance(x, str) and x.strip()]


def _set_list_field(day_entry: dict[str, Any], key: str, values: list[str]) -> None:
    day_entry[key] = list(values)


def _capacity_left(day_entry: dict[str, Any], pace: str) -> int:
    max_core = max_core_activities_for_pace(pace)
    locked = len(_list_field(day_entry, "locked_pois"))
    # candidate_pois is an alternative pool. Workers select only as many as the
    # remaining activity slots permit, so the full pool is not committed load.
    return max(0, max_core - locked)


def renegotiate_skeleton(
    *,
    skeleton: dict[str, Any],
    requests: list[ReplanRequest],
    pace: str,
    renegotiate_count: dict[int, int],
    total_days: int,
    max_affected_fraction: float = 0.5,
) -> RenegotiationOutcome:
    """确定性再协商：改骨架副本，返回受影响天与未解请求。

    - SUGGEST_MOVE: 校验目标天容量后迁移 POI
    - OVERLOADED: 裁剪超限 candidate
    - INFEASIBLE_DAY: 不自动解，记入 unresolved
    熔断：每天最多再协商 MAX_RENEGOTIATE_PER_DAY 次。
    """
    skeleton_copy = copy.deepcopy(skeleton)
    if not isinstance(skeleton_copy.get("days"), list):
        skeleton_copy["days"] = []

    outcome = RenegotiationOutcome(skeleton_copy=skeleton_copy)
    # MOVE 是原子操作，source/target 必须成对重派；两日行程至少允许 2 天。
    max_affected = max(1, ceil(total_days * max_affected_fraction))
    if any(req.kind == "SUGGEST_MOVE" for req in requests) and total_days >= 2:
        max_affected = max(max_affected, 2)

    for req in requests:
        if renegotiate_count.get(req.day, 0) >= MAX_RENEGOTIATE_PER_DAY:
            outcome.unresolved.append(req)
            outcome.messages.append(
                f"第 {req.day} 天再协商已达上限，降级为人工/部分交付："
                f"{req.to_error_detail()}"
            )
            continue

        if req.kind == "INFEASIBLE_DAY":
            outcome.unresolved.append(req)
            outcome.messages.append(
                f"第 {req.day} 天结构性不可行，无法自动改骨架：{req.reason}"
            )
            continue

        if req.kind == "OVERLOADED":
            proposed_days = outcome.affected_days | {req.day}
            if len(proposed_days) > max_affected:
                outcome.unresolved.append(req)
                outcome.messages.append(
                    f"第 {req.day} 天 OVERLOADED 会使重派范围超过上限，降级部分交付"
                )
                continue
            day_entry = _day_entry(skeleton_copy, req.day)
            if day_entry is None:
                outcome.unresolved.append(req)
                continue
            max_core = max_core_activities_for_pace(pace)
            locked = _list_field(day_entry, "locked_pois")
            candidates = _list_field(day_entry, "candidate_pois")
            # 保留 locked 全量（若 locked 已超限，编译期会再 demote），裁剪 candidate
            slots = max(0, max_core - len(locked))
            if len(candidates) > slots:
                kept = candidates[:slots]
                removed = candidates[slots:]
                _set_list_field(day_entry, "candidate_pois", kept)
                outcome.amendments.append(
                    SkeletonAmendment(
                        kind="OVERLOADED",
                        description=(f"第 {req.day} 天减载：移除超限候选 {removed}"),
                        source_day=req.day,
                        poi=removed[0] if removed else None,
                    )
                )
                outcome.affected_days.add(req.day)
                renegotiate_count[req.day] = renegotiate_count.get(req.day, 0) + 1
                outcome.messages.append(
                    f"第 {req.day} 天 OVERLOADED：已裁剪候选，将只重派该天"
                )
            else:
                # 无 candidate 可裁 → 视为不可自动解
                outcome.unresolved.append(req)
                outcome.messages.append(
                    f"第 {req.day} 天 OVERLOADED 但无可裁候选：{req.reason}"
                )
            continue

        if req.kind == "SUGGEST_MOVE":
            if not req.move_poi or req.to_day is None:
                outcome.unresolved.append(req)
                outcome.messages.append(
                    f"第 {req.day} 天 SUGGEST_MOVE 字段不完整，拒绝自动改骨架"
                )
                continue
            if req.to_day == req.day or req.to_day < 1 or req.to_day > total_days:
                outcome.unresolved.append(req)
                outcome.messages.append(
                    f"第 {req.day} 天 SUGGEST_MOVE 目标天非法 to_day={req.to_day}"
                )
                continue
            proposed_days = outcome.affected_days | {req.day, req.to_day}
            if len(proposed_days) > max_affected:
                outcome.unresolved.append(req)
                outcome.messages.append(
                    f"MOVE {req.day}→{req.to_day} 会使重派范围超过上限，"
                    "为避免拆散成对天，整条 MOVE 降级部分交付"
                )
                continue
            if renegotiate_count.get(req.to_day, 0) >= MAX_RENEGOTIATE_PER_DAY:
                outcome.unresolved.append(req)
                outcome.messages.append(
                    f"目标第 {req.to_day} 天再协商已达上限，拒绝 MOVE"
                )
                continue

            src = _day_entry(skeleton_copy, req.day)
            dst = _day_entry(skeleton_copy, req.to_day)
            if src is None or dst is None:
                outcome.unresolved.append(req)
                continue

            src_locked = _list_field(src, "locked_pois")
            src_cand = _list_field(src, "candidate_pois")
            poi = req.move_poi
            if poi not in src_locked and poi not in src_cand:
                outcome.unresolved.append(req)
                outcome.messages.append(f"第 {req.day} 天找不到 POI '{poi}'，无法 MOVE")
                continue

            if _capacity_left(dst, pace) <= 0:
                outcome.unresolved.append(req)
                outcome.messages.append(f"第 {req.to_day} 天已满载，拒绝接收 '{poi}'")
                continue

            was_locked = poi in src_locked
            if was_locked:
                src_locked = [p for p in src_locked if p != poi]
                _set_list_field(src, "locked_pois", src_locked)
            else:
                src_cand = [p for p in src_cand if p != poi]
                _set_list_field(src, "candidate_pois", src_cand)

            dst_locked = _list_field(dst, "locked_pois")
            dst_cand = _list_field(dst, "candidate_pois")
            # 从 locked 移出的保持 locked；从 candidate 移出的进 candidate
            if was_locked:
                if poi not in dst_locked:
                    dst_locked.append(poi)
                    _set_list_field(dst, "locked_pois", dst_locked)
                dst_cand = [p for p in dst_cand if p != poi]
                _set_list_field(dst, "candidate_pois", dst_cand)
            else:
                if poi not in dst_cand and poi not in dst_locked:
                    dst_cand.append(poi)
                    _set_list_field(dst, "candidate_pois", dst_cand)

            outcome.amendments.append(
                SkeletonAmendment(
                    kind="SUGGEST_MOVE",
                    description=(f"将 '{poi}' 从第 {req.day} 天移到第 {req.to_day} 天"),
                    source_day=req.day,
                    target_day=req.to_day,
                    poi=poi,
                )
            )
            outcome.affected_days.add(req.day)
            outcome.affected_days.add(req.to_day)
            renegotiate_count[req.day] = renegotiate_count.get(req.day, 0) + 1
            # 目标天也计一次，防止 A↔B 互移
            renegotiate_count[req.to_day] = renegotiate_count.get(req.to_day, 0) + 1
            outcome.messages.append(
                f"已接受 MOVE：'{poi}' 第 {req.day}→{req.to_day} 天，将只重派受影响天"
            )
            continue

        outcome.unresolved.append(req)

    return outcome
