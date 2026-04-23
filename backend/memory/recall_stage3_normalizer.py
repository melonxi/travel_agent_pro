from __future__ import annotations

from config import Stage3RecallConfig
from memory.destination_normalization import normalize_destination
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_models import RecallQueryEnvelope, SourcePolicy
from state.models import TravelPlanState

_DOMAIN_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "hotel": ("hotel", "accommodation"),
    "accommodation": ("accommodation", "hotel"),
    "flight": ("flight",),
    "train": ("train",),
    "food": ("food",),
    "pace": ("pace", "planning_style"),
}

_KEYWORD_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "住宿": ("住宿", "酒店", "民宿", "住哪里", "旅馆"),
    "住哪里": ("住哪里",),
    "酒店": ("酒店", "住宿", "住哪里", "民宿"),
    "民宿": ("民宿", "住宿", "酒店", "住哪里"),
    "机票": ("机票", "航班", "飞机"),
    "航班": ("航班", "机票", "飞机"),
    "高铁": ("高铁", "火车", "列车"),
    "餐厅": ("餐厅", "吃饭", "美食", "吃"),
    "吃": ("吃", "餐厅", "美食", "吃饭"),
    "节奏": ("节奏", "慢", "轻松", "累"),
}


def build_query_envelope(
    query: RecallRetrievalPlan,
    user_message: str,
    plan: TravelPlanState,
    config: Stage3RecallConfig,
) -> RecallQueryEnvelope:
    destination = query.destination or getattr(plan, "destination", "") or ""
    source_policy = _build_source_policy(query, config)
    expanded_domains = _expand_domains(query.domains)
    expanded_keywords = _expand_keywords(query.keywords)

    destination_canonical = ""
    destination_aliases: tuple[str, ...] = ()
    destination_children: tuple[str, ...] = ()
    destination_region = ""
    if config.destination_normalization_enabled and destination:
        normalized = normalize_destination(destination)
        destination_canonical = normalized.canonical
        destination_aliases = normalized.aliases
        destination_children = normalized.children
        destination_region = normalized.region

    return RecallQueryEnvelope(
        plan=query,
        user_message=user_message,
        source_policy=source_policy,
        original_domains=tuple(query.domains),
        expanded_domains=expanded_domains,
        original_keywords=tuple(query.keywords),
        expanded_keywords=expanded_keywords,
        destination=destination,
        destination_canonical=destination_canonical,
        destination_aliases=destination_aliases,
        destination_children=destination_children,
        destination_region=destination_region,
    )


def _build_source_policy(
    query: RecallRetrievalPlan,
    config: Stage3RecallConfig,
) -> SourcePolicy:
    source = query.source
    search_profile = source in {"profile", "hybrid_history"}
    search_slices = source in {"episode_slice", "hybrid_history"}
    widened = False if not config.source_widening.enabled else False
    return SourcePolicy(
        requested_source=source,
        search_profile=search_profile,
        search_slices=search_slices,
        widened=widened,
        widening_reason="",
    )


def _expand_domains(domains: list[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    seen: set[str] = set()
    for domain in domains:
        for value in _DOMAIN_EXPANSIONS.get(domain, (domain,)):
            if value and value not in seen:
                seen.add(value)
                expanded.append(value)
    return tuple(expanded)


def _expand_keywords(values: list[str]) -> tuple[str, ...]:
    expanded: list[str] = []
    seen: set[str] = set()
    normalized_values = [value.strip() for value in values if value and value.strip()]
    joined = "\n".join(normalized_values)

    for value in normalized_values:
        if value not in seen:
            seen.add(value)
            expanded.append(value)

        for trigger, synonyms in _KEYWORD_EXPANSIONS.items():
            if trigger in value or trigger in joined:
                for synonym in synonyms:
                    if synonym not in seen:
                        seen.add(synonym)
                        expanded.append(synonym)

    return tuple(expanded)
