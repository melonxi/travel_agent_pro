# Phase 3 Candidate POI Uniqueness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent duplicate POI ownership inside a single skeleton by making `locked_pois` and `candidate_pois` globally unique across all days during `set_skeleton_plans` writes.

**Architecture:** Tighten the skeleton prompt so the model treats `candidate_pois` as a single-day-owned pool, then enforce the same rule in `set_skeleton_plans` validation so duplicate POIs are rejected before Phase 5 parallel workers ever see them. Keep Phase 5 runtime behavior unchanged; this is a Phase 3 boundary hardening change only.

**Tech Stack:** Python 3.12+, pytest, prompt skill-card architecture, Phase 3 plan tools

---

### Task 1: Tighten Skeleton Prompt Contract

**Files:**
- Modify: `backend/phase/prompts.py`
- Test: `backend/tests/test_prompt_architecture.py`

- [ ] **Step 1: Write the failing prompt-architecture tests**

Add these tests to `backend/tests/test_prompt_architecture.py` inside `TestPhase3Split`:

```python
    def test_skeleton_marks_candidate_pois_as_single_day_owned(self):
        prompt = PHASE3_STEP_PROMPTS["skeleton"]
        assert "单天专属候选池" in prompt

    def test_skeleton_requires_global_uniqueness_across_locked_and_candidate(self):
        prompt = PHASE3_STEP_PROMPTS["skeleton"]
        assert "locked_pois" in prompt
        assert "candidate_pois" in prompt
        assert "同一套 skeleton 内" in prompt
        assert "只能出现在一天" in prompt
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_prompt_architecture.py -q
```

Expected: FAIL with missing assertions because the skeleton prompt does not yet describe `candidate_pois` as single-day-owned or globally unique with `locked_pois`.

- [ ] **Step 3: Update the skeleton prompt text**

Edit the `skeleton` entry in `backend/phase/prompts.py` so the structural rules section reads like this:

```python
- `days`：list，每天必须包含：
  - `area_cluster`：当天主区域列表（如 `["浅草", "上野"]`）
  - `theme`：当天主题
  - `locked_pois`：该天独占的强锚点列表（如 `["浅草寺"]`）。每个 POI 只能归属一天，不允许在整套 skeleton 的其他天再次出现在 `locked_pois` 或 `candidate_pois`。
  - `candidate_pois`：该天单天专属的候选 POI 池（如 `["仲见世商店街", "上野公園"]`），不能为空。`candidate_pois` 里的 POI 只能归属这一天，不允许在整套 skeleton 的其他天再次出现在 `locked_pois` 或 `candidate_pois`。
```

Also add one explicit note below the optional fields:

```python
注意：同一套 skeleton 内，一个 POI 只能出现在一天的 `locked_pois` 或 `candidate_pois` 中一次。不要把同一个 POI 当作多天共享候选池；如果只是弱备选，也必须只归属给最合适的一天。
```

And add one minimal legal example:

```python
示例：
- Day 1: `locked_pois=["浅草寺"]`, `candidate_pois=["仲见世商店街"]`
- Day 2: `locked_pois=["东京国立博物馆"]`, `candidate_pois=["上野公園"]`
- 不要让 `上野公園` 同时出现在 Day 1 和 Day 2 的 `candidate_pois`
```

- [ ] **Step 4: Run the prompt tests to verify they pass**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_prompt_architecture.py -q
```

Expected: PASS

- [ ] **Step 5: Commit the prompt-contract change**

```bash
git add backend/phase/prompts.py backend/tests/test_prompt_architecture.py
git commit -m "test: tighten phase3 skeleton prompt contract"
```

### Task 2: Add Failing Schema Tests for Global POI Uniqueness

**Files:**
- Modify: `backend/tests/test_plan_tools/test_skeleton_schema.py`

- [ ] **Step 1: Write the failing uniqueness tests**

Append these tests to `backend/tests/test_plan_tools/test_skeleton_schema.py`:

```python
@pytest.mark.asyncio
async def test_cross_day_candidate_poi_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="上野公園"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["上野公園"],
                },
                {
                    "area_cluster": ["上野"],
                    "locked_pois": ["东京国立博物馆"],
                    "candidate_pois": ["上野公園"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_locked_poi_and_other_day_candidate_poi_conflict_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["仲见世商店街"],
                },
                {
                    "area_cluster": ["上野"],
                    "locked_pois": ["东京国立博物馆"],
                    "candidate_pois": ["浅草寺"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_same_day_locked_and_candidate_conflict_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺"],
                    "candidate_pois": ["浅草寺", "仲见世商店街"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_same_day_candidate_poi_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="仲见世商店街"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": [],
                    "candidate_pois": ["仲见世商店街", "仲见世商店街"],
                },
            ],
        }])


@pytest.mark.asyncio
async def test_same_day_locked_poi_duplicate_raises():
    plan = _make_plan()
    tool = _make_tool(plan)
    with pytest.raises(ToolError, match="浅草寺"):
        await tool(plans=[{
            "id": "plan_a",
            "name": "平衡版",
            "days": [
                {
                    "area_cluster": ["浅草"],
                    "locked_pois": ["浅草寺", "浅草寺"],
                    "candidate_pois": ["仲见世商店街"],
                },
            ],
        }])
```

- [ ] **Step 2: Run the skeleton-schema tests to verify they fail**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_plan_tools/test_skeleton_schema.py -q
```

Expected: FAIL because `_validate_skeleton_days()` only checks cross-day `locked_pois` duplicates today.

- [ ] **Step 3: Commit the failing test additions**

```bash
git add backend/tests/test_plan_tools/test_skeleton_schema.py
git commit -m "test: cover skeleton poi uniqueness conflicts"
```

### Task 3: Enforce Global POI Uniqueness in `set_skeleton_plans`

**Files:**
- Modify: `backend/tools/plan_tools/phase3_tools.py`
- Test: `backend/tests/test_plan_tools/test_skeleton_schema.py`
- Test: `backend/tests/test_prompt_architecture.py`

- [ ] **Step 1: Replace the old `all_locked` logic with a unified ownership map**

Update `_validate_skeleton_days()` in `backend/tools/plan_tools/phase3_tools.py`. Replace the current `all_locked` block with this structure:

```python
        poi_owner: dict[str, str] = {}

        def register_poi(poi: str, location: str) -> None:
            previous = poi_owner.get(poi)
            if previous is not None:
                raise ToolError(
                    f"'{poi}' 已出现在 {previous}，又出现在 {location}；"
                    "同一套 skeleton 内，POI 只能归属一天",
                    error_code="INVALID_VALUE",
                    suggestion=(
                        f"把 '{poi}' 只保留在最适合的一天；"
                        "如果只是弱备选，不要在多天重复写入 candidate_pois"
                    ),
                )
            poi_owner[poi] = location
```

- [ ] **Step 2: Validate both `locked_pois` and `candidate_pois` through the same registration path**

Still inside `_validate_skeleton_days()`, after validating list shapes, iterate through both fields:

```python
            for poi in lp:
                if not isinstance(poi, str):
                    continue
                register_poi(poi, f"{prefix}.locked_pois")

            for poi in cp:
                if not isinstance(poi, str):
                    continue
                register_poi(poi, f"{prefix}.candidate_pois")
```

Delete the old `all_locked` duplicate check entirely so there is only one uniqueness rule.

- [ ] **Step 3: Run the targeted tests**

Run:

```bash
backend/.venv/bin/pytest \
  backend/tests/test_plan_tools/test_skeleton_schema.py \
  backend/tests/test_prompt_architecture.py -q
```

Expected: PASS

- [ ] **Step 4: Run the broader regression slice**

Run:

```bash
backend/.venv/bin/pytest \
  backend/tests/test_plan_tools/test_phase3_tools.py \
  backend/tests/test_plan_tools/test_skeleton_schema.py \
  backend/tests/test_prompt_architecture.py \
  backend/tests/test_orchestrator.py \
  backend/tests/test_worker_prompt.py -q
```

Expected: PASS

- [ ] **Step 5: Commit the validation implementation**

```bash
git add \
  backend/tools/plan_tools/phase3_tools.py \
  backend/tests/test_plan_tools/test_skeleton_schema.py \
  backend/tests/test_prompt_architecture.py
git commit -m "feat: enforce unique poi ownership in skeleton plans"
```

### Task 4: Update Project Overview and Verify Final State

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Test: `docs/superpowers/specs/2026-04-20-phase3-candidate-poi-uniqueness-design.md`
- Test: `docs/superpowers/plans/2026-04-20-phase3-candidate-poi-uniqueness.md`

- [ ] **Step 1: Update the overview to reflect the shipped validation rule**

Adjust the Phase 3 plan-tools summary in `PROJECT_OVERVIEW.md` so it no longer says only “校验跨天 locked_pois 唯一性”, but instead says the skeleton writer now enforces global POI uniqueness across `locked_pois` and `candidate_pois` for new writes.

Use wording like:

```markdown
`tools.plan_tools.phase3_tools`（Phase 3 强 schema 写工具工厂；对骨架 id/name 做规范化，days 每天强制 `area_cluster`/`locked_pois`/`candidate_pois` 三必填字段，并在 `set_skeleton_plans` 新写入时校验 `locked_pois` + `candidate_pois` 的全局 POI 唯一性，拒绝空 days 数组，并兼容 legacy 选择态、冲突检测与歧义回退）
```

- [ ] **Step 2: Verify the worktree is clean except for intended plan/spec/docs artifacts**

Run:

```bash
git status --short
```

Expected: only the files changed in this task should appear before commit.

- [ ] **Step 3: Commit the overview update**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update overview for skeleton poi uniqueness"
```

- [ ] **Step 4: Verify the final branch state**

Run:

```bash
git log --oneline --decorate -5
backend/.venv/bin/pytest \
  backend/tests/test_plan_tools/test_phase3_tools.py \
  backend/tests/test_plan_tools/test_skeleton_schema.py \
  backend/tests/test_prompt_architecture.py \
  backend/tests/test_orchestrator.py \
  backend/tests/test_worker_prompt.py -q
```

Expected:

- Recent commits include the three implementation commits above
- All targeted tests pass
