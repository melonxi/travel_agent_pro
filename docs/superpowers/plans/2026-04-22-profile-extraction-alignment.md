# Profile Extraction Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align profile-memory extraction with the existing recall pipeline by requiring recall-ready metadata, normalizing profile items before save, and upgrading repeated hypotheses into stable preferences.

**Architecture:** Keep the current recall gate, retrieval-plan, and symbolic-recall flow unchanged. Strengthen only the extraction/save side: expand the profile extraction contract, add a deterministic normalization module, and merge normalized observations with existing profile evidence before classification and persistence.

**Tech Stack:** Python 3.12, FastAPI backend, pytest, pytest-asyncio, existing memory v3 models/store/policy pipeline.

---

## File Structure

- Create: `backend/memory/profile_normalization.py`
  - Canonical domain/key normalization
  - Recall-hint cleanup and default aliases
  - Applicability/source-ref defaults
  - Cross-turn merge/upgrade helper
- Modify: `backend/memory/extraction.py`
  - Require recall-ready fields in profile extraction schema
  - Update profile extraction prompt text
- Modify: `backend/main.py`
  - Normalize profile items before classification/save
  - Merge incoming profile observations with existing profile items
- Modify: `backend/tests/test_memory_extraction.py`
  - Cover new schema fields and prompt contract
- Create: `backend/tests/test_profile_normalization.py`
  - Unit tests for normalization and merge/upgrade behavior
- Modify: `backend/tests/test_memory_integration.py`
  - Integration coverage for enriched profile writes and hypothesis promotion
- Modify: `PROJECT_OVERVIEW.md`
  - Reflect normalized profile extraction and evidence-based upgrade in current architecture

---

### Task 1: Strengthen Profile Extraction Contract

**Files:**
- Modify: `backend/memory/extraction.py`
- Test: `backend/tests/test_memory_extraction.py`

- [ ] **Step 1: Write the failing schema/prompt tests**

Add these tests to `backend/tests/test_memory_extraction.py`:

```python
def test_profile_item_schema_requires_recall_ready_fields():
    tool = build_v3_profile_extraction_tool()
    profile_item = tool["parameters"]["properties"]["profile_updates"]["properties"][
        "stable_preferences"
    ]["items"]

    assert "applicability" in profile_item["properties"]
    assert "recall_hints" in profile_item["properties"]
    assert "source_refs" in profile_item["properties"]
    assert "applicability" in profile_item["required"]
    assert "recall_hints" in profile_item["required"]
    assert "source_refs" in profile_item["required"]


def test_profile_prompt_requires_recall_ready_metadata():
    prompt = build_v3_profile_extraction_prompt(
        user_messages=["我以后都不坐红眼航班"],
        profile=UserMemoryProfile.empty("u1"),
        plan_facts={"destination": "京都"},
    )

    assert "applicability" in prompt
    assert "recall_hints" in prompt
    assert "source_refs" in prompt
    assert "keywords" in prompt
    assert "aliases" in prompt
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py -k "recall_ready_fields or recall_ready_metadata" -v
```

Expected: failures because the schema and prompt do not yet require those fields.

- [ ] **Step 3: Expand the profile item schema**

In `backend/memory/extraction.py`, update `_build_profile_item_schema()` so the item properties also include:

```python
            "applicability": {
                "type": "string",
                "description": "该画像适用于什么范围，例如“适用于大多数旅行”。",
            },
            "recall_hints": {
                "type": "object",
                "properties": {
                    "domains": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["domains", "keywords", "aliases"],
                "additionalProperties": False,
            },
            "source_refs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "session_id": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                    "required": ["kind", "session_id", "quote"],
                    "additionalProperties": False,
                },
            },
```

Add these names into the `required` list:

```python
            "applicability",
            "recall_hints",
            "source_refs",
```

- [ ] **Step 4: Tighten the profile extraction prompt**

In `build_v3_profile_extraction_prompt()`, extend the hard requirements section with:

```python
    - 每条 profile item 都必须提供 `applicability`，用一句简洁中文说明适用范围，不要写成命令。
    - 每条 profile item 都必须提供 `recall_hints.domains`、`recall_hints.keywords`、`recall_hints.aliases`；其中 keywords 要尽量贴近用户原话，aliases 提供常见同义表达。
    - 每条 profile item 都必须提供 `source_refs`，至少保留一条当前轮消息引用，包含 `kind`、`session_id`、`quote`。
    - `source_refs.quote` 只保留必要短句，不要带敏感信息。
```

- [ ] **Step 5: Run the focused tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py -k "recall_ready_fields or recall_ready_metadata" -v
```

Expected: both tests pass.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add backend/memory/extraction.py backend/tests/test_memory_extraction.py
git commit -m "feat: require recall-ready profile extraction fields"
```

---

### Task 2: Add Deterministic Profile Normalization

**Files:**
- Create: `backend/memory/profile_normalization.py`
- Create: `backend/tests/test_profile_normalization.py`

- [ ] **Step 1: Write the failing normalization tests**

Create `backend/tests/test_profile_normalization.py` with:

```python
from memory.profile_normalization import normalize_profile_item
from memory.v3_models import MemoryProfileItem


def _item(**overrides):
    base = dict(
        id="",
        domain="food",
        key="dislike_spicy_food",
        value="不吃辣",
        polarity="avoid",
        stability="explicit_declared",
        confidence=0.9,
        status="active",
        context={},
        applicability="",
        recall_hints={"domains": [], "keywords": ["不吃辣"], "aliases": []},
        source_refs=[{"kind": "message", "session_id": "s1", "quote": "我不吃辣"}],
        created_at="",
        updated_at="",
    )
    base.update(overrides)
    return MemoryProfileItem(**base)


def test_normalize_profile_item_canonicalizes_key_and_hints():
    item = normalize_profile_item("stable_preferences", _item())

    assert item.key == "avoid_spicy"
    assert "food" in item.recall_hints["domains"]
    assert "不能吃辣" in item.recall_hints["aliases"]
    assert item.applicability


def test_normalize_profile_item_keeps_existing_applicability_when_present():
    item = normalize_profile_item(
        "constraints",
        _item(key="avoid_red_eye", domain="flight", applicability="适用于所有旅行。"),
    )

    assert item.applicability == "适用于所有旅行。"
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_profile_normalization.py -v
```

Expected: import error because `memory.profile_normalization` does not exist yet.

- [ ] **Step 3: Implement normalization module**

Create `backend/memory/profile_normalization.py` with:

```python
from __future__ import annotations

from dataclasses import replace

from memory.v3_models import MemoryProfileItem


_CANONICAL_KEYS = {
    ("food", "dislike_spicy_food"): "avoid_spicy",
    ("food", "no_spicy"): "avoid_spicy",
    ("food", "avoid_spicy"): "avoid_spicy",
    ("flight", "avoid_red_eye"): "avoid_red_eye",
}

_DEFAULT_ALIASES = {
    ("food", "avoid_spicy"): ["不吃辣", "不能吃辣", "避开辣味"],
    ("flight", "avoid_red_eye"): ["红眼航班", "夜间航班"],
}

_DEFAULT_APPLICABILITY = {
    "constraints": "适用于所有旅行，除非用户明确临时允许。",
    "rejections": "适用于同类决策，除非用户明确改变主意。",
    "stable_preferences": "适用于大多数旅行。",
    "preference_hypotheses": "仅作为暂时偏好假设，需更多观察确认。",
}


def normalize_profile_item(bucket: str, item: MemoryProfileItem) -> MemoryProfileItem:
    canonical_key = _CANONICAL_KEYS.get((item.domain, item.key), item.key)
    hints = item.recall_hints if isinstance(item.recall_hints, dict) else {}
    domains = _dedupe([item.domain, *hints.get("domains", [])])
    keywords = _dedupe(list(hints.get("keywords", [])))
    aliases = _dedupe(
        [*hints.get("aliases", []), *_DEFAULT_ALIASES.get((item.domain, canonical_key), [])]
    )
    applicability = item.applicability.strip() or _DEFAULT_APPLICABILITY.get(bucket, "适用于当前已知相似旅行。")
    return replace(
        item,
        key=canonical_key,
        applicability=applicability,
        recall_hints={
            "domains": domains,
            "keywords": [value for value in keywords if value],
            "aliases": [value for value in aliases if value],
        },
    )


def _dedupe(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped
```

- [ ] **Step 4: Run the normalization tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_profile_normalization.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add backend/memory/profile_normalization.py backend/tests/test_profile_normalization.py
git commit -m "feat: normalize profile memory items before save"
```

---

### Task 3: Merge Incoming Evidence And Promote Repeated Hypotheses

**Files:**
- Modify: `backend/memory/profile_normalization.py`
- Modify: `backend/main.py`
- Test: `backend/tests/test_profile_normalization.py`

- [ ] **Step 1: Write the failing merge/upgrade tests**

Append to `backend/tests/test_profile_normalization.py`:

```python
from memory.profile_normalization import merge_profile_item_with_existing


def test_merge_profile_item_promotes_repeated_hypothesis_to_stable():
    existing = _item(
        id="preference_hypotheses:food:avoid_spicy:{}",
        key="avoid_spicy",
        stability="soft_constraint",
        context={"observation_count": 1},
        applicability="仅作为暂时偏好假设，需更多观察确认。",
    )
    incoming = _item(
        key="avoid_spicy",
        stability="soft_constraint",
        context={},
    )

    merged_bucket, merged = merge_profile_item_with_existing(
        bucket="preference_hypotheses",
        incoming=incoming,
        existing_items=[existing],
    )

    assert merged_bucket == "stable_preferences"
    assert merged.context["observation_count"] == 2
    assert merged.stability == "pattern_observed"


def test_merge_profile_item_keeps_single_observation_as_hypothesis():
    incoming = _item(key="prefer_quiet_room", value=True, polarity="prefer", confidence=0.7)

    merged_bucket, merged = merge_profile_item_with_existing(
        bucket="preference_hypotheses",
        incoming=incoming,
        existing_items=[],
    )

    assert merged_bucket == "preference_hypotheses"
    assert merged.context["observation_count"] == 1
```

- [ ] **Step 2: Run the focused tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_profile_normalization.py -k "promotes_repeated_hypothesis or single_observation" -v
```

Expected: failures because `merge_profile_item_with_existing` does not exist.

- [ ] **Step 3: Implement merge/upgrade helper**

In `backend/memory/profile_normalization.py`, add:

```python
def merge_profile_item_with_existing(
    *,
    bucket: str,
    incoming: MemoryProfileItem,
    existing_items: list[MemoryProfileItem],
) -> tuple[str, MemoryProfileItem]:
    matching = next(
        (item for item in existing_items if item.domain == incoming.domain and item.key == incoming.key),
        None,
    )
    if matching is None:
        context = dict(incoming.context)
        context["observation_count"] = max(int(context.get("observation_count", 0) or 0), 1)
        return bucket, replace(incoming, context=context)

    observation_count = max(
        int(dict(matching.context).get("observation_count", 1) or 1) + 1,
        2,
    )
    merged_context = dict(incoming.context)
    merged_context["observation_count"] = observation_count
    merged_source_refs = _merge_source_refs(matching.source_refs, incoming.source_refs)
    promoted_bucket = bucket
    promoted_stability = incoming.stability
    if bucket == "preference_hypotheses" and observation_count >= 2:
        promoted_bucket = "stable_preferences"
        promoted_stability = "pattern_observed"
    merged = replace(
        incoming,
        stability=promoted_stability,
        confidence=max(matching.confidence, incoming.confidence),
        context=merged_context,
        source_refs=merged_source_refs,
    )
    return promoted_bucket, merged


def _merge_source_refs(left: list[dict], right: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in [*left, *right]:
        if not isinstance(ref, dict):
            continue
        key = (
            str(ref.get("kind", "")),
            str(ref.get("session_id", "")),
            str(ref.get("quote", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "kind": key[0],
                "session_id": key[1],
                "quote": key[2],
            }
        )
    return merged
```

- [ ] **Step 4: Run the merge tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_profile_normalization.py -v
```

Expected: all normalization tests pass.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add backend/memory/profile_normalization.py backend/tests/test_profile_normalization.py
git commit -m "feat: promote repeated profile hypotheses"
```

---

### Task 4: Wire Normalization And Upgrade Into Profile Save Flow

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Write the failing integration tests**

Add these tests to `backend/tests/test_memory_integration.py` near the existing profile extraction tests:

```python
@pytest.mark.asyncio
async def test_profile_extraction_writes_recall_ready_metadata(app):
    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_recall":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_recall_gate",
                        name=tool_name,
                        arguments={
                            "needs_recall": False,
                            "intent_type": "no_recall_needed",
                            "reason": "current_preference_statement",
                            "confidence": 0.9,
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": False},
                            "reason": "profile_memory_signal",
                            "message": "检测到长期偏好",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_profile",
                    name="extract_profile_memory",
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [
                                {
                                    "domain": "food",
                                    "key": "dislike_spicy_food",
                                    "value": "不吃辣",
                                    "polarity": "avoid",
                                    "stability": "explicit_declared",
                                    "confidence": 0.95,
                                    "reason": "明确长期饮食偏好",
                                    "evidence": "我不吃辣",
                                    "applicability": "",
                                    "recall_hints": {"domains": [], "keywords": ["不吃辣"], "aliases": []},
                                    "source_refs": [{"kind": "message", "session_id": "s1", "quote": "我不吃辣"}],
                                }
                            ],
                            "preference_hypotheses": [],
                        },
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)
```

Then assert after the request:

```python
    item = profile.json()["stable_preferences"][0]
    assert item["key"] == "avoid_spicy"
    assert item["applicability"]
    assert item["recall_hints"]["aliases"]
    assert item["source_refs"][0]["quote"] == "我不吃辣"
```

Add a second integration test for repeated hypothesis promotion:

```python
@pytest.mark.asyncio
async def test_profile_extraction_promotes_repeated_hypothesis(app):
    ...
    assert profile.json()["stable_preferences"][0]["key"] == "prefer_quiet_room"
    assert profile.json()["preference_hypotheses"] == []
```

- [ ] **Step 2: Run the focused integration tests to verify RED**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "recall_ready_metadata or promotes_repeated_hypothesis" -v
```

Expected: failures because the save flow does not yet normalize keys or promote hypotheses.

- [ ] **Step 3: Integrate normalization and merge into `_save_profile_updates()`**

In `backend/main.py`, add imports:

```python
from memory.profile_normalization import (
    merge_profile_item_with_existing,
    normalize_profile_item,
)
```

Inside `_save_profile_updates()`, replace the inner save loop with:

```python
        existing_profile = await memory_mgr.v3_store.load_profile(user_id)
        existing_by_bucket = {
            "constraints": list(existing_profile.constraints),
            "rejections": list(existing_profile.rejections),
            "stable_preferences": list(existing_profile.stable_preferences),
            "preference_hypotheses": list(existing_profile.preference_hypotheses),
        }
        for bucket, items in buckets:
            for raw_item in items:
                normalized = normalize_profile_item(bucket, raw_item)
                target_bucket, merged_item = merge_profile_item_with_existing(
                    bucket=bucket,
                    incoming=normalized,
                    existing_items=existing_by_bucket.get(bucket, []) + existing_by_bucket.get("stable_preferences", []),
                )
                action = policy.classify_v3_profile_item(target_bucket, merged_item)
                if action == "drop":
                    continue
                sanitized = policy.sanitize_v3_profile_item(merged_item)
                sanitized.status = action
                sanitized.updated_at = now
                if not sanitized.created_at:
                    sanitized.created_at = now
                sanitized.id = generate_profile_item_id(target_bucket, sanitized)
                await memory_mgr.v3_store.upsert_profile_item(user_id, target_bucket, sanitized)
                existing_by_bucket.setdefault(target_bucket, []).append(sanitized)
```

- [ ] **Step 4: Run the focused integration tests to verify GREEN**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "recall_ready_metadata or promotes_repeated_hypothesis" -v
```

Expected: those new tests pass.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "feat: normalize and merge profile extraction before save"
```

---

### Task 5: Full Verification And Documentation Sync

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Test: `backend/tests/test_memory_extraction.py`
- Test: `backend/tests/test_profile_normalization.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Update project overview**

In `PROJECT_OVERVIEW.md`, extend the memory-system row so it explicitly states:

```markdown
profile extraction now writes recall-ready metadata (`applicability`, `recall_hints`, `source_refs`), normalizes high-value profile domains/keys before persistence, and upgrades repeated preference hypotheses into stable preferences using existing-profile evidence.
```

- [ ] **Step 2: Run the targeted verification suite**

Run:

```bash
cd backend && pytest \
  tests/test_memory_extraction.py \
  tests/test_profile_normalization.py \
  tests/test_memory_integration.py -q
```

Expected: all tests pass with exit code 0.

- [ ] **Step 3: Run one wider regression slice**

Run:

```bash
cd backend && pytest \
  tests/test_symbolic_recall.py \
  tests/test_memory_manager.py \
  tests/test_memory_policy.py -q
```

Expected: all tests pass with exit code 0.

- [ ] **Step 4: Commit Task 5**

Run:

```bash
git add PROJECT_OVERVIEW.md \
  backend/tests/test_memory_extraction.py \
  backend/tests/test_profile_normalization.py \
  backend/tests/test_memory_integration.py \
  backend/main.py \
  backend/memory/extraction.py \
  backend/memory/profile_normalization.py
git commit -m "feat: align profile extraction with recall pipeline"
```

