# Memory Extraction Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split v3 memory extraction into a routing gate plus dedicated profile and working-memory extractors.

**Architecture:** Keep the existing async memory scheduler and public `_extract_memory_candidates()` entry point. Change the gate result from a boolean to explicit routes, then orchestrate `extract_profile_memory` and `extract_working_memory` as separate forced tool calls. Profile writes continue through `MemoryPolicy.classify_v3_profile_item()`; working-memory writes continue through `MemoryPolicy.sanitize_working_memory_item()`.

**Tech Stack:** Python 3.12, FastAPI, pytest, pytest-asyncio, existing LLM provider abstraction and forced tool-call helpers.

---

## File Structure

- Modify `backend/memory/extraction.py`
  - Add route dataclass.
  - Add profile-only and working-memory-only tool builders.
  - Add profile-only and working-memory-only prompts.
  - Add parsers for the two new tool argument shapes.
  - Keep combined parser only for compatibility tests.
- Modify `backend/main.py`
  - Interpret gate routes.
  - Replace combined extraction internals with route-aware orchestration.
  - Publish separate `profile_memory_extraction` and `working_memory_extraction` background tasks.
  - Preserve `app.state.extract_memory_candidates` for existing tests and debug hooks.
- Modify `backend/tests/test_memory_extraction.py`
  - Cover route parsing, new tool schemas, and prompt boundaries.
- Modify `backend/tests/test_memory_integration.py`
  - Update forced-tool-call expectations.
  - Cover profile-only, working-only, both, pure trip-state skip, and partial failure semantics.
- Modify `PROJECT_OVERVIEW.md`
  - After implementation, update the current architecture description from combined extraction to routing extraction.

---

### Task 1: Route-Aware Gate Types And Schema

**Files:**
- Modify: `backend/memory/extraction.py`
- Test: `backend/tests/test_memory_extraction.py`

- [ ] **Step 1: Write failing tests for gate routes**

Append these tests to `TestV3ExtractionGate` in `backend/tests/test_memory_extraction.py`:

```python
    def test_gate_tool_requires_routes(self):
        tool = build_v3_extraction_gate_tool()

        assert tool["name"] == "decide_memory_extraction"
        assert tool["parameters"]["required"] == [
            "should_extract",
            "routes",
            "reason",
            "message",
        ]
        routes = tool["parameters"]["properties"]["routes"]
        assert routes["required"] == ["profile", "working_memory"]
        assert routes["properties"]["profile"]["type"] == "boolean"
        assert routes["properties"]["working_memory"]["type"] == "boolean"

    def test_parse_gate_tool_arguments_reads_routes(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": True,
                "routes": {"profile": True, "working_memory": False},
                "reason": "explicit_long_term_constraint",
                "message": "检测到长期旅行约束",
            }
        )

        assert result.should_extract is True
        assert result.routes.profile is True
        assert result.routes.working_memory is False
        assert result.reason == "explicit_long_term_constraint"

    def test_parse_gate_tool_arguments_supports_legacy_boolean(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": True,
                "reason": "explicit_preference_signal",
                "message": "检测到可复用偏好信号",
            }
        )

        assert result.should_extract is True
        assert result.routes.profile is True
        assert result.routes.working_memory is True

    def test_parse_gate_tool_arguments_false_clears_routes(self):
        result = parse_v3_extraction_gate_tool_arguments(
            {
                "should_extract": False,
                "routes": {"profile": True, "working_memory": True},
                "reason": "trip_state_only",
                "message": "本轮只是当前行程事实",
            }
        )

        assert result.should_extract is False
        assert result.routes.profile is False
        assert result.routes.working_memory is False
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py::TestV3ExtractionGate -v
```

Expected: failures mentioning missing `routes` or missing `V3ExtractionGateResult.routes`.

- [ ] **Step 3: Implement route dataclass and parser**

In `backend/memory/extraction.py`, replace the current `V3ExtractionGateResult` block with:

```python
@dataclass
class V3ExtractionRoutes:
    profile: bool = False
    working_memory: bool = False

    @property
    def any(self) -> bool:
        return self.profile or self.working_memory


@dataclass
class V3ExtractionGateResult:
    should_extract: bool = False
    routes: V3ExtractionRoutes = field(default_factory=V3ExtractionRoutes)
    reason: str = ""
    message: str = ""
```

Update `build_v3_extraction_gate_tool()` so the schema includes `routes`:

```python
                "routes": {
                    "type": "object",
                    "properties": {
                        "profile": {
                            "type": "boolean",
                            "description": "是否需要执行长期画像 profile extraction。",
                        },
                        "working_memory": {
                            "type": "boolean",
                            "description": "是否需要执行当前会话 working memory extraction。",
                        },
                    },
                    "required": ["profile", "working_memory"],
                    "additionalProperties": False,
                },
```

Change the required list to:

```python
            "required": ["should_extract", "routes", "reason", "message"],
```

Replace `parse_v3_extraction_gate_tool_arguments()` with:

```python
def parse_v3_extraction_gate_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionGateResult:
    if not isinstance(arguments, dict):
        return V3ExtractionGateResult()

    legacy_should_extract = bool(arguments.get("should_extract", False))
    routes_raw = arguments.get("routes")
    if isinstance(routes_raw, dict):
        routes = V3ExtractionRoutes(
            profile=bool(routes_raw.get("profile", False)),
            working_memory=bool(routes_raw.get("working_memory", False)),
        )
        if not legacy_should_extract:
            routes = V3ExtractionRoutes()
    else:
        routes = V3ExtractionRoutes(
            profile=legacy_should_extract,
            working_memory=legacy_should_extract,
        )

    reason = str(arguments.get("reason", "") or "").strip()
    message = str(arguments.get("message", "") or "").strip()
    return V3ExtractionGateResult(
        should_extract=routes.any,
        routes=routes,
        reason=reason,
        message=message,
    )
```

- [ ] **Step 4: Update gate prompt text**

In `build_v3_extraction_gate_prompt()`, replace the tool return instructions with:

```python
    请调用工具 `{_V3_EXTRACTION_GATE_TOOL_NAME}` 返回：
    - `should_extract`: 只要 profile 或 working_memory 任一路由为 true，就返回 true
    - `routes.profile`: 是否需要提取跨旅行长期用户画像
    - `routes.working_memory`: 是否需要提取当前 session/trip 的短期工作记忆
    - `reason`: 稳定英文标识
    - `message`: 给前端看的简短中文说明
```

Replace the examples with:

```python
    示例：
    - “我以后都不坐红眼航班” -> `routes.profile=true, routes.working_memory=false`
    - “这轮先别考虑迪士尼” -> `routes.profile=false, routes.working_memory=true`
    - “我不吃辣，这轮先别考虑迪士尼” -> 两个 route 都为 true
    - “这次五一去京都，预算 3 万” -> 两个 route 都为 false
```

- [ ] **Step 5: Run gate tests**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py::TestV3ExtractionGate -v
```

Expected: all `TestV3ExtractionGate` tests pass.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add backend/memory/extraction.py backend/tests/test_memory_extraction.py
git commit -m "feat: route memory extraction gate"
```

---

### Task 2: Profile And Working-Memory Tool Builders

**Files:**
- Modify: `backend/memory/extraction.py`
- Test: `backend/tests/test_memory_extraction.py`

- [ ] **Step 1: Write failing tests for new tool schemas**

Append this test class to `backend/tests/test_memory_extraction.py`:

```python
class TestSplitMemoryExtractionTools:
    def test_profile_tool_outputs_only_profile_updates(self):
        tool = build_v3_profile_extraction_tool()

        assert tool["name"] == "extract_profile_memory"
        properties = tool["parameters"]["properties"]
        assert list(properties.keys()) == ["profile_updates"]
        assert tool["parameters"]["required"] == ["profile_updates"]
        assert "working_memory" not in properties

    def test_working_memory_tool_outputs_only_working_memory(self):
        tool = build_v3_working_memory_extraction_tool()

        assert tool["name"] == "extract_working_memory"
        properties = tool["parameters"]["properties"]
        assert list(properties.keys()) == ["working_memory"]
        assert tool["parameters"]["required"] == ["working_memory"]
        assert "profile_updates" not in properties

    def test_split_tool_name_helpers(self):
        assert v3_profile_extraction_tool_name() == "extract_profile_memory"
        assert v3_working_memory_extraction_tool_name() == "extract_working_memory"
```

Update imports in the test file to include:

```python
    build_v3_profile_extraction_tool,
    build_v3_working_memory_extraction_tool,
    v3_profile_extraction_tool_name,
    v3_working_memory_extraction_tool_name,
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py::TestSplitMemoryExtractionTools -v
```

Expected: import errors for the new functions.

- [ ] **Step 3: Extract reusable schema builders**

In `backend/memory/extraction.py`, add these constants near the existing tool name constants:

```python
_V3_PROFILE_EXTRACTION_TOOL_NAME = "extract_profile_memory"
_V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME = "extract_working_memory"
```

Move the inline `profile_item_schema` and `working_item_schema` bodies out of `build_v3_extraction_tool()` into private helpers:

```python
def _build_profile_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "enum": _V3_PROFILE_DOMAINS,
                "description": "长期画像所属领域。只能使用枚举值。",
            },
            "key": {
                "type": "string",
                "description": "稳定字段名，使用 snake_case，例如 avoid_red_eye、avoid_spicy。",
            },
            "value": {
                "description": "偏好或约束的值。可为 string / boolean / number / object。",
            },
            "polarity": {
                "type": "string",
                "enum": _V3_POLARITIES,
                "description": "用户是偏好、规避还是中性事实。",
            },
            "stability": {
                "type": "string",
                "enum": _V3_STABILITIES,
                "description": "稳定性标签。单次观察长期偏好请放 preference_hypotheses，而不是伪装成稳定偏好。",
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "0 到 1 的置信度。",
            },
            "reason": {
                "type": "string",
                "description": "简短说明为什么值得写入记忆。",
            },
            "evidence": {
                "type": "string",
                "description": "尽量贴近用户原话的证据短句。",
            },
        },
        "required": [
            "domain",
            "key",
            "value",
            "polarity",
            "stability",
            "confidence",
            "reason",
            "evidence",
        ],
        "additionalProperties": False,
    }


def _build_working_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "phase": {
                "type": "integer",
                "description": "该临时信号产生于哪个阶段，例如 1 / 3 / 5 / 7。",
            },
            "kind": {
                "type": "string",
                "enum": _V3_WORKING_KINDS,
                "description": "会话内临时记忆的类型。",
            },
            "domains": {
                "type": "array",
                "items": {"type": "string", "enum": _V3_WORKING_MEMORY_DOMAINS},
                "description": "相关领域，至少一个。",
            },
            "content": {
                "type": "string",
                "description": "会话内短期记忆内容，例如“这轮先别考虑迪士尼”。",
            },
            "reason": {
                "type": "string",
                "description": "为什么需要在当前会话保留这个临时信号。",
            },
            "status": {
                "type": "string",
                "enum": ["active"],
                "description": "当前仅允许 active。",
            },
            "expires": {
                "type": "object",
                "properties": {
                    "on_session_end": {"type": "boolean"},
                    "on_trip_change": {"type": "boolean"},
                    "on_phase_exit": {"type": "boolean"},
                },
                "required": [
                    "on_session_end",
                    "on_trip_change",
                    "on_phase_exit",
                ],
                "additionalProperties": False,
            },
        },
        "required": [
            "phase",
            "kind",
            "domains",
            "content",
            "reason",
            "status",
            "expires",
        ],
        "additionalProperties": False,
    }
```

Update `build_v3_extraction_tool()` to use:

```python
    profile_item_schema = _build_profile_item_schema()
    working_item_schema = _build_working_item_schema()
```

- [ ] **Step 4: Add split tool builders and helper names**

Add these functions after `build_v3_extraction_tool()`:

```python
def build_v3_profile_extraction_tool() -> dict[str, Any]:
    profile_item_schema = _build_profile_item_schema()
    return {
        "name": _V3_PROFILE_EXTRACTION_TOOL_NAME,
        "description": (
            "只提取跨旅行长期用户画像 profile_updates。当前行程事实、"
            "会话临时信号、PII、以及推测内容都不要输出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "profile_updates": {
                    "type": "object",
                    "properties": {
                        bucket: {"type": "array", "items": profile_item_schema}
                        for bucket in _V3_PROFILE_BUCKETS
                    },
                    "required": list(_V3_PROFILE_BUCKETS),
                    "additionalProperties": False,
                },
            },
            "required": ["profile_updates"],
            "additionalProperties": False,
        },
    }


def build_v3_working_memory_extraction_tool() -> dict[str, Any]:
    working_item_schema = _build_working_item_schema()
    return {
        "name": _V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME,
        "description": (
            "只提取当前 session/trip 内短期有用的 working_memory。"
            "长期画像、当前行程权威事实、PII、以及推测内容都不要输出。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "working_memory": {"type": "array", "items": working_item_schema},
            },
            "required": ["working_memory"],
            "additionalProperties": False,
        },
    }
```

Add helper names near existing helper functions:

```python
def v3_profile_extraction_tool_name() -> str:
    return _V3_PROFILE_EXTRACTION_TOOL_NAME


def v3_working_memory_extraction_tool_name() -> str:
    return _V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME
```

- [ ] **Step 5: Run split tool tests**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py::TestSplitMemoryExtractionTools -v
```

Expected: all split tool tests pass.

- [ ] **Step 6: Run full extraction unit tests**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py -v
```

Expected: all tests in `test_memory_extraction.py` pass.

- [ ] **Step 7: Commit Task 2**

Run:

```bash
git add backend/memory/extraction.py backend/tests/test_memory_extraction.py
git commit -m "feat: add split memory extraction tools"
```

---

### Task 3: Split Prompts And Parsers

**Files:**
- Modify: `backend/memory/extraction.py`
- Test: `backend/tests/test_memory_extraction.py`

- [ ] **Step 1: Write failing parser and prompt tests**

Append these tests to `TestSplitMemoryExtractionTools`:

```python
    def test_profile_prompt_excludes_working_memory_target(self):
        prompt = build_v3_profile_extraction_prompt(
            user_messages=["以后我都不坐红眼航班"],
            profile=UserMemoryProfile.empty("u1"),
            plan_facts={"destination": "京都"},
        )

        assert "extract_profile_memory" in prompt
        assert "profile_updates" in prompt
        assert "working_memory" not in prompt
        assert "本次目的地、日期、预算" in prompt

    def test_working_prompt_excludes_profile_updates_target(self):
        prompt = build_v3_working_memory_extraction_prompt(
            user_messages=["这轮先别考虑迪士尼"],
            working_memory=SessionWorkingMemory.empty("u1", "s1", "trip_1"),
            plan_facts={"destination": "东京"},
        )

        assert "extract_working_memory" in prompt
        assert "working_memory" in prompt
        assert "profile_updates" not in prompt
        assert "长期偏好" in prompt

    def test_parse_profile_tool_arguments(self):
        result = parse_v3_profile_extraction_tool_arguments(
            {
                "profile_updates": {
                    "constraints": [
                        {
                            "domain": "flight",
                            "key": "avoid_red_eye",
                            "value": True,
                            "polarity": "avoid",
                            "stability": "explicit_declared",
                            "confidence": 0.95,
                            "reason": "明确表达",
                            "evidence": "以后不坐红眼航班",
                        }
                    ],
                    "rejections": [],
                    "stable_preferences": [],
                    "preference_hypotheses": [],
                }
            }
        )

        assert result.profile_updates.constraints[0].key == "avoid_red_eye"
        assert result.working_memory == []

    def test_parse_working_memory_tool_arguments(self):
        result = parse_v3_working_memory_extraction_tool_arguments(
            {
                "working_memory": [
                    {
                        "phase": 3,
                        "kind": "temporary_rejection",
                        "domains": ["attraction"],
                        "content": "这轮先别考虑迪士尼",
                        "reason": "当前候选筛选需要避让",
                        "status": "active",
                        "expires": {
                            "on_session_end": True,
                            "on_trip_change": True,
                            "on_phase_exit": False,
                        },
                    }
                ]
            }
        )

        assert result.profile_updates.constraints == []
        assert result.working_memory[0].kind == "temporary_rejection"
```

Update imports in the test file to include:

```python
    SessionWorkingMemory,
    UserMemoryProfile,
    build_v3_profile_extraction_prompt,
    build_v3_working_memory_extraction_prompt,
    parse_v3_profile_extraction_tool_arguments,
    parse_v3_working_memory_extraction_tool_arguments,
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py::TestSplitMemoryExtractionTools -v
```

Expected: import errors for prompt and parser functions.

- [ ] **Step 3: Add split prompt builders**

Add these functions after `build_v3_extraction_prompt()`:

```python
def build_v3_profile_extraction_prompt(
    user_messages: list[str],
    profile: UserMemoryProfile,
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    profile_text = json.dumps(profile.to_dict(), ensure_ascii=False, indent=2)
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    return f"""你在执行一个内部长期画像提取任务。你的目标只有 `profile_updates`。

请调用工具 `{_V3_PROFILE_EXTRACTION_TOOL_NAME}` 提交结果，不要输出 JSON 正文、不要输出解释文字、不要输出代码块。

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有长期画像：
{profile_text}

分类规则：
- `constraints`：跨旅行硬约束，例如“不坐红眼航班”“不住青旅”。
- `rejections`：明确拒绝的对象，要带 value。
- `stable_preferences`：多次观察到，或用户明确声明为长期成立的稳定偏好。
- `preference_hypotheses`：单次观察得到的偏好假设。

硬性要求：
- 本次目的地、日期、预算、旅客人数、候选池、骨架、每日计划都属于当前 trip state，不要输出。
- 当前会话临时信号不要输出；这些由 working memory extractor 处理。
- 支付信息、会员信息、身份证号、护照号、手机号、邮箱、银行卡号等敏感信息直接忽略，不要输出。
- 不要推测用户没说过的偏好。
- 不要重复已有 profile 条目。

如果没有可提取内容，就调用工具并传入空数组。"""


def build_v3_working_memory_extraction_prompt(
    user_messages: list[str],
    working_memory: SessionWorkingMemory,
    plan_facts: dict[str, Any],
) -> str:
    messages_text = "\n".join(f"- {message}" for message in user_messages)
    working_text = json.dumps(working_memory.to_dict(), ensure_ascii=False, indent=2)
    facts_text = json.dumps(plan_facts, ensure_ascii=False, indent=2)
    return f"""你在执行一个内部会话工作记忆提取任务。你的目标只有 `working_memory`。

请调用工具 `{_V3_WORKING_MEMORY_EXTRACTION_TOOL_NAME}` 提交结果，不要输出 JSON 正文、不要输出解释文字、不要输出代码块。

用户消息：
{messages_text}

当前解析出的本次行程事实：
{facts_text}

已有会话工作记忆：
{working_text}

分类规则：
- `temporary_preference`：只在当前 session/trip 内成立的临时偏好。
- `temporary_rejection`：只在当前 session/trip 内成立的临时否决。
- `decision_hint`：当前规划后续应记住的决策线索。
- `open_question`：用户还没有回答、后续需要追问的问题。
- `watchout`：当前 trip 后续需要避开的风险提醒。
- `note`：无法归入以上类型但当前会话有用的短期备注。

硬性要求：
- 当前 trip 的目的地、日期、预算、旅客人数、候选池、骨架、每日计划属于 TravelPlanState，不要输出。
- 长期偏好、长期约束、永久拒绝不要输出；这些由 profile extractor 处理。
- 支付信息、会员信息、身份证号、护照号、手机号、邮箱、银行卡号等敏感信息直接忽略，不要输出。
- 不要重复已有 working memory 条目。
- 每条 item 必须设置 `status=active`，并设置完整 expires。

如果没有可提取内容，就调用工具并传入空数组。"""
```

- [ ] **Step 4: Add split parsers**

Add these parser functions after `parse_v3_extraction_tool_arguments()`:

```python
def parse_v3_profile_extraction_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionResult:
    if not isinstance(arguments, dict):
        return V3ExtractionResult()
    return parse_v3_extraction_response(
        json.dumps(
            {
                "profile_updates": arguments.get("profile_updates", {}),
                "working_memory": [],
            },
            ensure_ascii=False,
        )
    )


def parse_v3_working_memory_extraction_tool_arguments(
    arguments: dict[str, Any] | None,
) -> V3ExtractionResult:
    if not isinstance(arguments, dict):
        return V3ExtractionResult()
    return parse_v3_extraction_response(
        json.dumps(
            {
                "profile_updates": {
                    "constraints": [],
                    "rejections": [],
                    "stable_preferences": [],
                    "preference_hypotheses": [],
                },
                "working_memory": arguments.get("working_memory", []),
            },
            ensure_ascii=False,
        )
    )
```

- [ ] **Step 5: Run prompt and parser tests**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py::TestSplitMemoryExtractionTools -v
```

Expected: all split prompt and parser tests pass.

- [ ] **Step 6: Run all extraction unit tests**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add backend/memory/extraction.py backend/tests/test_memory_extraction.py
git commit -m "feat: split memory extraction prompts"
```

---

### Task 4: Route-Aware Orchestration In Main

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Update forced-tool-call integration test**

In `backend/tests/test_memory_integration.py`, replace `test_memory_extraction_uses_forced_tool_call` with:

```python
@pytest.mark.asyncio
async def test_memory_extraction_uses_routed_forced_tool_calls(app):
    observed = {"calls": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ExtractionProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["calls"].append(
                {
                    "tool_name": tool_name,
                    "tools": tools,
                    "tool_choice": tool_choice,
                }
            )
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": True},
                            "reason": "mixed_profile_and_working_signal",
                            "message": "检测到长期偏好和临时规划信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [
                                    {
                                        "domain": "food",
                                        "key": "avoid_spicy",
                                        "value": "不吃辣",
                                        "polarity": "avoid",
                                        "stability": "explicit_declared",
                                        "confidence": 0.95,
                                        "context": {},
                                        "applicability": "通用旅行饮食偏好",
                                        "recall_hints": {"keywords": ["不吃辣"]},
                                        "source_refs": [],
                                    }
                                ],
                                "preference_hypotheses": [],
                            }
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_working",
                    name=tool_name,
                    arguments={
                        "working_memory": [
                            {
                                "phase": 3,
                                "kind": "temporary_rejection",
                                "domains": ["attraction"],
                                "content": "这轮先别考虑迪士尼",
                                "reason": "当前候选筛选需要避让",
                                "status": "active",
                                "expires": {
                                    "on_session_end": True,
                                    "on_trip_change": True,
                                    "on_phase_exit": False,
                                },
                            }
                        ]
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ExtractionProvider())

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={
                    "message": "我不吃辣，这轮先别考虑迪士尼",
                    "user_id": "u1",
                },
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert [call["tool_name"] for call in observed["calls"]] == [
        "decide_memory_extraction",
        "extract_profile_memory",
        "extract_working_memory",
    ]
    assert observed["calls"][0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "decide_memory_extraction"},
    }
    assert observed["calls"][1]["tool_choice"] == {
        "type": "function",
        "function": {"name": "extract_profile_memory"},
    }
    assert observed["calls"][2]["tool_choice"] == {
        "type": "function",
        "function": {"name": "extract_working_memory"},
    }
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py::test_memory_extraction_uses_routed_forced_tool_calls -v
```

Expected: observed calls still include `extract_memory_candidates`, so the assertion fails.

- [ ] **Step 3: Import split extraction helpers in `main.py`**

Update the `from memory.extraction import (...)` block in `backend/main.py` to include:

```python
    build_v3_profile_extraction_prompt,
    build_v3_profile_extraction_tool,
    build_v3_working_memory_extraction_prompt,
    build_v3_working_memory_extraction_tool,
    parse_v3_profile_extraction_tool_arguments,
    parse_v3_working_memory_extraction_tool_arguments,
```

Keep the existing combined functions imported until all tests are migrated.

- [ ] **Step 4: Update gate fallback message in `_decide_memory_extraction()`**

In `_decide_memory_extraction()`, after parsing `decision`, change the default message logic to route-aware text:

```python
        if not decision.reason:
            if decision.routes.profile and decision.routes.working_memory:
                decision.reason = "mixed_profile_and_working_signal"
            elif decision.routes.profile:
                decision.reason = "profile_memory_signal"
            elif decision.routes.working_memory:
                decision.reason = "working_memory_signal"
            else:
                decision.reason = "no_reusable_memory_signal"
        if not decision.message:
            if decision.routes.profile and decision.routes.working_memory:
                decision.message = "检测到长期偏好和临时规划信号"
            elif decision.routes.profile:
                decision.message = "检测到长期旅行偏好信号"
            elif decision.routes.working_memory:
                decision.message = "检测到当前会话临时记忆信号"
            else:
                decision.message = "本轮未发现可复用记忆信号"
```

The returned `MemoryExtractionGateDecision` must preserve routes. The dataclass is defined in `backend/main.py`.

- [ ] **Step 5: Add route fields to `MemoryExtractionGateDecision`**

Find the `MemoryExtractionGateDecision` dataclass in `backend/main.py`. Add:

```python
    routes: dict[str, bool] = field(default_factory=dict)
```

Update `to_result()` to include:

```python
            "routes": dict(self.routes),
```

When returning from `_decide_memory_extraction()`, set:

```python
            routes={
                "profile": decision.routes.profile,
                "working_memory": decision.routes.working_memory,
            },
```

- [ ] **Step 6: Split `_do_extract_memory_candidates()` into route-aware helpers**

Inside `backend/main.py`, add two local helpers near `_do_extract_memory_candidates()`:

```python
    async def _extract_profile_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
    ) -> V3ExtractionResult:
        profile = await memory_mgr.v3_store.load_profile(user_id)
        prompt = build_v3_profile_extraction_prompt(
            user_messages=user_messages,
            profile=profile,
            plan_facts=_memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider(config.llm)
        tool_args = await _collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_profile_extraction_tool(),
        )
        return parse_v3_profile_extraction_tool_arguments(tool_args)


    async def _extract_working_memory_items(
        *,
        session_id: str,
        user_id: str,
        user_messages: list[str],
        plan_snapshot: TravelPlanState,
    ) -> V3ExtractionResult:
        working_memory = await memory_mgr.v3_store.load_working_memory(
            user_id, session_id, plan_snapshot.trip_id
        )
        prompt = build_v3_working_memory_extraction_prompt(
            user_messages=user_messages,
            working_memory=working_memory,
            plan_facts=_memory_plan_facts(plan_snapshot),
        )
        extraction_llm = create_llm_provider(config.llm)
        tool_args = await _collect_forced_tool_call_arguments(
            extraction_llm,
            messages=[Message(role=Role.USER, content=prompt)],
            tool_def=build_v3_working_memory_extraction_tool(),
        )
        return parse_v3_working_memory_extraction_tool_arguments(tool_args)
```

Keep `_do_extract_memory_candidates()` as the orchestrator that calls these helpers based on `gate_decision.routes`.

- [ ] **Step 7: Pass route decision into extraction**

Change `_extract_memory_candidates()` and `_do_extract_memory_candidates()` signatures to accept:

```python
        routes: dict[str, bool] | None = None,
```

At the start of `_do_extract_memory_candidates()`, normalize:

```python
        route_flags = routes or {"profile": True, "working_memory": True}
        run_profile = bool(route_flags.get("profile"))
        run_working = bool(route_flags.get("working_memory"))
        if not run_profile and not run_working:
            return MemoryExtractionOutcome(
                status="skipped",
                message="本轮没有新的可复用记忆",
                item_ids=[],
                reason="no_routes",
            )
```

Update `_run_memory_job()` so the extraction call passes:

```python
            routes=gate_decision.routes,
```

- [ ] **Step 8: Combine split extraction results before policy/write**

In `_do_extract_memory_candidates()`, replace the combined forced call with:

```python
        result = V3ExtractionResult()
        if run_profile:
            profile_result = await _extract_profile_memory_items(
                session_id=session_id,
                user_id=user_id,
                user_messages=user_messages,
                plan_snapshot=plan_snapshot,
            )
            result.profile_updates = profile_result.profile_updates
        if run_working:
            working_result = await _extract_working_memory_items(
                session_id=session_id,
                user_id=user_id,
                user_messages=user_messages,
                plan_snapshot=plan_snapshot,
            )
            result.working_memory = working_result.working_memory
```

Leave the existing policy/write block in place after `result` is assembled.

- [ ] **Step 9: Run routed forced-tool-call test**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py::test_memory_extraction_uses_routed_forced_tool_calls -v
```

Expected: pass.

- [ ] **Step 10: Run affected memory integration tests**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "memory_extraction" -v
```

Expected: remaining failures are only tests that still expect `extract_memory_candidates`; update those tests in later tasks.

- [ ] **Step 11: Commit Task 4**

Run:

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "feat: route memory extraction orchestration"
```

---

### Task 5: Route-Specific Integration Coverage

**Files:**
- Modify: `backend/tests/test_memory_integration.py`
- Modify: `backend/main.py` if tests reveal missing route handling

- [ ] **Step 1: Add profile-only integration test**

Add this test to `backend/tests/test_memory_integration.py`:

```python
@pytest.mark.asyncio
async def test_memory_extraction_profile_route_writes_profile_only(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class ProfileOnlyProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
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
                            "message": "检测到长期旅行偏好信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_profile_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_profile",
                    name=tool_name,
                    arguments={
                        "profile_updates": {
                            "constraints": [],
                            "rejections": [],
                            "stable_preferences": [
                                {
                                    "domain": "food",
                                    "key": "avoid_spicy",
                                    "value": "不吃辣",
                                    "polarity": "avoid",
                                    "stability": "explicit_declared",
                                    "confidence": 0.95,
                                    "context": {},
                                    "applicability": "通用旅行饮食偏好",
                                    "recall_hints": {"keywords": ["不吃辣"]},
                                    "source_refs": [],
                                }
                            ],
                            "preference_hypotheses": [],
                        }
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: ProfileOnlyProvider())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")
            working = await client.get(
                f"/api/memory/u1/sessions/{session_id}/working-memory"
            )

    assert resp.status_code == 200
    assert profile.json()["stable_preferences"][0]["key"] == "avoid_spicy"
    assert working.json()["items"] == []
```

- [ ] **Step 2: Add working-only integration test**

Add:

```python
@pytest.mark.asyncio
async def test_memory_extraction_working_route_writes_working_only(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class WorkingOnlyProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": False, "working_memory": True},
                            "reason": "working_memory_signal",
                            "message": "检测到当前会话临时记忆信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            assert tool_name == "extract_working_memory"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_working",
                    name=tool_name,
                    arguments={
                        "working_memory": [
                            {
                                "phase": 3,
                                "kind": "temporary_rejection",
                                "domains": ["attraction"],
                                "content": "这轮先别考虑迪士尼",
                                "reason": "当前候选筛选需要避让",
                                "status": "active",
                                "expires": {
                                    "on_session_end": True,
                                    "on_trip_change": True,
                                    "on_phase_exit": False,
                                },
                            }
                        ]
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: WorkingOnlyProvider())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)
            profile = await client.get("/api/memory/u1/profile")
            working = await client.get(
                f"/api/memory/u1/sessions/{session_id}/working-memory"
            )

    assert resp.status_code == 200
    assert profile.json()["stable_preferences"] == []
    assert working.json()["items"][0]["content"] == "这轮先别考虑迪士尼"
```

- [ ] **Step 3: Add no-route skip integration test**

Add:

```python
@pytest.mark.asyncio
async def test_memory_extraction_no_routes_skips_extractors(app):
    observed = {"tools": []}

    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class NoRouteProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            observed["tools"].append(tool_name)
            assert tool_name == "decide_memory_extraction"
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_gate",
                    name=tool_name,
                    arguments={
                        "should_extract": False,
                        "routes": {"profile": False, "working_memory": False},
                        "reason": "trip_state_only",
                        "message": "本轮只是当前行程事实",
                    },
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: NoRouteProvider())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "这次五一去京都，预算3万", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    assert resp.status_code == 200
    assert observed["tools"] == ["decide_memory_extraction"]
```

- [ ] **Step 4: Run route-specific integration tests**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "profile_route or working_route or no_routes or routed_forced" -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Fix only route handling defects found by tests**

If any selected test fails, make the smallest code change in `backend/main.py` that matches the test expectation. Do not change schemas or prompts in this task.

- [ ] **Step 6: Run selected tests again**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "profile_route or working_route or no_routes or routed_forced" -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit Task 5**

Run:

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "test: cover routed memory extraction"
```

---

### Task 6: Background Task Names And Partial Failure Semantics

**Files:**
- Modify: `backend/main.py`
- Test: `backend/tests/test_memory_integration.py`

- [ ] **Step 1: Add test for split internal task kinds**

Add:

```python
@pytest.mark.asyncio
async def test_memory_extraction_publishes_split_internal_tasks(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class BothProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": True},
                            "reason": "mixed_profile_and_working_signal",
                            "message": "检测到长期偏好和临时规划信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [],
                                "preference_hypotheses": [],
                            }
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            yield LLMChunk(
                type=ChunkType.TOOL_CALL_START,
                tool_call=ToolCall(
                    id="tc_working",
                    name=tool_name,
                    arguments={"working_memory": []},
                ),
            )
            yield LLMChunk(type=ChunkType.DONE)

    run_memory_job = app.state.run_memory_job
    original_publish = _get_function_closure_value(run_memory_job, "_publish_memory_task")
    published_tasks = []

    def recording_publish(session_id: str, task):
        published_tasks.append(task)
        original_publish(session_id, task)

    with pytest.MonkeyPatch.context() as mp:
        _set_function_closure_value(
            run_memory_job, "_publish_memory_task", recording_publish
        )
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: BothProvider())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    _set_function_closure_value(
        run_memory_job, "_publish_memory_task", original_publish
    )

    kinds = [getattr(task, "kind", None) for task in published_tasks]
    assert "memory_extraction_gate" in kinds
    assert "profile_memory_extraction" in kinds
    assert "working_memory_extraction" in kinds
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py::test_memory_extraction_publishes_split_internal_tasks -v
```

Expected: missing `profile_memory_extraction` and `working_memory_extraction`.

- [ ] **Step 3: Publish profile extraction task lifecycle**

In `_run_memory_job()`, before calling profile extraction, publish pending:

```python
        profile_task_id = f"profile_memory_extraction:{snapshot.session_id}:{snapshot.turn_id}"
        profile_started_at = time.time()
        if gate_decision.routes.get("profile"):
            _publish_memory_task(
                snapshot.session_id,
                InternalTask(
                    id=profile_task_id,
                    kind="profile_memory_extraction",
                    label="长期画像提取",
                    status="pending",
                    message="正在提取长期旅行画像…",
                    blocking=False,
                    scope="background",
                    started_at=profile_started_at,
                ),
            )
```

After profile extraction finishes, publish success/skipped/warning/error with:

```python
                    kind="profile_memory_extraction",
                    label="长期画像提取",
```

Use `saved_profile_count` and `pending_profile_count` in `result`.

- [ ] **Step 4: Publish working-memory extraction task lifecycle**

In `_run_memory_job()`, before calling working-memory extraction, publish pending:

```python
        working_task_id = f"working_memory_extraction:{snapshot.session_id}:{snapshot.turn_id}"
        working_started_at = time.time()
        if gate_decision.routes.get("working_memory"):
            _publish_memory_task(
                snapshot.session_id,
                InternalTask(
                    id=working_task_id,
                    kind="working_memory_extraction",
                    label="工作记忆提取",
                    status="pending",
                    message="正在提取当前会话工作记忆…",
                    blocking=False,
                    scope="background",
                    started_at=working_started_at,
                ),
            )
```

After working extraction finishes, publish success/skipped/warning/error with:

```python
                    kind="working_memory_extraction",
                    label="工作记忆提取",
```

Use `saved_working_count` in `result`.

- [ ] **Step 5: Preserve aggregate outcome**

Keep the existing `memory_extraction` aggregate task for compatibility. Its result must include:

```python
{
    "routes": gate_decision.routes,
    "saved_profile_count": outcome.saved_profile_count,
    "saved_working_count": outcome.saved_working_count,
    "item_ids": outcome.item_ids,
    "reason": outcome.reason,
}
```

Do not remove the aggregate `memory_extraction` task in this implementation pass. The split tasks add observability without changing existing consumers that already understand `memory_extraction`.

- [ ] **Step 6: Add partial failure test**

Add:

```python
@pytest.mark.asyncio
async def test_memory_extraction_partial_failure_keeps_consumed_count_unadvanced(app):
    async def fake_run(self, messages, phase, tools_override=None):
        yield LLMChunk(type=ChunkType.DONE)

    class PartialFailureProvider:
        async def chat(self, messages, tools=None, stream=True, tool_choice=None):
            tool_name = tools[0]["name"]
            if tool_name == "decide_memory_extraction":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_gate",
                        name=tool_name,
                        arguments={
                            "should_extract": True,
                            "routes": {"profile": True, "working_memory": True},
                            "reason": "mixed_profile_and_working_signal",
                            "message": "检测到长期偏好和临时规划信号",
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            if tool_name == "extract_profile_memory":
                yield LLMChunk(
                    type=ChunkType.TOOL_CALL_START,
                    tool_call=ToolCall(
                        id="tc_profile",
                        name=tool_name,
                        arguments={
                            "profile_updates": {
                                "constraints": [],
                                "rejections": [],
                                "stable_preferences": [],
                                "preference_hypotheses": [],
                            }
                        },
                    ),
                )
                yield LLMChunk(type=ChunkType.DONE)
                return
            raise RuntimeError("working extraction failed")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.loop.AgentLoop.run", fake_run)
        mp.setattr("main.create_llm_provider", lambda _config: PartialFailureProvider())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            session_resp = await client.post("/api/sessions")
            session_id = session_resp.json()["session_id"]
            resp = await client.post(
                f"/api/chat/{session_id}",
                json={"message": "我不吃辣，这轮先别考虑迪士尼", "user_id": "u1"},
            )
            await _wait_for_memory_scheduler_idle(app, session_id)

    runtime = app.state.memory_scheduler_runtimes[session_id]
    assert resp.status_code == 200
    assert runtime.last_consumed_user_count == 0
```

- [ ] **Step 7: Implement partial failure outcome**

Wrap each routed extractor call in `_extract_memory_candidates()` with exception handling that records a warning outcome and lets already-written items remain. Return:

```python
MemoryExtractionOutcome(
    status="warning",
    message="部分记忆提取失败，本轮将稍后重试。",
    item_ids=pending_ids,
    saved_profile_count=saved_profile_count,
    saved_working_count=saved_working_count,
    reason="partial_failure",
    error="working_memory_extraction_failed",
)
```

Do not update `last_consumed_user_count` when outcome status is `warning`.

- [ ] **Step 8: Run internal task and partial failure tests**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py -k "split_internal_tasks or partial_failure" -v
```

Expected: both tests pass.

- [ ] **Step 9: Commit Task 6**

Run:

```bash
git add backend/main.py backend/tests/test_memory_integration.py
git commit -m "feat: expose split memory extraction tasks"
```

---

### Task 7: Full Regression And Documentation

**Files:**
- Modify: `PROJECT_OVERVIEW.md`
- Test: memory-focused pytest suite

- [ ] **Step 1: Update `PROJECT_OVERVIEW.md` runtime memory description**

In `PROJECT_OVERVIEW.md`, update the Memory System row to describe:

```text
用户消息一进入 chat 就提交后台 memory job；后台先执行 memory_extraction_gate，gate 输出 profile / working_memory routes，再按需执行 profile_memory_extraction 与 working_memory_extraction；chat 与提取保持解耦。
```

Update the Internal task stream section so background tasks list includes:

```text
memory_extraction_gate
profile_memory_extraction
working_memory_extraction
```

- [ ] **Step 2: Run memory unit tests**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py tests/test_memory_policy.py tests/test_memory_v3_store.py tests/test_memory_manager.py -v
```

Expected: all selected unit tests pass.

- [ ] **Step 3: Run memory integration tests**

Run:

```bash
cd backend && pytest tests/test_memory_integration.py tests/test_memory_v3_api.py -v
```

Expected: all selected integration tests pass.

- [ ] **Step 4: Run targeted API regression**

Run:

```bash
cd backend && pytest tests/test_api.py -k "memory_extraction or memory" -v
```

Expected: selected API tests pass.

- [ ] **Step 5: Run broader backend smoke set**

Run:

```bash
cd backend && pytest tests/test_memory_extraction.py tests/test_memory_integration.py tests/test_agent_loop.py tests/test_api.py -v
```

Expected: all selected tests pass.

- [ ] **Step 6: Check working tree**

Run:

```bash
git status --short
```

Expected: only intended files are modified.

- [ ] **Step 7: Commit Task 7**

Run:

```bash
git add PROJECT_OVERVIEW.md backend/memory/extraction.py backend/main.py backend/tests/test_memory_extraction.py backend/tests/test_memory_integration.py backend/tests/test_api.py
git commit -m "docs: update memory extraction routing overview"
```

---

## Execution Notes

- Preserve the existing async memory scheduling behavior. Do not move extraction back into the chat SSE critical path.
- Preserve current `MemoryPolicy` behavior. This plan changes routing and extraction shape, not risk policy.
- Preserve current working memory storage path. The `session_id` / `trip_id` storage question is recorded separately in `docs/TODO.md`.
- Do not delete compatibility parser code until all tests no longer depend on it.
