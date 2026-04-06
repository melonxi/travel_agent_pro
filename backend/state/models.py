# backend/state/models.py
from __future__ import annotations

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
    def from_dict(cls, d: dict) -> Location:
        return cls(lat=d["lat"], lng=d["lng"], name=d.get("name", ""))


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
        return cls(
            name=d["name"],
            location=Location.from_dict(d["location"]),
            start_time=d["start_time"],
            end_time=d["end_time"],
            category=d["category"],
            cost=d.get("cost", 0),
            transport_from_prev=d.get("transport_from_prev"),
            transport_duration_min=d.get("transport_duration_min", 0),
            notes=d.get("notes", ""),
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
        return cls(
            day=d["day"],
            date=d["date"],
            activities=[Activity.from_dict(a) for a in d.get("activities", [])],
            notes=d.get("notes", ""),
        )


@dataclass
class Constraint:
    type: str  # "hard" | "soft"
    description: str

    def to_dict(self) -> dict:
        return {"type": self.type, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> Constraint:
        return cls(type=d["type"], description=d["description"])


@dataclass
class Preference:
    key: str
    value: str

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value}

    @classmethod
    def from_dict(cls, d: dict) -> Preference:
        return cls(key=d["key"], value=d["value"])


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
        "accommodation",
        "daily_plans",
    ],
    3: ["dates", "accommodation", "daily_plans"],
    5: ["daily_plans"],
}


@dataclass
class TravelPlanState:
    session_id: str
    phase: int = 1
    destination: str | None = None
    destination_candidates: list[dict] = field(default_factory=list)
    dates: DateRange | None = None
    travelers: Travelers | None = None
    budget: Budget | None = None
    accommodation: Accommodation | None = None
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
                    default = [] if isinstance(getattr(self, attr), list) else None
                    setattr(self, attr, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "phase": self.phase,
            "destination": self.destination,
            "destination_candidates": self.destination_candidates,
            "dates": self.dates.to_dict() if self.dates else None,
            "travelers": self.travelers.to_dict() if self.travelers else None,
            "budget": self.budget.to_dict() if self.budget else None,
            "accommodation": self.accommodation.to_dict()
            if self.accommodation
            else None,
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
        return cls(
            session_id=d["session_id"],
            phase=phase,
            destination=d.get("destination"),
            destination_candidates=d.get("destination_candidates", []),
            dates=DateRange.from_dict(d["dates"]) if d.get("dates") else None,
            travelers=Travelers.from_dict(d["travelers"])
            if d.get("travelers")
            else None,
            budget=Budget.from_dict(d["budget"]) if d.get("budget") else None,
            accommodation=Accommodation.from_dict(d["accommodation"])
            if d.get("accommodation")
            else None,
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
