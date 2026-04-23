from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedDestination:
    original: str
    canonical: str
    aliases: tuple[str, ...]
    region: str
    children: tuple[str, ...]


@dataclass(frozen=True)
class DestinationMatch:
    query: NormalizedDestination
    candidate: NormalizedDestination
    match_type: str
    score: float


_DESTINATION_CATALOG: dict[str, dict[str, tuple[str, ...] | str]] = {
    "东京": {"aliases": ("東京", "Tokyo", "tokyo"), "region": "关东", "children": ()},
    "千叶": {"aliases": ("Chiba",), "region": "关东", "children": ()},
    "埼玉": {"aliases": ("Saitama",), "region": "关东", "children": ()},
    "神奈川": {"aliases": ("Kanagawa",), "region": "关东", "children": ()},
    "京都": {"aliases": ("Kyoto",), "region": "关西", "children": ()},
    "大阪": {"aliases": ("Osaka",), "region": "关西", "children": ()},
    "奈良": {"aliases": ("Nara",), "region": "关西", "children": ()},
    "神户": {"aliases": ("Kobe",), "region": "关西", "children": ()},
    "关东": {
        "aliases": ("関東", "Kanto"),
        "region": "关东",
        "children": ("东京", "千叶", "埼玉", "神奈川"),
    },
    "关西": {
        "aliases": ("関西", "Kansai"),
        "region": "关西",
        "children": ("京都", "大阪", "奈良", "神户"),
    },
    "北海道": {"aliases": ("Hokkaido",), "region": "北海道", "children": ("札幌",)},
    "札幌": {"aliases": ("Sapporo",), "region": "北海道", "children": ()},
    "冲绳": {"aliases": ("沖縄", "Okinawa"), "region": "冲绳", "children": ()},
    "福冈": {"aliases": ("福岡", "Fukuoka"), "region": "九州", "children": ()},
    "巴黎": {"aliases": ("Paris",), "region": "法兰西岛", "children": ()},
    "伦敦": {"aliases": ("London",), "region": "英格兰", "children": ()},
    "首尔": {"aliases": ("Seoul",), "region": "韩国", "children": ()},
    "台北": {"aliases": ("Taipei",), "region": "台湾", "children": ()},
    "香港": {"aliases": ("Hong Kong",), "region": "香港", "children": ()},
}

_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _entry in _DESTINATION_CATALOG.items():
    _ALIAS_TO_CANONICAL[_canonical] = _canonical
    for _alias in _entry["aliases"]:
        _ALIAS_TO_CANONICAL[_alias] = _canonical


def normalize_destination(value: str) -> NormalizedDestination:
    original = " ".join(str(value or "").split())
    canonical = _ALIAS_TO_CANONICAL.get(original, original)
    entry = _DESTINATION_CATALOG.get(
        canonical,
        {"aliases": (), "region": "", "children": ()},
    )

    return NormalizedDestination(
        original=original,
        canonical=canonical,
        aliases=entry["aliases"],
        region=str(entry["region"]),
        children=entry["children"],
    )


def match_destination(query_value: str, candidate_value: str) -> DestinationMatch:
    query = normalize_destination(query_value)
    candidate = normalize_destination(candidate_value)

    if not query.canonical or not candidate.canonical:
        return DestinationMatch(query=query, candidate=candidate, match_type="none", score=0.0)

    if query.canonical == candidate.canonical:
        if query.original == candidate.original:
            return DestinationMatch(
                query=query,
                candidate=candidate,
                match_type="exact",
                score=1.0,
            )
        return DestinationMatch(
            query=query,
            candidate=candidate,
            match_type="alias",
            score=0.95,
        )

    if candidate.canonical in query.children or query.canonical in candidate.children:
        return DestinationMatch(
            query=query,
            candidate=candidate,
            match_type="parent_child",
            score=0.75,
        )

    if query.region and query.region == candidate.region:
        return DestinationMatch(
            query=query,
            candidate=candidate,
            match_type="region_weak",
            score=0.35,
        )

    return DestinationMatch(query=query, candidate=candidate, match_type="none", score=0.0)
