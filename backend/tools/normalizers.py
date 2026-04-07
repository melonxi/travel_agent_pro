# backend/tools/normalizers.py
from __future__ import annotations

from dataclasses import dataclass, asdict
from difflib import SequenceMatcher
from typing import Any


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FlightResult:
    airline: str
    flight_no: str
    origin: str
    destination: str
    dep_time: str
    arr_time: str
    duration_min: int
    stops: int
    price: float | None
    currency: str
    cabin_class: str
    source: str  # "amadeus" | "flyai"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AccommodationResult:
    name: str
    address: str
    lat: float | None
    lng: float | None
    rating: float | None
    price_per_night: float | None
    currency: str
    star_rating: str | None
    bed_type: str | None
    source: str  # "google" | "flyai" | "merged"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class POIResult:
    name: str
    address: str
    lat: float | None
    lng: float | None
    rating: float | None
    category: str | None
    is_free: bool | None
    ticket_price: float | None
    ticket_url: str | None
    source: str  # "google" | "flyai" | "merged"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TrainResult:
    train_no: str
    origin: str
    origin_station: str
    destination: str
    destination_station: str
    dep_time: str
    arr_time: str
    duration_min: int
    stops: int
    price: float | None
    currency: str
    seat_class: str
    source: str  # "flyai"
    booking_url: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Normalize: Amadeus / Google → unified dataclass
# ---------------------------------------------------------------------------


def normalize_amadeus_flight(raw: dict) -> FlightResult:
    price_obj = raw.get("price", {})
    itineraries = raw.get("itineraries", [{}])
    first_itin = itineraries[0] if itineraries else {}
    segments = first_itin.get("segments", [{}])
    first_seg = segments[0] if segments else {}

    carrier = first_seg.get("carrierCode", "")
    number = first_seg.get("number", "")
    dep = first_seg.get("departure", {})
    arr = first_seg.get("arrival", {})

    # Parse duration like "PT3H30M" → minutes
    duration_str = first_itin.get("duration", "")
    duration_min = _parse_iso_duration(duration_str)

    price_str = price_obj.get("total")
    price = float(price_str) if price_str else None

    return FlightResult(
        airline=carrier,
        flight_no=f"{carrier}{number}",
        origin=dep.get("iataCode", ""),
        destination=arr.get("iataCode", ""),
        dep_time=dep.get("at", ""),
        arr_time=arr.get("at", ""),
        duration_min=duration_min,
        stops=max(0, len(segments) - 1),
        price=price,
        currency=price_obj.get("currency", "USD"),
        cabin_class="economy",
        source="amadeus",
        booking_url=None,
    )


def normalize_google_accommodation(raw: dict) -> AccommodationResult:
    loc = raw.get("geometry", {}).get("location", {})
    return AccommodationResult(
        name=raw.get("name", ""),
        address=raw.get("formatted_address", ""),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        rating=raw.get("rating"),
        price_per_night=None,  # Google Places doesn't provide actual prices
        currency="USD",
        star_rating=str(raw["price_level"]) if raw.get("price_level") else None,
        bed_type=None,
        source="google",
        booking_url=None,
    )


def normalize_google_poi(raw: dict) -> POIResult:
    loc = raw.get("geometry", {}).get("location", {})
    return POIResult(
        name=raw.get("name", ""),
        address=raw.get("formatted_address", ""),
        lat=loc.get("lat"),
        lng=loc.get("lng"),
        rating=raw.get("rating"),
        category=", ".join(raw.get("types", [])) or None,
        is_free=None,
        ticket_price=None,
        ticket_url=None,
        source="google",
        booking_url=None,
    )


# ---------------------------------------------------------------------------
# Normalize: FlyAI → unified dataclass
# ---------------------------------------------------------------------------


def _safe_float(val: object) -> float | None:
    """Parse a price value that may contain currency symbols (¥, $, €, etc.).

    Handles cases like '¥589', '$120.50', '1,200', '1200', 120, 120.5, None, ''.
    Returns None if the value cannot be parsed.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    # Strip common currency symbols, whitespace, and thousand-separators
    import re

    cleaned = re.sub(r"[¥$€£￥,\s]", "", s)
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def normalize_flyai_flight(raw: dict) -> FlightResult:
    journeys = raw.get("journeys", [{}])
    first_journey = journeys[0] if journeys else {}
    segments = first_journey.get("segments", [{}])
    first_seg = segments[0] if segments else {}

    # New CLI uses ticketPrice at top level; fall back to adultPrice/price
    price = _safe_float(raw.get("adultPrice") or raw.get("ticketPrice") or raw.get("price"))

    # New: marketingTransportName / marketingTransportNo; old: airlineName / flightNo
    airline = first_seg.get("marketingTransportName") or first_seg.get("airlineName", "")
    flight_no = first_seg.get("marketingTransportNo") or first_seg.get("flightNo", "")

    # New: depDateTime / arrDateTime; old: depTime / arrTime
    dep_time = first_seg.get("depDateTime") or first_seg.get("depTime", "")
    arr_time = first_seg.get("arrDateTime") or first_seg.get("arrTime", "")

    # New: duration is string like "140分钟"; old: int minutes
    duration_min = _parse_cn_duration(first_seg.get("duration", 0))

    # New: journeyType "直达" means 0 stops; old: explicit stopCount
    stops = int(first_seg.get("stopCount", 0))
    if not stops and first_journey.get("journeyType") == "直达":
        stops = 0
    elif not stops and len(segments) > 1:
        stops = len(segments) - 1

    # New: seatClassName; old: cabin
    cabin_class = first_seg.get("seatClassName") or first_seg.get("cabin", "")

    return FlightResult(
        airline=airline,
        flight_no=flight_no,
        origin=first_seg.get("depCityName", ""),
        destination=first_seg.get("arrCityName", ""),
        dep_time=dep_time,
        arr_time=arr_time,
        duration_min=duration_min,
        stops=stops,
        price=price,
        currency="CNY",
        cabin_class=cabin_class,
        source="flyai",
        booking_url=raw.get("jumpUrl"),
    )


def normalize_flyai_hotel(raw: dict) -> AccommodationResult:
    price = _safe_float(raw.get("price"))

    score_val = raw.get("score")
    rating = float(score_val) if score_val is not None else None

    # New CLI uses "name"; old used "title"
    name = raw.get("name") or raw.get("title", "")

    # New CLI provides latitude/longitude
    lat = float(raw["latitude"]) if raw.get("latitude") else None
    lng = float(raw["longitude"]) if raw.get("longitude") else None

    return AccommodationResult(
        name=name,
        address=raw.get("address", ""),
        lat=lat,
        lng=lng,
        rating=rating,
        price_per_night=price,
        currency="CNY",
        star_rating=raw.get("star") or raw.get("starRating"),
        bed_type=raw.get("bedType"),
        source="flyai",
        booking_url=raw.get("detailUrl"),
    )


def normalize_flyai_poi(raw: dict) -> POIResult:
    ticket_info = raw.get("ticketInfo", {}) or {}
    ticket_price = _safe_float(ticket_info.get("price"))

    # New CLI uses "name"; old used "title" (with fallback)
    name = raw.get("name") or raw.get("title", "")

    return POIResult(
        name=name,
        address=raw.get("address", ""),
        lat=None,
        lng=None,
        rating=float(raw["score"]) if raw.get("score") else None,
        category=raw.get("category"),
        is_free=raw.get("freePoiStatus", None),
        ticket_price=ticket_price,
        ticket_url=raw.get("jumpUrl"),
        source="flyai",
        booking_url=raw.get("jumpUrl"),
    )


def normalize_flyai_train(raw: dict) -> TrainResult:
    journeys = raw.get("journeys", [{}])
    first_journey = journeys[0] if journeys else {}
    segments = first_journey.get("segments", [{}])
    first_seg = segments[0] if segments else {}

    price = _safe_float(raw.get("adultPrice") or raw.get("price") or raw.get("ticketPrice"))

    duration_min = _parse_cn_duration(
        first_seg.get("duration") or first_journey.get("totalDuration", 0)
    )

    stops = 0
    if first_journey.get("journeyType") != "直达":
        stops = max(0, len(segments) - 1)

    return TrainResult(
        train_no=first_seg.get("marketingTransportNo", ""),
        origin=first_seg.get("depCityName", ""),
        origin_station=first_seg.get("depStationName", ""),
        destination=first_seg.get("arrCityName", ""),
        destination_station=first_seg.get("arrStationName", ""),
        dep_time=first_seg.get("depDateTime", ""),
        arr_time=first_seg.get("arrDateTime", ""),
        duration_min=duration_min,
        stops=stops,
        price=price,
        currency="CNY",
        seat_class=first_seg.get("seatClassName", ""),
        source="flyai",
        booking_url=raw.get("jumpUrl"),
    )


# ---------------------------------------------------------------------------
# Merge strategies
# ---------------------------------------------------------------------------


def merge_flights(
    amadeus: list[FlightResult], flyai: list[FlightResult]
) -> list[FlightResult]:
    """Deduplicate by (flight_no, dep_date). Prefer record with booking_url."""
    seen: dict[tuple[str, str], FlightResult] = {}

    for f in amadeus:
        dep_date = f.dep_time[:10]
        key = (f.flight_no.upper(), dep_date)
        seen[key] = f

    for f in flyai:
        dep_date = f.dep_time[:10]
        key = (f.flight_no.upper(), dep_date)
        if key in seen:
            # Prefer the one with booking_url
            if f.booking_url and not seen[key].booking_url:
                seen[key] = f
        else:
            seen[key] = f

    return list(seen.values())


def merge_accommodations(
    google: list[AccommodationResult], flyai: list[AccommodationResult]
) -> list[AccommodationResult]:
    """Fuzzy match by name (ratio > 0.8). Merge Google coords + FlyAI price/booking."""
    result: list[AccommodationResult] = []
    flyai_matched: set[int] = set()

    for g in google:
        best_idx, best_ratio = -1, 0.0
        for i, f in enumerate(flyai):
            if i in flyai_matched:
                continue
            ratio = _fuzzy_name_ratio(g.name, f.name)
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_ratio > 0.8 and best_idx >= 0:
            flyai_matched.add(best_idx)
            f = flyai[best_idx]
            result.append(
                AccommodationResult(
                    name=g.name,
                    address=g.address or f.address,
                    lat=g.lat,
                    lng=g.lng,
                    rating=g.rating or f.rating,
                    price_per_night=f.price_per_night or g.price_per_night,
                    currency=f.currency if f.price_per_night else g.currency,
                    star_rating=f.star_rating or g.star_rating,
                    bed_type=f.bed_type,
                    source="merged",
                    booking_url=f.booking_url,
                )
            )
        else:
            result.append(g)

    for i, f in enumerate(flyai):
        if i not in flyai_matched:
            result.append(f)

    return result


def merge_pois(google: list[POIResult], flyai: list[POIResult]) -> list[POIResult]:
    """Same fuzzy-match strategy as accommodations."""
    result: list[POIResult] = []
    flyai_matched: set[int] = set()

    for g in google:
        best_idx, best_ratio = -1, 0.0
        for i, f in enumerate(flyai):
            if i in flyai_matched:
                continue
            ratio = _fuzzy_name_ratio(g.name, f.name)
            if ratio > best_ratio:
                best_ratio = ratio
                best_idx = i

        if best_ratio > 0.8 and best_idx >= 0:
            flyai_matched.add(best_idx)
            f = flyai[best_idx]
            result.append(
                POIResult(
                    name=g.name,
                    address=g.address or f.address,
                    lat=g.lat,
                    lng=g.lng,
                    rating=g.rating or f.rating,
                    category=f.category or g.category,
                    is_free=f.is_free if f.is_free is not None else g.is_free,
                    ticket_price=f.ticket_price,
                    ticket_url=f.ticket_url,
                    source="merged",
                    booking_url=f.booking_url or g.booking_url,
                )
            )
        else:
            result.append(g)

    for i, f in enumerate(flyai):
        if i not in flyai_matched:
            result.append(f)

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fuzzy_name_ratio(a: str, b: str) -> float:
    """Fuzzy name match that handles mixed CJK/Latin names.

    Uses multiple strategies and returns the best score:
    1. Full lowercased SequenceMatcher comparison
    2. Latin-only portion comparison (strips CJK characters)
    3. Containment check — if the shorter Latin name is contained
       in the longer one, treat as high-confidence match
    """
    import re

    a_low, b_low = a.lower().strip(), b.lower().strip()
    full_ratio = SequenceMatcher(None, a_low, b_low).ratio()

    # Extract Latin-only tokens (letters, hyphens, spaces)
    a_latin = re.sub(r"[^a-z\s\-]", "", a_low).strip()
    b_latin = re.sub(r"[^a-z\s\-]", "", b_low).strip()

    best = full_ratio

    if a_latin and b_latin:
        latin_ratio = SequenceMatcher(None, a_latin, b_latin).ratio()
        best = max(best, latin_ratio)

        # Containment: if shorter is fully within longer, high confidence
        shorter, longer = sorted([a_latin, b_latin], key=len)
        if shorter and shorter in longer:
            best = max(best, 0.85)

    return best


def _parse_iso_duration(s: str) -> int:
    """Parse ISO 8601 duration like 'PT3H30M' to minutes."""
    if not s or not s.startswith("PT"):
        return 0
    s = s[2:]  # strip "PT"
    hours = 0
    minutes = 0
    if "H" in s:
        parts = s.split("H")
        hours = int(parts[0])
        s = parts[1]
    if "M" in s:
        minutes = int(s.replace("M", ""))
    return hours * 60 + minutes


def _parse_cn_duration(val: int | str) -> int:
    """Parse duration from FlyAI — either int minutes or string like '140分钟'."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        import re
        m = re.search(r"(\d+)", val)
        return int(m.group(1)) if m else 0
    return 0
