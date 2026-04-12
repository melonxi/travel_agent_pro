# backend/state/models.py
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Location:
    lat: float
    lng: float
    name: str = ""

    def to_dict(self) -> dict:
        return {"lat": self.lat, "lng": self.lng, "name": self.name}

    @classmethod
    def from_dict(cls, d: Any) -> Location:
        # Tolerate LLM-provided payloads: accept str / None / partial dict.
        if d is None:
            return cls(lat=0.0, lng=0.0, name="")
        if isinstance(d, str):
            return cls(lat=0.0, lng=0.0, name=d)
        if not isinstance(d, dict):
            return cls(lat=0.0, lng=0.0, name=str(d))
        try:
            lat = float(d.get("lat", 0) or 0)
        except (TypeError, ValueError):
            lat = 0.0
        try:
            lng = float(d.get("lng", 0) or 0)
        except (TypeError, ValueError):
            lng = 0.0
        name = d.get("name") or d.get("address") or ""
        return cls(lat=lat, lng=lng, name=str(name))


@dataclass
class DateRange:
    start: str  # YYYY-MM-DD
    end: str

    @property
    def total_days(self) -> int:
        from datetime import date as dt_date

        s = dt_date.fromisoformat(self.start)
        e = dt_date.fromisoformat(self.end)
        return (e - s).days

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end}

    @classmethod
    def from_dict(cls, d: dict) -> DateRange:
        return cls(start=d["start"], end=d["end"])


@dataclass
class Travelers:
    adults: int = 1
    children: int = 0

    def to_dict(self) -> dict:
        return {"adults": self.adults, "children": self.children}

    @classmethod
    def from_dict(cls, d: dict) -> Travelers:
        return cls(adults=d.get("adults", 1), children=d.get("children", 0))


@dataclass
class Budget:
    total: float
    currency: str = "CNY"

    def to_dict(self) -> dict:
        return {"total": self.total, "currency": self.currency}

    @classmethod
    def from_dict(cls, d: dict) -> Budget:
        return cls(total=d["total"], currency=d.get("currency", "CNY"))


@dataclass
class Accommodation:
    area: str
    hotel: str | None = None

    def to_dict(self) -> dict:
        return {"area": self.area, "hotel": self.hotel}

    @classmethod
    def from_dict(cls, d: dict) -> Accommodation:
        return cls(area=d["area"], hotel=d.get("hotel"))


@dataclass
class Activity:
    name: str
    location: Location
    start_time: str  # "HH:MM"
    end_time: str
    category: str
    cost: float = 0
    transport_from_prev: str | None = None
    transport_duration_min: int = 0
    notes: str = ""

    @property
    def duration_minutes(self) -> int:
        sh, sm = map(int, self.start_time.split(":"))
        eh, em = map(int, self.end_time.split(":"))
        return (eh * 60 + em) - (sh * 60 + sm)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "location": self.location.to_dict(),
            "start_time": self.start_time,
            "end_time": self.end_time,
            "category": self.category,
            "cost": self.cost,
            "transport_from_prev": self.transport_from_prev,
            "transport_duration_min": self.transport_duration_min,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Activity:
        # Tolerate LLM-provided payloads where optional fields are missing or
        # where location is passed as a string name instead of a full dict.
        if not isinstance(d, dict):
            raise TypeError(f"Activity.from_dict expects dict, got {type(d).__name__}")
        return cls(
            name=str(d.get("name", "")),
            location=Location.from_dict(d.get("location")),
            start_time=str(d.get("start_time", "")),
            end_time=str(d.get("end_time", "")),
            category=str(d.get("category") or "activity"),
            cost=d.get("cost", 0) or 0,
            transport_from_prev=d.get("transport_from_prev"),
            transport_duration_min=d.get("transport_duration_min", 0) or 0,
            notes=str(d.get("notes", "") or ""),
        )


@dataclass
class DayPlan:
    day: int
    date: str
    activities: list[Activity] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "day": self.day,
            "date": self.date,
            "activities": [a.to_dict() for a in self.activities],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DayPlan:
        if not isinstance(d, dict):
            raise TypeError(f"DayPlan.from_dict expects dict, got {type(d).__name__}")
        try:
            day_value = int(d.get("day", 0))
        except (TypeError, ValueError):
            day_value = 0
        return cls(
            day=day_value,
            date=str(d.get("date", "")),
            activities=[Activity.from_dict(a) for a in d.get("activities", []) or []],
            notes=str(d.get("notes", "") or ""),
        )


@dataclass(init=False)
class Constraint:
    type: str  # "hard" | "soft"
    description: str
    source: str = ""

    def __init__(
        self,
        type: str,
        description: str,
        *,
        source: str = "",
    ) -> None:
        self.type = type
        self.description = description
        self.source = source

    def to_dict(self) -> dict:
        data = {"type": self.type, "description": self.description}
        if self.source:
            data["source"] = self.source
        return data

    @classmethod
    def from_dict(cls, d: dict) -> Constraint:
        return cls(type=d["type"], description=d["description"], source=d.get("source", ""))


@dataclass(init=False)
class Preference:
    key: str
    value: str
    source: str = ""

    def __init__(
        self,
        key: str | None = None,
        value: str = "",
        *,
        category: str | None = None,
        source: str = "",
    ) -> None:
        self.key = str(key if key is not None else category if category is not None else "")
        self.value = value
        self.source = source

    @property
    def category(self) -> str:
        return self.key

    def to_dict(self) -> dict:
        data = {"key": self.key, "value": self.value}
        if self.source:
            data["source"] = self.source
        return data

    @classmethod
    def from_dict(cls, d: dict) -> Preference:
        return cls(
            key=d.get("key") or d.get("category"),
            value=d.get("value", ""),
            source=d.get("source", ""),
        )


@dataclass
class BacktrackEvent:
    from_phase: int
    to_phase: int
    reason: str
    snapshot_path: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "from_phase": self.from_phase,
            "to_phase": self.to_phase,
            "reason": self.reason,
            "snapshot_path": self.snapshot_path,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict) -> BacktrackEvent:
        return cls(
            from_phase=d["from_phase"],
            to_phase=d["to_phase"],
            reason=d["reason"],
            snapshot_path=d["snapshot_path"],
            timestamp=d.get("timestamp", ""),
        )


# Phase -> which fields are downstream products (cleared on backtrack)
_PHASE_DOWNSTREAM: dict[int, list[str]] = {
    1: [
        "destination",
        "destination_candidates",
        "dates",
        "phase3_step",
        "trip_brief",
        "candidate_pool",
        "shortlist",
        "skeleton_plans",
        "selected_skeleton_id",
        "transport_options",
        "selected_transport",
        "accommodation_options",
        "accommodation",
        "risks",
        "alternatives",
        "daily_plans",
    ],
    3: [
        "dates",
        "phase3_step",
        "trip_brief",
        "candidate_pool",
        "shortlist",
        "skeleton_plans",
        "selected_skeleton_id",
        "transport_options",
        "selected_transport",
        "accommodation_options",
        "accommodation",
        "risks",
        "alternatives",
        "daily_plans",
    ],
    5: ["daily_plans"],
}

_FIELD_DEFAULTS: dict[str, Any] = {
    "destination": None,
    "destination_candidates": [],
    "dates": None,
    "phase3_step": "brief",
    "trip_brief": {},
    "candidate_pool": [],
    "shortlist": [],
    "skeleton_plans": [],
    "selected_skeleton_id": None,
    "transport_options": [],
    "selected_transport": None,
    "accommodation_options": [],
    "accommodation": None,
    "risks": [],
    "alternatives": [],
    "daily_plans": [],
}


def infer_phase3_step_from_state(
    *,
    phase: int,
    dates: DateRange | None,
    trip_brief: dict[str, Any] | None,
    candidate_pool: list[dict[str, Any]] | None,
    shortlist: list[dict[str, Any]] | None,
    skeleton_plans: list[dict[str, Any]] | None,
    selected_skeleton_id: str | None,
    accommodation: Accommodation | None,
) -> str:
    if phase < 3:
        return "brief"
    if phase > 3:
        return "lock"
    if not dates or not trip_brief:
        return "brief"
    if not selected_skeleton_id:
        if skeleton_plans:
            return "skeleton"
        if shortlist or candidate_pool:
            return "candidate"
        return "candidate"
    # Validate selected_skeleton_id resolves to an actual skeleton
    if skeleton_plans:
        matched = any(
            s.get("id") == selected_skeleton_id or s.get("name") == selected_skeleton_id
            for s in skeleton_plans
        )
        if not matched:
            # Dangling reference — stay in skeleton stage
            return "skeleton"
    if not accommodation:
        return "lock"
    return "lock"


@dataclass
class TravelPlanState:
    session_id: str
    trip_id: str | None = None
    phase: int = 1
    destination: str | None = None
    destination_candidates: list[dict] = field(default_factory=list)
    dates: DateRange | None = None
    phase3_step: str = "brief"
    trip_brief: dict[str, Any] = field(default_factory=dict)
    candidate_pool: list[dict[str, Any]] = field(default_factory=list)
    shortlist: list[dict[str, Any]] = field(default_factory=list)
    skeleton_plans: list[dict[str, Any]] = field(default_factory=list)
    selected_skeleton_id: str | None = None
    transport_options: list[dict[str, Any]] = field(default_factory=list)
    selected_transport: dict[str, Any] | None = None
    accommodation_options: list[dict[str, Any]] = field(default_factory=list)
    travelers: Travelers | None = None
    budget: Budget | None = None
    accommodation: Accommodation | None = None
    risks: list[dict[str, Any]] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    daily_plans: list[DayPlan] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    preferences: list[Preference] = field(default_factory=list)
    backtrack_history: list[BacktrackEvent] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())
    version: int = 1

    def clear_downstream(self, from_phase: int) -> None:
        """Clear all output produced after from_phase. Keep constraints and preferences."""
        for phase in sorted(_PHASE_DOWNSTREAM):
            if phase >= from_phase:
                for attr in _PHASE_DOWNSTREAM[phase]:
                    setattr(self, attr, deepcopy(_FIELD_DEFAULTS[attr]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "trip_id": self.trip_id,
            "phase": self.phase,
            "destination": self.destination,
            "destination_candidates": self.destination_candidates,
            "dates": self.dates.to_dict() if self.dates else None,
            "phase3_step": self.phase3_step,
            "trip_brief": self.trip_brief,
            "candidate_pool": self.candidate_pool,
            "shortlist": self.shortlist,
            "skeleton_plans": self.skeleton_plans,
            "selected_skeleton_id": self.selected_skeleton_id,
            "transport_options": self.transport_options,
            "selected_transport": self.selected_transport,
            "accommodation_options": self.accommodation_options,
            "travelers": self.travelers.to_dict() if self.travelers else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "accommodation": self.accommodation.to_dict()
            if self.accommodation
            else None,
            "risks": self.risks,
            "alternatives": self.alternatives,
            "daily_plans": [dp.to_dict() for dp in self.daily_plans],
            "constraints": [c.to_dict() for c in self.constraints],
            "preferences": [p.to_dict() for p in self.preferences],
            "backtrack_history": [b.to_dict() for b in self.backtrack_history],
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TravelPlanState:
        raw_phase = d.get("phase", 1)
        phase = 1 if raw_phase == 2 else (3 if raw_phase == 4 else raw_phase)
        dates = DateRange.from_dict(d["dates"]) if d.get("dates") else None
        accommodation = (
            Accommodation.from_dict(d["accommodation"])
            if d.get("accommodation")
            else None
        )
        trip_brief = d.get("trip_brief", {})
        candidate_pool = d.get("candidate_pool", [])
        shortlist = d.get("shortlist", [])
        skeleton_plans = d.get("skeleton_plans", [])
        selected_skeleton_id = d.get("selected_skeleton_id")
        return cls(
            session_id=d["session_id"],
            trip_id=d.get("trip_id"),
            phase=phase,
            destination=d.get("destination"),
            destination_candidates=d.get("destination_candidates", []),
            dates=dates,
            phase3_step=d.get(
                "phase3_step",
                infer_phase3_step_from_state(
                    phase=phase,
                    dates=dates,
                    trip_brief=trip_brief,
                    candidate_pool=candidate_pool,
                    shortlist=shortlist,
                    skeleton_plans=skeleton_plans,
                    selected_skeleton_id=selected_skeleton_id,
                    accommodation=accommodation,
                ),
            ),
            trip_brief=trip_brief,
            candidate_pool=candidate_pool,
            shortlist=shortlist,
            skeleton_plans=skeleton_plans,
            selected_skeleton_id=selected_skeleton_id,
            transport_options=d.get("transport_options", []),
            selected_transport=d.get("selected_transport"),
            accommodation_options=d.get("accommodation_options", []),
            travelers=Travelers.from_dict(d["travelers"])
            if d.get("travelers")
            else None,
            budget=Budget.from_dict(d["budget"]) if d.get("budget") else None,
            accommodation=accommodation,
            risks=d.get("risks", []),
            alternatives=d.get("alternatives", []),
            daily_plans=[DayPlan.from_dict(dp) for dp in d.get("daily_plans", [])],
            constraints=[Constraint.from_dict(c) for c in d.get("constraints", [])],
            preferences=[Preference.from_dict(p) for p in d.get("preferences", [])],
            backtrack_history=[
                BacktrackEvent.from_dict(b) for b in d.get("backtrack_history", [])
            ],
            created_at=d.get("created_at", ""),
            last_updated=d.get("last_updated", ""),
            version=d.get("version", 1),
        )
