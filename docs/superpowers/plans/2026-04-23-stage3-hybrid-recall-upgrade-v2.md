# Stage 3 Hybrid Recall Upgrade v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Stage 3 into a configurable hybrid candidate generator that improves recall coverage while preserving current default behavior.

**Architecture:** Stage 3 becomes an internal orchestration layer with query normalization, retrieval lanes, candidate fusion, and telemetry. The first implementation keeps only the symbolic lane enabled by default, then adds lexical and semantic lanes behind feature flags so Stage 4 receives better candidates without taking over Stage 4 reranking responsibilities.

**Tech Stack:** Python 3.12, existing dataclasses/config style, pytest, FastEmbed 0.8.x, `BAAI/bge-small-zh-v1.5`, ONNX Runtime CPU via FastEmbed.

---

## Embedding Decision

Use `BAAI/bge-small-zh-v1.5` through FastEmbed as the default local embedding runtime.

Runtime readiness status:

- Completed before this Stage 3 implementation plan in commit `987b104 chore: verify stage3 embedding runtime`.
- `backend/pyproject.toml` already declares `fastembed>=0.8.0,<0.9.0`.
- `scripts/verify-stage3-embedding-runtime.py` already verifies the real runtime.
- Local cache has been validated at `backend/data/embedding_cache`; the cache is git-ignored runtime data.
- The implementation tasks below must not re-add the dependency or re-download the model as part of normal unit tests.

Decision basis checked on 2026-04-23:

- Hugging Face model card for `BAAI/bge-small-zh-v1.5` identifies it as a Chinese BGE v1.5 embedding model with 512-dimensional output and MIT/commercial-friendly release terms: <https://huggingface.co/BAAI/bge-small-zh-v1.5>
- FastEmbed lists `BAAI/bge-small-zh-v1.5` as a supported text embedding model with 512 dimensions, MIT license, and about `0.090` GB model size: <https://qdrant.github.io/fastembed/examples/Supported_Models/>
- PyPI lists FastEmbed `0.8.0`, Python `>=3.10.0`, and describes the library as using ONNX Runtime without GPU or large PyTorch dependencies: <https://pypi.org/project/fastembed/>

Why this choice:

- The product domain is mostly Chinese natural-language travel memory; a Chinese retrieval embedding model is a better default than an English or generic multilingual small model.
- The backend already requires Python `>=3.12`, and FastEmbed supports Python 3.12.
- FastEmbed keeps the implementation lighter than `torch + transformers + sentence-transformers`.
- `bge-m3` is stronger but too large for this local Stage 3 first pass; it belongs in a future eval once we know semantic recall is the bottleneck.
- `shibing624/text2vec-base-chinese` remains a reasonable alternative, but it is less retrieval-focused for this task and likely increases runtime packaging complexity.

Runtime policy during implementation:

- If the local environment changes, verify the runtime with `./scripts/verify-stage3-embedding-runtime.py --local-files-only` before debugging Stage 3 code.
- Production should pre-warm that cache in the image or deployment artifact and set `local_files_only=true`.
- Unit tests must not download the model. They use a fake embedding provider.
- The semantic lane must degrade to disabled with telemetry if FastEmbed import, model load, or embedding generation fails.

## Scope

This plan implements Phase A through Phase D from the Stage 3 v2 spec:

- Phase A: config, DTOs, symbolic lane extraction, default behavior equivalence, telemetry shell.
- Phase B: query normalization and destination evidence behind a feature flag.
- Phase C: lexical lane behind a feature flag.
- Phase D: FastEmbed provider and semantic lane behind a feature flag.

This plan does not implement:

- Stage 4 reranker changes.
- Persistent vector store or write-time embedding persistence.
- LLM-driven agentic retrieval in the main loop.
- Source widening as an enabled production behavior. The config and source policy fields are prepared, but the first merged implementation keeps widening disabled.

## File Map

- Create `backend/memory/recall_stage3_models.py`: internal Stage 3 DTOs, telemetry, source policy, lane result types.
- Create `backend/memory/destination_normalization.py`: deterministic destination alias and hierarchy matching.
- Create `backend/memory/recall_stage3_normalizer.py`: converts `RecallRetrievalPlan` + message + plan facts into `RecallQueryEnvelope`.
- Create `backend/memory/recall_stage3_fusion.py`: lane union, weighted RRF, source/lane caps, deterministic tie-breaking.
- Create `backend/memory/recall_stage3_lanes.py`: symbolic, lexical, and semantic lane implementations.
- Create `backend/memory/embedding_provider.py`: FastEmbed-backed provider plus test-friendly fake/null provider contract.
- Create `backend/memory/recall_stage3.py`: public Stage 3 entrypoint used by `MemoryManager`.
- Modify `backend/config.py`: add Stage 3 config under `memory.retrieval.stage3`.
- Modify `backend/memory/manager.py`: call Stage 3 entrypoint, pass `active_plan` to Stage 4, attach Stage 3 telemetry.
- Modify `backend/memory/formatter.py`: add `stage3` telemetry field to `MemoryRecallTelemetry`.
- Existing `backend/pyproject.toml`: already contains `fastembed>=0.8.0,<0.9.0` from commit `987b104`.
- Existing `scripts/verify-stage3-embedding-runtime.py`: real runtime verification script; do not duplicate it in this plan.
- Test `backend/tests/test_stage3_config.py`: config defaults and YAML parsing.
- Test `backend/tests/test_destination_normalization.py`: exact, alias, parent-child, region-weak, mismatch.
- Test `backend/tests/test_recall_stage3_fusion.py`: RRF fusion, tie-breaking, caps.
- Test `backend/tests/test_recall_stage3_symbolic.py`: default symbolic equivalence.
- Test `backend/tests/test_recall_stage3_lexical.py`: synonym/expanded keyword recall behind flag.
- Test `backend/tests/test_recall_stage3_semantic.py`: semantic lane with fake embedding provider.
- Modify `backend/tests/test_memory_manager.py`: manager integration, active plan handoff, Stage 3 telemetry.

## Task 1: Add Stage 3 Config

**Files:**
- Modify: `backend/config.py`
- Create: `backend/tests/test_stage3_config.py`

- [ ] **Step 1: Write failing config tests**

Create `backend/tests/test_stage3_config.py`:

```python
from pathlib import Path

from config import (
    MemoryRetrievalConfig,
    Stage3RecallConfig,
    load_config,
)


def test_stage3_config_defaults_keep_new_behavior_disabled():
    config = MemoryRetrievalConfig()

    assert isinstance(config.stage3, Stage3RecallConfig)
    assert config.stage3.symbolic.enabled is True
    assert config.stage3.lexical.enabled is False
    assert config.stage3.semantic.enabled is False
    assert config.stage3.destination_normalization_enabled is False
    assert config.stage3.source_widening.enabled is False
    assert config.stage3.fusion.lane_weights == (
        ("symbolic", 1.0),
        ("lexical", 0.6),
        ("semantic", 0.8),
        ("entity", 0.4),
        ("temporal", 0.2),
    )


def test_stage3_config_loads_from_yaml(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
memory:
  retrieval:
    stage3:
      destination_normalization_enabled: true
      symbolic:
        enabled: true
        top_k: 11
        timeout_ms: 30
      lexical:
        enabled: true
        top_k: 7
        timeout_ms: 18
      semantic:
        enabled: true
        provider: fastembed
        model_name: BAAI/bge-small-zh-v1.5
        cache_dir: backend/data/embedding_cache
        local_files_only: true
        min_score: 0.61
        top_k: 9
      source_widening:
        enabled: true
        min_primary_candidates: 2
        max_secondary_candidates: 1
""",
        encoding="utf-8",
    )

    app_config = load_config(config_path)
    stage3 = app_config.memory.retrieval.stage3

    assert stage3.destination_normalization_enabled is True
    assert stage3.symbolic.top_k == 11
    assert stage3.lexical.enabled is True
    assert stage3.lexical.top_k == 7
    assert stage3.semantic.enabled is True
    assert stage3.semantic.local_files_only is True
    assert stage3.semantic.min_score == 0.61
    assert stage3.source_widening.enabled is True
    assert stage3.source_widening.max_secondary_candidates == 1
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_stage3_config.py -v
```

Expected: fails because `Stage3RecallConfig` and `MemoryRetrievalConfig.stage3` do not exist.

- [ ] **Step 3: Add config dataclasses**

In `backend/config.py`, add these dataclasses after `MemoryRerankerConfig`:

```python
@dataclass(frozen=True)
class Stage3LaneConfig:
    enabled: bool = True
    top_k: int = 20
    timeout_ms: int = 25


@dataclass(frozen=True)
class Stage3SemanticConfig(Stage3LaneConfig):
    enabled: bool = False
    provider: str = "fastembed"
    model_name: str = "BAAI/bge-small-zh-v1.5"
    cache_dir: str = "backend/data/embedding_cache"
    local_files_only: bool = False
    min_score: float = 0.58
    cache_max_items: int = 10000
    cache_max_mb: int = 64


@dataclass(frozen=True)
class Stage3FusionConfig:
    rrf_k: int = 60
    max_candidates: int = 30
    max_profile_candidates: int = 16
    max_slice_candidates: int = 16
    lane_weights: tuple[tuple[str, float], ...] = (
        ("symbolic", 1.0),
        ("lexical", 0.6),
        ("semantic", 0.8),
        ("entity", 0.4),
        ("temporal", 0.2),
    )


@dataclass(frozen=True)
class Stage3SourceWideningConfig:
    enabled: bool = False
    min_primary_candidates: int = 3
    max_secondary_candidates: int = 2


@dataclass(frozen=True)
class Stage3RecallConfig:
    symbolic: Stage3LaneConfig = field(default_factory=Stage3LaneConfig)
    lexical: Stage3LaneConfig = field(
        default_factory=lambda: Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=20)
    )
    semantic: Stage3SemanticConfig = field(default_factory=Stage3SemanticConfig)
    entity: Stage3LaneConfig = field(
        default_factory=lambda: Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=15)
    )
    temporal: Stage3LaneConfig = field(
        default_factory=lambda: Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=10)
    )
    fusion: Stage3FusionConfig = field(default_factory=Stage3FusionConfig)
    source_widening: Stage3SourceWideningConfig = field(
        default_factory=Stage3SourceWideningConfig
    )
    destination_normalization_enabled: bool = False
```

Then add `stage3` to `MemoryRetrievalConfig`:

```python
@dataclass(frozen=True)
class MemoryRetrievalConfig:
    core_limit: int = 10
    phase_limit: int = 8
    include_pending: bool = False
    recall_gate_enabled: bool = True
    recall_gate_model: str = ""
    recall_gate_timeout_seconds: float = 6.0
    reranker: MemoryRerankerConfig = field(default_factory=MemoryRerankerConfig)
    stage3: Stage3RecallConfig = field(default_factory=Stage3RecallConfig)
```

- [ ] **Step 4: Add YAML parser helpers**

In `backend/config.py`, add these helpers before `_build_memory_config()`:

```python
def _build_stage3_lane_config(raw: dict, default: Stage3LaneConfig) -> Stage3LaneConfig:
    return Stage3LaneConfig(
        enabled=_as_bool(raw.get("enabled"), default.enabled),
        top_k=int(raw.get("top_k", default.top_k)),
        timeout_ms=int(raw.get("timeout_ms", default.timeout_ms)),
    )


def _build_stage3_semantic_config(raw: dict) -> Stage3SemanticConfig:
    default = Stage3SemanticConfig()
    return Stage3SemanticConfig(
        enabled=_as_bool(raw.get("enabled"), default.enabled),
        top_k=int(raw.get("top_k", default.top_k)),
        timeout_ms=int(raw.get("timeout_ms", default.timeout_ms)),
        provider=str(raw.get("provider", default.provider)),
        model_name=str(raw.get("model_name", default.model_name)),
        cache_dir=str(raw.get("cache_dir", default.cache_dir)),
        local_files_only=_as_bool(raw.get("local_files_only"), default.local_files_only),
        min_score=float(raw.get("min_score", default.min_score)),
        cache_max_items=int(raw.get("cache_max_items", default.cache_max_items)),
        cache_max_mb=int(raw.get("cache_max_mb", default.cache_max_mb)),
    )


def _build_stage3_fusion_config(raw: dict) -> Stage3FusionConfig:
    default = Stage3FusionConfig()
    lane_weights = raw.get("lane_weights", default.lane_weights)
    if isinstance(lane_weights, dict):
        parsed_weights = tuple((str(key), float(value)) for key, value in lane_weights.items())
    elif isinstance(lane_weights, list):
        parsed_weights = tuple(
            (str(item[0]), float(item[1]))
            for item in lane_weights
            if isinstance(item, (list, tuple)) and len(item) == 2
        )
    else:
        parsed_weights = default.lane_weights
    return Stage3FusionConfig(
        rrf_k=int(raw.get("rrf_k", default.rrf_k)),
        max_candidates=int(raw.get("max_candidates", default.max_candidates)),
        max_profile_candidates=int(
            raw.get("max_profile_candidates", default.max_profile_candidates)
        ),
        max_slice_candidates=int(
            raw.get("max_slice_candidates", default.max_slice_candidates)
        ),
        lane_weights=parsed_weights or default.lane_weights,
    )


def _build_stage3_source_widening_config(raw: dict) -> Stage3SourceWideningConfig:
    default = Stage3SourceWideningConfig()
    return Stage3SourceWideningConfig(
        enabled=_as_bool(raw.get("enabled"), default.enabled),
        min_primary_candidates=int(
            raw.get("min_primary_candidates", default.min_primary_candidates)
        ),
        max_secondary_candidates=int(
            raw.get("max_secondary_candidates", default.max_secondary_candidates)
        ),
    )


def _build_stage3_recall_config(raw: dict) -> Stage3RecallConfig:
    return Stage3RecallConfig(
        symbolic=_build_stage3_lane_config(raw.get("symbolic", {}), Stage3LaneConfig()),
        lexical=_build_stage3_lane_config(
            raw.get("lexical", {}),
            Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=20),
        ),
        semantic=_build_stage3_semantic_config(raw.get("semantic", {})),
        entity=_build_stage3_lane_config(
            raw.get("entity", {}),
            Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=15),
        ),
        temporal=_build_stage3_lane_config(
            raw.get("temporal", {}),
            Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=10),
        ),
        fusion=_build_stage3_fusion_config(raw.get("fusion", {})),
        source_widening=_build_stage3_source_widening_config(
            raw.get("source_widening", {})
        ),
        destination_normalization_enabled=_as_bool(
            raw.get("destination_normalization_enabled"), False
        ),
    )
```

In `_build_memory_config()`, read `stage3_raw` and pass it into `MemoryRetrievalConfig`:

```python
stage3_raw = retrieval_raw.get("stage3", {})
```

Then add this field inside `MemoryRetrievalConfig(...)`:

```python
stage3=_build_stage3_recall_config(stage3_raw),
```

- [ ] **Step 5: Verify config tests pass**

Run:

```bash
cd backend && pytest tests/test_stage3_config.py -v
```

Expected: both tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/config.py backend/tests/test_stage3_config.py
git commit -m "feat: add stage3 recall config"
```

## Task 2: Add Destination Normalization

**Files:**
- Create: `backend/memory/destination_normalization.py`
- Create: `backend/tests/test_destination_normalization.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_destination_normalization.py`:

```python
from memory.destination_normalization import match_destination, normalize_destination


def test_normalize_destination_maps_aliases_to_canonical_name():
    normalized = normalize_destination("東京")

    assert normalized.canonical == "东京"
    assert "東京" in normalized.aliases
    assert normalized.region == "关东"


def test_match_destination_exact():
    result = match_destination("京都", "京都")

    assert result.match_type == "exact"
    assert result.score == 1.0


def test_match_destination_alias():
    result = match_destination("東京", "东京")

    assert result.match_type == "alias"
    assert result.score == 0.95


def test_match_destination_parent_child():
    result = match_destination("关西", "京都")

    assert result.match_type == "parent_child"
    assert result.score == 0.75


def test_match_destination_region_sibling_is_weak_not_exact():
    result = match_destination("大阪", "京都")

    assert result.match_type == "region_weak"
    assert result.score == 0.35


def test_match_destination_unrelated_destination():
    result = match_destination("巴黎", "京都")

    assert result.match_type == "none"
    assert result.score == 0.0
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_destination_normalization.py -v
```

Expected: fails because `memory.destination_normalization` does not exist.

- [ ] **Step 3: Implement destination normalization**

Create `backend/memory/destination_normalization.py`:

```python
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
    "东京": {
        "aliases": ("東京", "Tokyo", "tokyo"),
        "region": "关东",
        "children": (),
    },
    "千叶": {"aliases": ("Chiba",), "region": "关东", "children": ()},
    "埼玉": {"aliases": ("Saitama",), "region": "关东", "children": ()},
    "神奈川": {"aliases": ("Kanagawa",), "region": "关东", "children": ()},
    "京都": {"aliases": ("Kyoto",), "region": "关西", "children": ()},
    "大阪": {"aliases": ("Osaka",), "region": "关西", "children": ()},
    "奈良": {"aliases": ("Nara",), "region": "关西", "children": ()},
    "神户": {"aliases": ("Kobe",), "region": "关西", "children": ()},
    "关东": {
        "aliases": ("関東", "Kanto",),
        "region": "关东",
        "children": ("东京", "千叶", "埼玉", "神奈川"),
    },
    "关西": {
        "aliases": ("関西", "Kansai",),
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
for canonical, details in _DESTINATION_CATALOG.items():
    _ALIAS_TO_CANONICAL[canonical] = canonical
    for alias in details["aliases"]:
        _ALIAS_TO_CANONICAL[str(alias)] = canonical


def normalize_destination(value: str) -> NormalizedDestination:
    original = " ".join(str(value or "").split())
    canonical = _ALIAS_TO_CANONICAL.get(original, original)
    details = _DESTINATION_CATALOG.get(
        canonical,
        {"aliases": (), "region": "", "children": ()},
    )
    return NormalizedDestination(
        original=original,
        canonical=canonical,
        aliases=tuple(str(alias) for alias in details["aliases"]),
        region=str(details["region"]),
        children=tuple(str(child) for child in details["children"]),
    )


def match_destination(query_value: str, candidate_value: str) -> DestinationMatch:
    query = normalize_destination(query_value)
    candidate = normalize_destination(candidate_value)
    if not query.canonical or not candidate.canonical:
        return DestinationMatch(query, candidate, "none", 0.0)
    if query.canonical == candidate.canonical:
        match_type = "exact" if query.original == candidate.original else "alias"
        score = 1.0 if match_type == "exact" else 0.95
        return DestinationMatch(query, candidate, match_type, score)
    if candidate.canonical in query.children or query.canonical in candidate.children:
        return DestinationMatch(query, candidate, "parent_child", 0.75)
    if query.region and query.region == candidate.region:
        return DestinationMatch(query, candidate, "region_weak", 0.35)
    return DestinationMatch(query, candidate, "none", 0.0)
```

- [ ] **Step 4: Verify destination tests pass**

Run:

```bash
cd backend && pytest tests/test_destination_normalization.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/destination_normalization.py backend/tests/test_destination_normalization.py
git commit -m "feat: add destination normalization"
```

## Task 3: Add Stage 3 DTOs and Query Normalizer

**Files:**
- Create: `backend/memory/recall_stage3_models.py`
- Create: `backend/memory/recall_stage3_normalizer.py`
- Create: `backend/tests/test_recall_stage3_normalizer.py`

- [ ] **Step 1: Write failing normalizer tests**

Create `backend/tests/test_recall_stage3_normalizer.py`:

```python
from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_normalizer import build_query_envelope
from state.models import TravelPlanState


def _plan(**overrides) -> RecallRetrievalPlan:
    base = dict(
        source="profile",
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="東京",
        keywords=["住哪里"],
        top_k=5,
        reason="test",
    )
    base.update(overrides)
    return RecallRetrievalPlan(**base)


def test_build_query_envelope_preserves_default_profile_source_policy():
    envelope = build_query_envelope(
        query=_plan(),
        user_message="住宿按我习惯来",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert envelope.source_policy.requested_source == "profile"
    assert envelope.source_policy.search_profile is True
    assert envelope.source_policy.search_slices is False
    assert envelope.source_policy.widened is False
    assert envelope.destination == "東京"
    assert envelope.destination_canonical == ""
    assert envelope.expanded_keywords == ("住哪里",)


def test_build_query_envelope_expands_destination_when_enabled():
    config = Stage3RecallConfig(destination_normalization_enabled=True)

    envelope = build_query_envelope(
        query=_plan(),
        user_message="上次东京住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="t1", destination="东京"),
        config=config,
    )

    assert envelope.destination == "東京"
    assert envelope.destination_canonical == "东京"
    assert "東京" in envelope.destination_aliases
    assert envelope.destination_region == "关东"


def test_build_query_envelope_expands_hotel_keywords():
    envelope = build_query_envelope(
        query=_plan(keywords=["住宿"]),
        user_message="我上次住的地方怎么样",
        plan=TravelPlanState(session_id="s1", trip_id="t1"),
        config=Stage3RecallConfig(),
    )

    assert "住宿" in envelope.expanded_keywords
    assert "酒店" in envelope.expanded_keywords
    assert "民宿" in envelope.expanded_keywords
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_normalizer.py -v
```

Expected: fails because Stage 3 model and normalizer modules do not exist.

- [ ] **Step 3: Implement Stage 3 DTOs**

Create `backend/memory/recall_stage3_models.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from memory.recall_query import RecallRetrievalPlan
from memory.retrieval_candidates import RecallCandidate


@dataclass(frozen=True)
class SourcePolicy:
    requested_source: str
    search_profile: bool
    search_slices: bool
    widened: bool = False
    widening_reason: str = ""


@dataclass(frozen=True)
class RecallQueryEnvelope:
    plan: RecallRetrievalPlan
    user_message: str
    source_policy: SourcePolicy
    original_domains: tuple[str, ...]
    expanded_domains: tuple[str, ...]
    original_keywords: tuple[str, ...]
    expanded_keywords: tuple[str, ...]
    destination: str
    destination_canonical: str = ""
    destination_aliases: tuple[str, ...] = ()
    destination_children: tuple[str, ...] = ()
    destination_region: str = ""


@dataclass
class RetrievalEvidence:
    item_id: str
    source: str
    lanes: list[str] = field(default_factory=list)
    lane_scores: dict[str, float] = field(default_factory=dict)
    lane_ranks: dict[str, int] = field(default_factory=dict)
    fused_score: float = 0.0
    matched_domains: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    matched_entities: list[str] = field(default_factory=list)
    destination_match_type: str = "none"
    semantic_score: float | None = None
    lexical_score: float | None = None
    temporal_score: float | None = None
    retrieval_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "source": self.source,
            "lanes": list(self.lanes),
            "lane_scores": dict(self.lane_scores),
            "lane_ranks": dict(self.lane_ranks),
            "fused_score": self.fused_score,
            "matched_domains": list(self.matched_domains),
            "matched_keywords": list(self.matched_keywords),
            "matched_entities": list(self.matched_entities),
            "destination_match_type": self.destination_match_type,
            "semantic_score": self.semantic_score,
            "lexical_score": self.lexical_score,
            "temporal_score": self.temporal_score,
            "retrieval_reason": self.retrieval_reason,
        }


@dataclass
class Stage3Candidate:
    candidate: RecallCandidate
    evidence: RetrievalEvidence


@dataclass
class Stage3LaneResult:
    lane_name: str
    candidates: list[Stage3Candidate]
    error: str = ""


@dataclass
class Stage3Telemetry:
    lanes_attempted: list[str] = field(default_factory=list)
    lanes_succeeded: list[str] = field(default_factory=list)
    source_policy: dict[str, Any] = field(default_factory=dict)
    query_expansion: dict[str, list[str]] = field(default_factory=dict)
    candidates_by_lane: dict[str, int] = field(default_factory=dict)
    candidates_by_source: dict[str, int] = field(default_factory=dict)
    total_candidates_before_fusion: int = 0
    total_candidates_after_fusion: int = 0
    zero_hit: bool = False
    fallback_used: str = "none"
    lane_errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lanes_attempted": list(self.lanes_attempted),
            "lanes_succeeded": list(self.lanes_succeeded),
            "source_policy": dict(self.source_policy),
            "query_expansion": {
                key: list(value) for key, value in self.query_expansion.items()
            },
            "candidates_by_lane": dict(self.candidates_by_lane),
            "candidates_by_source": dict(self.candidates_by_source),
            "total_candidates_before_fusion": self.total_candidates_before_fusion,
            "total_candidates_after_fusion": self.total_candidates_after_fusion,
            "zero_hit": self.zero_hit,
            "fallback_used": self.fallback_used,
            "lane_errors": dict(self.lane_errors),
        }


@dataclass
class Stage3RecallResult:
    candidates: list[RecallCandidate]
    evidence_by_id: dict[str, RetrievalEvidence]
    telemetry: Stage3Telemetry
```

- [ ] **Step 4: Implement query normalizer**

Create `backend/memory/recall_stage3_normalizer.py`:

```python
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
    "住哪里": ("住哪里", "住宿", "酒店", "民宿", "旅馆"),
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
    *,
    query: RecallRetrievalPlan,
    user_message: str,
    plan: TravelPlanState,
    config: Stage3RecallConfig,
) -> RecallQueryEnvelope:
    destination = query.destination or getattr(plan, "destination", "") or ""
    source_policy = _build_source_policy(query, config)
    expanded_domains = _expand_domains(query.domains)
    expanded_keywords = _expand_keywords([*query.keywords, user_message])

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
        expanded_domains=tuple(expanded_domains),
        original_keywords=tuple(query.keywords),
        expanded_keywords=tuple(expanded_keywords),
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
    return SourcePolicy(
        requested_source=source,
        search_profile=search_profile,
        search_slices=search_slices,
        widened=False if not config.source_widening.enabled else False,
        widening_reason="",
    )


def _expand_domains(domains: list[str]) -> list[str]:
    expanded: list[str] = []
    for domain in domains:
        values = _DOMAIN_EXPANSIONS.get(domain, (domain,))
        for value in values:
            if value and value not in expanded:
                expanded.append(value)
    return expanded


def _expand_keywords(values: list[str]) -> list[str]:
    expanded: list[str] = []
    joined = "\n".join(values)
    for value in values:
        if not value:
            continue
        if value not in expanded:
            expanded.append(value)
        for trigger, synonyms in _KEYWORD_EXPANSIONS.items():
            if trigger in value or trigger in joined:
                for synonym in synonyms:
                    if synonym not in expanded:
                        expanded.append(synonym)
    return expanded
```

- [ ] **Step 5: Verify normalizer tests pass**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_normalizer.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/memory/recall_stage3_models.py backend/memory/recall_stage3_normalizer.py backend/tests/test_recall_stage3_normalizer.py
git commit -m "feat: add stage3 query envelope"
```

## Task 4: Add Candidate Fusion

**Files:**
- Create: `backend/memory/recall_stage3_fusion.py`
- Create: `backend/tests/test_recall_stage3_fusion.py`

- [ ] **Step 1: Write failing fusion tests**

Create `backend/tests/test_recall_stage3_fusion.py`:

```python
from config import Stage3FusionConfig
from memory.recall_stage3_fusion import fuse_lane_results
from memory.recall_stage3_models import RetrievalEvidence, Stage3Candidate, Stage3LaneResult
from memory.retrieval_candidates import RecallCandidate


def _candidate(item_id: str, source: str = "profile") -> Stage3Candidate:
    return Stage3Candidate(
        candidate=RecallCandidate(
            source=source,
            item_id=item_id,
            bucket="stable_preferences",
            score=1.0,
            matched_reason=["test"],
            content_summary=f"{item_id} content",
            domains=["hotel"],
            applicability="test",
        ),
        evidence=RetrievalEvidence(
            item_id=item_id,
            source=source,
            lanes=[],
            retrieval_reason="test",
        ),
    )


def test_fuse_lane_results_unions_duplicate_candidates_and_tracks_lanes():
    result = fuse_lane_results(
        [
            Stage3LaneResult("symbolic", [_candidate("a"), _candidate("b")]),
            Stage3LaneResult("lexical", [_candidate("b"), _candidate("c")]),
        ],
        Stage3FusionConfig(max_candidates=10),
    )

    assert [candidate.candidate.item_id for candidate in result] == ["b", "a", "c"]
    assert result[0].evidence.lanes == ["symbolic", "lexical"]
    assert result[0].evidence.lane_ranks == {"symbolic": 2, "lexical": 1}


def test_fuse_lane_results_applies_source_caps():
    result = fuse_lane_results(
        [
            Stage3LaneResult("symbolic", [_candidate("p1"), _candidate("p2")]),
            Stage3LaneResult("semantic", [_candidate("s1", source="episode_slice")]),
        ],
        Stage3FusionConfig(
            max_candidates=10,
            max_profile_candidates=1,
            max_slice_candidates=1,
        ),
    )

    assert [candidate.candidate.item_id for candidate in result] == ["p1", "s1"]
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_fusion.py -v
```

Expected: fails because fusion module does not exist.

- [ ] **Step 3: Implement fusion**

Create `backend/memory/recall_stage3_fusion.py`:

```python
from __future__ import annotations

from config import Stage3FusionConfig
from memory.recall_stage3_models import RetrievalEvidence, Stage3Candidate, Stage3LaneResult


def fuse_lane_results(
    lane_results: list[Stage3LaneResult],
    config: Stage3FusionConfig,
) -> list[Stage3Candidate]:
    lane_weights = dict(config.lane_weights)
    by_id: dict[str, Stage3Candidate] = {}

    for lane_result in lane_results:
        weight = float(lane_weights.get(lane_result.lane_name, 0.0))
        if weight <= 0.0:
            continue
        for index, stage3_candidate in enumerate(lane_result.candidates):
            item_id = stage3_candidate.candidate.item_id
            rank = index + 1
            contribution = weight / float(config.rrf_k + rank)
            if item_id not in by_id:
                copied = Stage3Candidate(
                    candidate=stage3_candidate.candidate,
                    evidence=_copy_evidence(stage3_candidate.evidence),
                )
                by_id[item_id] = copied
            target = by_id[item_id]
            if lane_result.lane_name not in target.evidence.lanes:
                target.evidence.lanes.append(lane_result.lane_name)
            target.evidence.lane_ranks[lane_result.lane_name] = rank
            target.evidence.lane_scores[lane_result.lane_name] = contribution
            target.evidence.fused_score += contribution
            _merge_evidence(target.evidence, stage3_candidate.evidence)

    ranked = sorted(
        by_id.values(),
        key=lambda candidate: (
            -candidate.evidence.fused_score,
            0 if "symbolic" in candidate.evidence.lanes else 1,
            candidate.candidate.source,
            candidate.candidate.item_id,
        ),
    )
    return _apply_caps(ranked, config)


def _copy_evidence(evidence: RetrievalEvidence) -> RetrievalEvidence:
    return RetrievalEvidence(
        item_id=evidence.item_id,
        source=evidence.source,
        lanes=list(evidence.lanes),
        lane_scores=dict(evidence.lane_scores),
        lane_ranks=dict(evidence.lane_ranks),
        fused_score=evidence.fused_score,
        matched_domains=list(evidence.matched_domains),
        matched_keywords=list(evidence.matched_keywords),
        matched_entities=list(evidence.matched_entities),
        destination_match_type=evidence.destination_match_type,
        semantic_score=evidence.semantic_score,
        lexical_score=evidence.lexical_score,
        temporal_score=evidence.temporal_score,
        retrieval_reason=evidence.retrieval_reason,
    )


def _merge_evidence(target: RetrievalEvidence, source: RetrievalEvidence) -> None:
    for value in source.matched_domains:
        if value not in target.matched_domains:
            target.matched_domains.append(value)
    for value in source.matched_keywords:
        if value not in target.matched_keywords:
            target.matched_keywords.append(value)
    for value in source.matched_entities:
        if value not in target.matched_entities:
            target.matched_entities.append(value)
    if target.destination_match_type == "none" and source.destination_match_type != "none":
        target.destination_match_type = source.destination_match_type
    if source.semantic_score is not None:
        target.semantic_score = max(target.semantic_score or 0.0, source.semantic_score)
    if source.lexical_score is not None:
        target.lexical_score = max(target.lexical_score or 0.0, source.lexical_score)
    if source.temporal_score is not None:
        target.temporal_score = max(target.temporal_score or 0.0, source.temporal_score)
    if source.retrieval_reason and source.retrieval_reason not in target.retrieval_reason:
        target.retrieval_reason = (
            f"{target.retrieval_reason}; {source.retrieval_reason}"
            if target.retrieval_reason
            else source.retrieval_reason
        )


def _apply_caps(
    ranked: list[Stage3Candidate],
    config: Stage3FusionConfig,
) -> list[Stage3Candidate]:
    selected: list[Stage3Candidate] = []
    source_counts = {"profile": 0, "episode_slice": 0}
    for candidate in ranked:
        source = candidate.candidate.source
        if source == "profile" and source_counts["profile"] >= config.max_profile_candidates:
            continue
        if source == "episode_slice" and source_counts["episode_slice"] >= config.max_slice_candidates:
            continue
        selected.append(candidate)
        if source in source_counts:
            source_counts[source] += 1
        if len(selected) >= config.max_candidates:
            break
    return selected
```

- [ ] **Step 4: Verify fusion tests pass**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_fusion.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/memory/recall_stage3_fusion.py backend/tests/test_recall_stage3_fusion.py
git commit -m "feat: add stage3 candidate fusion"
```

## Task 5: Add Symbolic Lane and Stage 3 Entrypoint

**Files:**
- Create: `backend/memory/recall_stage3_lanes.py`
- Create: `backend/memory/recall_stage3.py`
- Create: `backend/tests/test_recall_stage3_symbolic.py`

- [ ] **Step 1: Write failing symbolic equivalence tests**

Create `backend/tests/test_recall_stage3_symbolic.py`:

```python
from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.symbolic_recall import rank_episode_slices, rank_profile_items
from memory.v3_models import EpisodeSlice, MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


def _profile() -> UserMemoryProfile:
    return UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:preferred_area",
                domain="hotel",
                key="preferred_area",
                value="京都四条附近",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                recall_hints={"domains": ["hotel"], "keywords": ["住宿", "住哪里"]},
                applicability="适用于大多数住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )


def _slices() -> list[EpisodeSlice]:
    return [
        EpisodeSlice(
            id="slice_1",
            user_id="u1",
            source_episode_id="ep1",
            source_trip_id="old_trip",
            slice_type="accommodation_decision",
            domains=["hotel"],
            entities={"destination": "京都"},
            keywords=["住宿"],
            content="上次京都住四条附近的町屋。",
            applicability="仅供住宿选择参考。",
            created_at="2026-04-03T00:00:00",
        )
    ]


def _query(source: str = "hybrid_history") -> RecallRetrievalPlan:
    return RecallRetrievalPlan(
        source=source,
        buckets=["stable_preferences"],
        domains=["hotel"],
        destination="京都",
        keywords=["住宿"],
        top_k=5,
        reason="test",
    )


def test_stage3_symbolic_default_matches_existing_symbolic_candidates():
    profile = _profile()
    slices = _slices()
    query = _query()

    expected = [
        *rank_profile_items(query, profile)[: query.top_k],
        *rank_episode_slices(query, slices)[: query.top_k],
    ]
    result = retrieve_recall_candidates(
        query=query,
        profile=profile,
        slices=slices,
        user_message="上次京都住哪里",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        candidate.item_id for candidate in expected
    ]
    assert result.telemetry.lanes_attempted == ["symbolic"]
    assert result.telemetry.zero_hit is False
    assert set(result.evidence_by_id) == {"stable_preferences:hotel:preferred_area", "slice_1"}


def test_stage3_symbolic_default_reports_zero_hit():
    result = retrieve_recall_candidates(
        query=_query(source="profile"),
        profile=UserMemoryProfile.empty("u1"),
        slices=[],
        user_message="住宿按我习惯",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=Stage3RecallConfig(),
    )

    assert result.candidates == []
    assert result.telemetry.zero_hit is True
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_symbolic.py -v
```

Expected: fails because Stage 3 lane and entrypoint modules do not exist.

- [ ] **Step 3: Implement symbolic lane**

Create `backend/memory/recall_stage3_lanes.py` with the symbolic lane first:

```python
from __future__ import annotations

from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_models import RecallQueryEnvelope, RetrievalEvidence, Stage3Candidate, Stage3LaneResult
from memory.retrieval_candidates import RecallCandidate
from memory.symbolic_recall import rank_episode_slices, rank_profile_items
from memory.v3_models import EpisodeSlice, UserMemoryProfile


class SymbolicLane:
    lane_name = "symbolic"

    def run(
        self,
        envelope: RecallQueryEnvelope,
        profile: UserMemoryProfile,
        slices: list[EpisodeSlice],
        config: Stage3RecallConfig,
    ) -> Stage3LaneResult:
        candidates: list[RecallCandidate] = []
        lane_plan = _plan_for_source_policy(envelope)
        symbolic_limit = min(config.symbolic.top_k, envelope.plan.top_k)
        if envelope.source_policy.search_profile:
            candidates.extend(rank_profile_items(lane_plan, profile)[:symbolic_limit])
        if envelope.source_policy.search_slices:
            candidates.extend(rank_episode_slices(lane_plan, slices)[:symbolic_limit])
        return Stage3LaneResult(
            lane_name=self.lane_name,
            candidates=[
                Stage3Candidate(candidate=candidate, evidence=_evidence_from_candidate(candidate, self.lane_name))
                for candidate in candidates
            ],
        )


def _plan_for_source_policy(envelope: RecallQueryEnvelope) -> RecallRetrievalPlan:
    if envelope.source_policy.search_profile and envelope.source_policy.search_slices:
        source = "hybrid_history"
    elif envelope.source_policy.search_profile:
        source = "profile"
    elif envelope.source_policy.search_slices:
        source = "episode_slice"
    else:
        source = envelope.plan.source
    return RecallRetrievalPlan(
        source=source,
        buckets=list(envelope.plan.buckets),
        domains=list(envelope.plan.domains),
        destination=envelope.plan.destination,
        keywords=list(envelope.plan.keywords),
        top_k=envelope.plan.top_k,
        reason=envelope.plan.reason,
        fallback_used=envelope.plan.fallback_used,
    )


def _evidence_from_candidate(candidate: RecallCandidate, lane_name: str) -> RetrievalEvidence:
    matched_domains = [
        domain for domain in candidate.domains if any(domain in reason for reason in candidate.matched_reason)
    ]
    matched_keywords = [
        reason.split("keyword match on ", 1)[1]
        for reason in candidate.matched_reason
        if "keyword match on " in reason
    ]
    destination_match_type = (
        "exact" if any("exact destination match" in reason for reason in candidate.matched_reason) else "none"
    )
    return RetrievalEvidence(
        item_id=candidate.item_id,
        source=candidate.source,
        lanes=[lane_name],
        matched_domains=matched_domains,
        matched_keywords=matched_keywords,
        destination_match_type=destination_match_type,
        retrieval_reason="; ".join(candidate.matched_reason),
    )
```

- [ ] **Step 4: Implement Stage 3 entrypoint**

Create `backend/memory/recall_stage3.py`:

```python
from __future__ import annotations

from config import Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3_fusion import fuse_lane_results
from memory.recall_stage3_lanes import SymbolicLane
from memory.recall_stage3_models import Stage3RecallResult, Stage3Telemetry
from memory.recall_stage3_normalizer import build_query_envelope
from memory.v3_models import EpisodeSlice, UserMemoryProfile
from state.models import TravelPlanState


def retrieve_recall_candidates(
    *,
    query: RecallRetrievalPlan,
    profile: UserMemoryProfile,
    slices: list[EpisodeSlice],
    user_message: str,
    plan: TravelPlanState,
    config: Stage3RecallConfig,
    embedding_provider: object | None = None,
) -> Stage3RecallResult:
    envelope = build_query_envelope(
        query=query,
        user_message=user_message,
        plan=plan,
        config=config,
    )
    telemetry = Stage3Telemetry(
        source_policy={
            "requested_source": envelope.source_policy.requested_source,
            "search_profile": envelope.source_policy.search_profile,
            "search_slices": envelope.source_policy.search_slices,
            "widened": envelope.source_policy.widened,
            "widening_reason": envelope.source_policy.widening_reason,
        },
        query_expansion={
            "domains": list(envelope.expanded_domains),
            "keywords": list(envelope.expanded_keywords),
            "destination_aliases": list(envelope.destination_aliases),
            "destination_children": list(envelope.destination_children),
        },
    )

    lane_results = []
    if config.symbolic.enabled:
        telemetry.lanes_attempted.append("symbolic")
        symbolic_result = SymbolicLane().run(envelope, profile, slices, config)
        lane_results.append(symbolic_result)
        telemetry.candidates_by_lane["symbolic"] = len(symbolic_result.candidates)
        if not symbolic_result.error:
            telemetry.lanes_succeeded.append("symbolic")
        else:
            telemetry.lane_errors["symbolic"] = symbolic_result.error

    telemetry.total_candidates_before_fusion = sum(
        len(result.candidates) for result in lane_results
    )
    fused = fuse_lane_results(lane_results, config.fusion)
    telemetry.total_candidates_after_fusion = len(fused)
    telemetry.zero_hit = len(fused) == 0
    candidates_by_source: dict[str, int] = {}
    for stage3_candidate in fused:
        source = stage3_candidate.candidate.source
        candidates_by_source[source] = candidates_by_source.get(source, 0) + 1
    telemetry.candidates_by_source = candidates_by_source

    return Stage3RecallResult(
        candidates=[stage3_candidate.candidate for stage3_candidate in fused],
        evidence_by_id={
            stage3_candidate.candidate.item_id: stage3_candidate.evidence
            for stage3_candidate in fused
        },
        telemetry=telemetry,
    )
```

- [ ] **Step 5: Verify symbolic tests pass**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_symbolic.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Run existing symbolic tests**

Run:

```bash
cd backend && pytest tests/test_symbolic_recall.py -v
```

Expected: existing symbolic recall tests still pass.

- [ ] **Step 7: Commit**

```bash
git add backend/memory/recall_stage3_lanes.py backend/memory/recall_stage3.py backend/tests/test_recall_stage3_symbolic.py
git commit -m "feat: add stage3 symbolic entrypoint"
```

## Task 6: Add Lexical Lane Behind Feature Flag

**Files:**
- Modify: `backend/memory/recall_stage3_lanes.py`
- Modify: `backend/memory/recall_stage3.py`
- Create: `backend/tests/test_recall_stage3_lexical.py`

- [ ] **Step 1: Write failing lexical lane tests**

Create `backend/tests/test_recall_stage3_lexical.py`:

```python
from dataclasses import replace

from config import Stage3LaneConfig, Stage3RecallConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.v3_models import MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


def test_lexical_lane_recalls_profile_by_expanded_keyword_when_symbolic_misses():
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:quiet_stay",
                domain="hotel",
                key="quiet_stay",
                value="喜欢安静的旅馆",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                recall_hints={"keywords": ["旅馆", "安静"]},
                applicability="适用于住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )
    config = replace(
        Stage3RecallConfig(),
        symbolic=Stage3LaneConfig(enabled=False, top_k=20, timeout_ms=25),
        lexical=Stage3LaneConfig(enabled=True, top_k=20, timeout_ms=20),
    )

    result = retrieve_recall_candidates(
        query=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
        profile=profile,
        slices=[],
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=config,
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:quiet_stay"
    ]
    evidence = result.evidence_by_id["stable_preferences:hotel:quiet_stay"]
    assert evidence.lexical_score is not None
    assert "lexical" in evidence.lanes
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_lexical.py -v
```

Expected: fails because lexical lane is not implemented.

- [ ] **Step 3: Add lexical helpers and lane**

Append this code to `backend/memory/recall_stage3_lanes.py`:

```python
import json
from typing import Any

from memory.retrieval_candidates import build_episode_slice_candidates, build_profile_candidates
from memory.v3_models import MemoryProfileItem


class LexicalLane:
    lane_name = "lexical"

    def run(
        self,
        envelope: RecallQueryEnvelope,
        profile: UserMemoryProfile,
        slices: list[EpisodeSlice],
        config: Stage3RecallConfig,
    ) -> Stage3LaneResult:
        ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
        query_terms = _tokenize_for_lexical(
            " ".join([envelope.user_message, *envelope.expanded_domains, *envelope.expanded_keywords])
        )
        if not query_terms:
            return Stage3LaneResult(self.lane_name, [])

        if envelope.source_policy.search_profile:
            ranked.extend(_rank_profile_lexical(envelope, profile, query_terms))
        if envelope.source_policy.search_slices:
            ranked.extend(_rank_slices_lexical(envelope, slices, query_terms))

        ranked.sort(key=lambda entry: (-entry[0], entry[1].source, entry[1].item_id))
        top_ranked = ranked[: config.lexical.top_k]
        return Stage3LaneResult(
            self.lane_name,
            [
                Stage3Candidate(candidate=candidate, evidence=evidence)
                for _, candidate, evidence in top_ranked
            ],
        )


def _rank_profile_lexical(
    envelope: RecallQueryEnvelope,
    profile: UserMemoryProfile,
    query_terms: set[str],
) -> list[tuple[float, RecallCandidate, RetrievalEvidence]]:
    ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
    bucket_candidates: list[tuple[str, MemoryProfileItem, str]] = []
    for bucket in envelope.plan.buckets:
        for item in getattr(profile, bucket, []):
            text = _profile_item_text(bucket, item)
            score = _lexical_score(query_terms, text)
            if score <= 0.0:
                continue
            reason = f"lexical match score={score:.3f}"
            bucket_candidates.append((bucket, item, reason))
    candidates = build_profile_candidates(bucket_candidates)
    for candidate in candidates:
        score = _lexical_score(query_terms, candidate.content_summary)
        evidence = _evidence_from_candidate(candidate, "lexical")
        evidence.lexical_score = score
        evidence.lanes = ["lexical"]
        evidence.retrieval_reason = "lexical keyword expansion"
        ranked.append((score, candidate, evidence))
    return ranked


def _rank_slices_lexical(
    envelope: RecallQueryEnvelope,
    slices: list[EpisodeSlice],
    query_terms: set[str],
) -> list[tuple[float, RecallCandidate, RetrievalEvidence]]:
    ranked_slices: list[tuple[EpisodeSlice, str]] = []
    score_by_id: dict[str, float] = {}
    for slice_ in slices:
        score = _lexical_score(query_terms, _slice_text(slice_))
        if score <= 0.0:
            continue
        score_by_id[slice_.id] = score
        ranked_slices.append((slice_, f"lexical match score={score:.3f}"))
    ranked_slices.sort(key=lambda entry: (-score_by_id[entry[0].id], entry[0].id))
    candidates = build_episode_slice_candidates(ranked_slices)
    ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
    for candidate in candidates:
        score = score_by_id.get(candidate.item_id, 0.0)
        evidence = _evidence_from_candidate(candidate, "lexical")
        evidence.lexical_score = score
        evidence.lanes = ["lexical"]
        evidence.retrieval_reason = "lexical keyword expansion"
        ranked.append((score, candidate, evidence))
    return ranked


def _lexical_score(query_terms: set[str], text: str) -> float:
    document_terms = _tokenize_for_lexical(text)
    if not document_terms:
        return 0.0
    overlap = query_terms & document_terms
    if not overlap:
        return 0.0
    precision = len(overlap) / float(len(document_terms))
    recall = len(overlap) / float(len(query_terms))
    return 2.0 * precision * recall / max(precision + recall, 1e-6)


def _tokenize_for_lexical(text: str) -> set[str]:
    normalized = " ".join(str(text or "").split())
    tokens: set[str] = {part for part in normalized.replace("，", " ").replace("。", " ").split() if part}
    for index, char in enumerate(normalized):
        if char.isspace():
            continue
        tokens.add(char)
        if index + 1 < len(normalized) and not normalized[index + 1].isspace():
            tokens.add(normalized[index : index + 2])
    return tokens


def _profile_item_text(bucket: str, item: MemoryProfileItem) -> str:
    return " ".join(
        part
        for part in [
            bucket,
            item.domain,
            item.key,
            _stringify_for_stage3(item.value),
            item.applicability,
            _stringify_for_stage3(item.context),
            _stringify_for_stage3(item.recall_hints),
        ]
        if part
    )


def _slice_text(slice_: EpisodeSlice) -> str:
    return " ".join(
        part
        for part in [
            slice_.slice_type,
            " ".join(slice_.domains),
            _stringify_for_stage3(slice_.entities),
            " ".join(slice_.keywords),
            slice_.content,
            slice_.applicability,
        ]
        if part
    )


def _stringify_for_stage3(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
```

Keep the lexical helpers in the same module for this phase; split later only if the file grows past readability.

- [ ] **Step 4: Wire lexical lane into entrypoint**

Modify imports in `backend/memory/recall_stage3.py`:

```python
from memory.recall_stage3_lanes import LexicalLane, SymbolicLane
```

After the symbolic lane block, add:

```python
    if config.lexical.enabled:
        telemetry.lanes_attempted.append("lexical")
        lexical_result = LexicalLane().run(envelope, profile, slices, config)
        lane_results.append(lexical_result)
        telemetry.candidates_by_lane["lexical"] = len(lexical_result.candidates)
        if not lexical_result.error:
            telemetry.lanes_succeeded.append("lexical")
        else:
            telemetry.lane_errors["lexical"] = lexical_result.error
```

- [ ] **Step 5: Verify lexical tests pass**

Run:

```bash
cd backend && pytest tests/test_recall_stage3_lexical.py tests/test_recall_stage3_symbolic.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add backend/memory/recall_stage3_lanes.py backend/memory/recall_stage3.py backend/tests/test_recall_stage3_lexical.py
git commit -m "feat: add stage3 lexical lane"
```

## Task 7: Add Embedding Provider and Semantic Lane

**Files:**
- Create: `backend/memory/embedding_provider.py`
- Modify: `backend/memory/recall_stage3_lanes.py`
- Modify: `backend/memory/recall_stage3.py`
- Create: `backend/tests/test_embedding_provider.py`
- Create: `backend/tests/test_recall_stage3_semantic.py`

**Precondition:** the real FastEmbed runtime has already been verified in commit `987b104`. This task implements the provider wrapper and semantic lane only. Do not add `fastembed` again, do not create another runtime verification script, and do not make unit tests instantiate the real model.

- [ ] **Step 1: Write provider tests**

Create `backend/tests/test_embedding_provider.py`:

```python
from memory.embedding_provider import NullEmbeddingProvider, cosine_similarity


def test_cosine_similarity_returns_expected_value():
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0


def test_null_embedding_provider_returns_empty_vectors():
    provider = NullEmbeddingProvider()

    assert provider.embed(["京都住宿"]) == [[]]
```

- [ ] **Step 2: Write semantic lane tests with fake provider**

Create `backend/tests/test_recall_stage3_semantic.py`:

```python
from dataclasses import replace

from config import Stage3RecallConfig, Stage3SemanticConfig
from memory.recall_query import RecallRetrievalPlan
from memory.recall_stage3 import retrieve_recall_candidates
from memory.v3_models import MemoryProfileItem, UserMemoryProfile
from state.models import TravelPlanState


class FakeEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            if "安静" in text or "清静" in text:
                vectors.append([1.0, 0.0])
            elif "红眼" in text:
                vectors.append([0.0, 1.0])
            else:
                vectors.append([0.8, 0.2])
        return vectors


def test_semantic_lane_recalls_synonymous_profile_when_enabled():
    profile = UserMemoryProfile(
        schema_version=3,
        user_id="u1",
        stable_preferences=[
            MemoryProfileItem(
                id="stable_preferences:hotel:quiet",
                domain="hotel",
                key="quiet",
                value="偏好清静的住宿环境",
                polarity="prefer",
                stability="stable",
                confidence=0.9,
                status="active",
                applicability="适用于住宿选择。",
                created_at="2026-04-01T00:00:00",
                updated_at="2026-04-02T00:00:00",
            )
        ],
    )
    config = replace(
        Stage3RecallConfig(),
        semantic=Stage3SemanticConfig(enabled=True, min_score=0.7, top_k=5),
    )

    result = retrieve_recall_candidates(
        query=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
        profile=profile,
        slices=[],
        user_message="这次住宿想安静一点",
        plan=TravelPlanState(session_id="s1", trip_id="now"),
        config=config,
        embedding_provider=FakeEmbeddingProvider(),
    )

    assert [candidate.item_id for candidate in result.candidates] == [
        "stable_preferences:hotel:quiet"
    ]
    evidence = result.evidence_by_id["stable_preferences:hotel:quiet"]
    assert "semantic" in evidence.lanes
    assert evidence.semantic_score is not None
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_embedding_provider.py tests/test_recall_stage3_semantic.py -v
```

Expected: fails because embedding provider and semantic lane are not implemented.

- [ ] **Step 4: Implement embedding provider**

Create `backend/memory/embedding_provider.py`:

```python
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Protocol


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class NullEmbeddingProvider:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[] for _ in texts]


class CachedEmbeddingProvider:
    def __init__(self, provider: EmbeddingProvider, max_items: int = 10000):
        self._provider = provider
        self._max_items = max_items
        self._cache: OrderedDict[str, list[float]] = OrderedDict()

    def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [text for text in texts if text not in self._cache]
        if missing:
            for text, vector in zip(missing, self._provider.embed(missing), strict=False):
                self._cache[text] = vector
                self._cache.move_to_end(text)
                while len(self._cache) > self._max_items:
                    self._cache.popitem(last=False)
        result = []
        for text in texts:
            self._cache.move_to_end(text)
            result.append(self._cache[text])
        return result


class FastEmbedProvider:
    def __init__(
        self,
        *,
        model_name: str,
        cache_dir: str,
        local_files_only: bool,
    ):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(
            model_name=model_name,
            cache_dir=str(Path(cache_dir)),
            local_files_only=local_files_only,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [list(vector) for vector in self._model.embed(texts)]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)
```

- [ ] **Step 5: Add semantic lane**

Append this code to `backend/memory/recall_stage3_lanes.py`:

```python
from memory.embedding_provider import EmbeddingProvider, cosine_similarity


class SemanticLane:
    lane_name = "semantic"

    def run(
        self,
        envelope: RecallQueryEnvelope,
        profile: UserMemoryProfile,
        slices: list[EpisodeSlice],
        config: Stage3RecallConfig,
        embedding_provider: EmbeddingProvider | None,
    ) -> Stage3LaneResult:
        if embedding_provider is None:
            return Stage3LaneResult(self.lane_name, [], error="embedding_provider_missing")

        records = _semantic_records(envelope, profile, slices)
        if not records:
            return Stage3LaneResult(self.lane_name, [])

        query_text = " ".join(
            [envelope.user_message, *envelope.expanded_domains, *envelope.expanded_keywords]
        )
        try:
            vectors = embedding_provider.embed([query_text, *[record[2] for record in records]])
        except Exception as exc:
            return Stage3LaneResult(self.lane_name, [], error=f"embedding_error:{type(exc).__name__}")
        if len(vectors) != len(records) + 1:
            return Stage3LaneResult(self.lane_name, [], error="embedding_count_mismatch")

        query_vector = vectors[0]
        ranked: list[tuple[float, RecallCandidate, RetrievalEvidence]] = []
        for (candidate, evidence, _), vector in zip(records, vectors[1:], strict=False):
            score = cosine_similarity(query_vector, vector)
            if score < config.semantic.min_score:
                continue
            evidence.semantic_score = score
            evidence.lanes = ["semantic"]
            evidence.retrieval_reason = f"semantic cosine score={score:.3f}"
            ranked.append((score, candidate, evidence))

        ranked.sort(key=lambda entry: (-entry[0], entry[1].source, entry[1].item_id))
        return Stage3LaneResult(
            self.lane_name,
            [
                Stage3Candidate(candidate=candidate, evidence=evidence)
                for _, candidate, evidence in ranked[: config.semantic.top_k]
            ],
        )


def _semantic_records(
    envelope: RecallQueryEnvelope,
    profile: UserMemoryProfile,
    slices: list[EpisodeSlice],
) -> list[tuple[RecallCandidate, RetrievalEvidence, str]]:
    records: list[tuple[RecallCandidate, RetrievalEvidence, str]] = []
    if envelope.source_policy.search_profile:
        profile_rows: list[tuple[str, MemoryProfileItem, str]] = []
        text_by_id: dict[str, str] = {}
        for bucket in envelope.plan.buckets:
            for item in getattr(profile, bucket, []):
                text = _profile_item_text(bucket, item)
                text_by_id[item.id] = text
                profile_rows.append((bucket, item, "semantic candidate"))
        for candidate in build_profile_candidates(profile_rows):
            evidence = _evidence_from_candidate(candidate, "semantic")
            records.append((candidate, evidence, text_by_id.get(candidate.item_id, candidate.content_summary)))

    if envelope.source_policy.search_slices:
        slice_rows = [(slice_, "semantic candidate") for slice_ in slices]
        text_by_id = {slice_.id: _slice_text(slice_) for slice_ in slices}
        for candidate in build_episode_slice_candidates(slice_rows):
            evidence = _evidence_from_candidate(candidate, "semantic")
            records.append((candidate, evidence, text_by_id.get(candidate.item_id, candidate.content_summary)))
    return records
```

- [ ] **Step 6: Wire semantic lane into entrypoint**

Modify imports in `backend/memory/recall_stage3.py`:

```python
from memory.recall_stage3_lanes import LexicalLane, SemanticLane, SymbolicLane
```

After the lexical lane block, add:

```python
    if config.semantic.enabled:
        telemetry.lanes_attempted.append("semantic")
        semantic_result = SemanticLane().run(
            envelope,
            profile,
            slices,
            config,
            embedding_provider,
        )
        lane_results.append(semantic_result)
        telemetry.candidates_by_lane["semantic"] = len(semantic_result.candidates)
        if not semantic_result.error:
            telemetry.lanes_succeeded.append("semantic")
        else:
            telemetry.lane_errors["semantic"] = semantic_result.error
```

- [ ] **Step 7: Verify semantic tests pass**

Run:

```bash
cd backend && pytest tests/test_embedding_provider.py tests/test_recall_stage3_semantic.py -v
```

Expected: all tests pass without downloading a real model.

- [ ] **Step 8: Commit**

```bash
git add backend/memory/embedding_provider.py backend/memory/recall_stage3_lanes.py backend/memory/recall_stage3.py backend/tests/test_embedding_provider.py backend/tests/test_recall_stage3_semantic.py
git commit -m "feat: add stage3 semantic lane"
```

## Task 8: Integrate Stage 3 into MemoryManager and Telemetry

**Files:**
- Modify: `backend/memory/formatter.py`
- Modify: `backend/memory/manager.py`
- Modify: `backend/tests/test_memory_manager.py`

- [ ] **Step 1: Add manager integration tests**

Append these tests to `backend/tests/test_memory_manager.py`:

```python
@pytest.mark.asyncio
async def test_generate_context_attaches_stage3_telemetry(tmp_path: Path):
    manager = MemoryManager(data_dir=str(tmp_path))
    await manager.v3_store.upsert_profile_item(
        "u1",
        "stable_preferences",
        MemoryProfileItem(
            id="stable_preferences:hotel:preferred_area",
            domain="hotel",
            key="preferred_area",
            value="京都住四条附近",
            polarity="prefer",
            stability="stable",
            confidence=0.9,
            status="active",
            context={},
            applicability="适用于大多数住宿选择。",
            recall_hints={"domains": ["hotel"], "keywords": ["住宿"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )

    _, recall = await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿按我习惯",
        recall_gate=True,
        retrieval_plan=RecallRetrievalPlan(
            source="profile",
            buckets=["stable_preferences"],
            domains=["hotel"],
            destination="",
            keywords=["住宿"],
            top_k=5,
            reason="test",
        ),
    )

    assert recall.stage3["lanes_attempted"] == ["symbolic"]
    assert recall.stage3["zero_hit"] is False


@pytest.mark.asyncio
async def test_generate_context_passes_active_plan_to_reranker_when_plan_is_heuristic(
    tmp_path: Path,
    monkeypatch,
):
    manager = MemoryManager(data_dir=str(tmp_path))
    seen = {}

    async def fake_select_recall_candidates(**kwargs):
        seen["retrieval_plan"] = kwargs["retrieval_plan"]
        return kwargs["candidates"], RecallRerankResult(
            selected_item_ids=[candidate.item_id for candidate in kwargs["candidates"]],
            final_reason="fake",
            per_item_reason={},
            fallback_used="none",
        )

    monkeypatch.setattr("memory.manager.select_recall_candidates", fake_select_recall_candidates)
    await manager.v3_store.upsert_profile_item(
        "u1",
        "stable_preferences",
        MemoryProfileItem(
            id="stable_preferences:hotel:preferred_area",
            domain="hotel",
            key="preferred_area",
            value="京都住四条附近",
            polarity="prefer",
            stability="stable",
            confidence=0.9,
            status="active",
            context={},
            applicability="适用于大多数住宿选择。",
            recall_hints={"domains": ["hotel"], "keywords": ["住宿"]},
            source_refs=[],
            created_at="2026-04-19T00:00:00",
            updated_at="2026-04-19T00:00:00",
        ),
    )

    await manager.generate_context(
        "u1",
        TravelPlanState(session_id="s1", trip_id="trip_now"),
        user_message="住宿按我常规偏好来",
        recall_gate=True,
        short_circuit="force_recall",
        retrieval_plan=None,
    )

    assert seen["retrieval_plan"] is not None
    assert seen["retrieval_plan"].source == "profile"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_memory_manager.py::test_generate_context_attaches_stage3_telemetry tests/test_memory_manager.py::test_generate_context_passes_active_plan_to_reranker_when_plan_is_heuristic -v
```

Expected: fails because telemetry has no `stage3` field and manager still uses direct symbolic recall.

- [ ] **Step 3: Add Stage 3 telemetry field**

In `backend/memory/formatter.py`, add to `MemoryRecallTelemetry`:

```python
    stage3: dict[str, Any] = field(default_factory=dict)
```

In `to_dict()`, add:

```python
            "stage3": dict(self.stage3),
```

- [ ] **Step 4: Integrate Stage 3 in manager imports**

In `backend/memory/manager.py`, change imports:

```python
from memory.embedding_provider import CachedEmbeddingProvider, FastEmbedProvider, NullEmbeddingProvider
from memory.recall_stage3 import retrieve_recall_candidates
```

Keep `heuristic_retrieval_plan_from_message` and `should_trigger_memory_recall` from `memory.symbolic_recall`, but remove direct imports of `rank_profile_items` and `rank_episode_slices` after the manager no longer uses them.

- [ ] **Step 5: Add embedding provider factory to MemoryManager**

Inside `MemoryManager.__init__()`, add:

```python
        self._embedding_provider = None
```

Add this method to `MemoryManager`:

```python
    def _get_stage3_embedding_provider(self):
        semantic_config = self.retrieval_config.stage3.semantic
        if not semantic_config.enabled:
            return None
        if self._embedding_provider is not None:
            return self._embedding_provider
        if semantic_config.provider != "fastembed":
            self._embedding_provider = NullEmbeddingProvider()
            return self._embedding_provider
        try:
            self._embedding_provider = CachedEmbeddingProvider(
                FastEmbedProvider(
                    model_name=semantic_config.model_name,
                    cache_dir=semantic_config.cache_dir,
                    local_files_only=semantic_config.local_files_only,
                ),
                max_items=semantic_config.cache_max_items,
            )
        except Exception:
            self._embedding_provider = NullEmbeddingProvider()
        return self._embedding_provider
```

- [ ] **Step 6: Replace direct Stage 3 recall block**

In `MemoryManager.generate_context()`, replace the current `if should_run_query_recall and active_plan is not None:` block with:

```python
        stage3_result = None
        if should_run_query_recall and active_plan is not None:
            should_load_slices = (
                active_plan.source in {"episode_slice", "hybrid_history"}
                or self.retrieval_config.stage3.source_widening.enabled
            )
            candidate_slices = []
            if should_load_slices:
                destination_filter = (
                    active_plan.destination
                    if not self.retrieval_config.stage3.destination_normalization_enabled
                    else None
                )
                candidate_slices = await self.v3_store.list_episode_slices(
                    user_id,
                    destination=destination_filter or None,
                )
            stage3_result = retrieve_recall_candidates(
                query=active_plan,
                profile=profile,
                slices=candidate_slices,
                user_message=user_message,
                plan=plan,
                config=self.retrieval_config.stage3,
                embedding_provider=self._get_stage3_embedding_provider(),
            )
            recall_candidates.extend(stage3_result.candidates[: active_plan.top_k * 2])
```

This keeps Stage 3’s engineering pool separate from Stage 4’s final selection while avoiding an unbounded candidate list.

- [ ] **Step 7: Pass active plan to Stage 4 and attach telemetry**

In the reranker call, change:

```python
                retrieval_plan=retrieval_plan,
```

to:

```python
                retrieval_plan=active_plan,
```

After `telemetry.recall_attempted_but_zero_hit = ...`, add:

```python
        if stage3_result is not None:
            telemetry.stage3 = stage3_result.telemetry.to_dict()
```

- [ ] **Step 8: Verify manager tests pass**

Run:

```bash
cd backend && pytest tests/test_memory_manager.py -v
```

Expected: all memory manager tests pass.

- [ ] **Step 9: Commit**

```bash
git add backend/memory/formatter.py backend/memory/manager.py backend/tests/test_memory_manager.py
git commit -m "feat: route memory recall through stage3"
```

## Task 9: End-to-End Verification and Behavior Guard

**Files:**
- Modify: `backend/tests/test_recall_stage3_symbolic.py`

- [ ] **Step 1: Add default behavior guard**

Append this test to `backend/tests/test_recall_stage3_symbolic.py`:

```python
def test_stage3_default_config_keeps_only_symbolic_lane_enabled():
    config = Stage3RecallConfig()

    assert config.symbolic.enabled is True
    assert config.lexical.enabled is False
    assert config.semantic.enabled is False
    assert config.entity.enabled is False
    assert config.temporal.enabled is False
```

- [ ] **Step 2: Run focused Stage 3 tests**

Run:

```bash
cd backend && pytest \
  tests/test_stage3_config.py \
  tests/test_destination_normalization.py \
  tests/test_recall_stage3_normalizer.py \
  tests/test_recall_stage3_fusion.py \
  tests/test_recall_stage3_symbolic.py \
  tests/test_recall_stage3_lexical.py \
  tests/test_embedding_provider.py \
  tests/test_recall_stage3_semantic.py \
  -v
```

Expected: all focused Stage 3 tests pass.

- [ ] **Step 3: Run memory and reranker regression tests**

Run:

```bash
cd backend && pytest tests/test_memory_manager.py tests/test_symbolic_recall.py tests/test_recall_reranker.py -v
```

Expected: all tests pass. Existing `test_symbolic_recall.py` remains valid because compatibility wrappers in `symbolic_recall.py` are untouched.

- [ ] **Step 4: Run full backend test suite**

Run:

```bash
cd backend && pytest -q
```

Expected: all tests pass.

- [ ] **Step 5: Verify embedding runtime was already prepared**

Run:

```bash
./scripts/verify-stage3-embedding-runtime.py --local-files-only
```

Expected: exits `0`, prints `dim=512`, and does not download model files. If this fails, fix environment/cache setup before changing Stage 3 logic.

- [ ] **Step 6: Commit verification guard if changed**

```bash
git add backend/tests/test_recall_stage3_symbolic.py
git commit -m "test: guard stage3 default behavior"
```

## Task 10: Documentation and Project Overview

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: Update architecture overview**

In `PROJECT_OVERVIEW.md`, update the memory recall section to describe:

```markdown
Stage 3 memory recall now routes through `memory.recall_stage3.retrieve_recall_candidates()`.
The default production behavior enables only the symbolic lane, preserving the previous
`symbolic_recall.py` behavior. Lexical and semantic lanes are present behind
`memory.retrieval.stage3` feature flags. The default semantic runtime is FastEmbed with
`BAAI/bge-small-zh-v1.5`, using ONNX Runtime CPU and a runtime cache under
`backend/data/embedding_cache`.
```

- [ ] **Step 2: Verify docs mention default-disabled lanes**

Run:

```bash
rg "recall_stage3|BAAI/bge-small-zh-v1.5|symbolic lane" PROJECT_OVERVIEW.md
```

Expected: output contains all three concepts.

- [ ] **Step 3: Commit docs**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: document stage3 hybrid recall"
```

## Self-Review Checklist

- [ ] The plan preserves Stage 3 default behavior by enabling only the symbolic lane.
- [ ] Destination normalization is a feature-flagged evidence source, not a default hard filter.
- [ ] Semantic recall uses runtime cache and fake-provider unit tests, avoiding model downloads during tests.
- [ ] `RecallCandidate` is not polluted with embedding vectors or Stage 3-only fields.
- [ ] Stage 4 receives `active_plan`, fixing the current heuristic-plan handoff issue.
- [ ] Source widening remains disabled and is not treated as production behavior in this first implementation.
- [ ] Persistent vector store and write-time embedding persistence are explicitly out of scope.
