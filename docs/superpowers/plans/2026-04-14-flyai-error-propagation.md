# FlyAI Error Propagation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `search_trains`、`quick_travel_search`、`get_poi_info` 在 FlyAI 出现额度限制、鉴权失败或 CLI 错误时，和 `search_flights` 一样把真实错误透传到工具层，同时保持成功路径和双源降级行为不变。

**Architecture:** 继续沿用现有分层：`FlyAIClient` 负责把 CLI 文本错误识别成异常，具体工具根据单源/双源特点决定是直接转成 `ToolError` 还是在另一数据源成功时继续降级。实现保持在现有文件内，不新增中间抽象层。

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, respx, httpx

---

### Task 1: 为 `search_trains` 补 FlyAI 真实错误透传测试并实现

**Files:**
- Modify: `backend/tests/test_search_trains.py`
- Modify: `backend/tools/search_trains.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_search_trains.py` 末尾新增：

```python
@pytest.mark.asyncio
async def test_search_trains_surfaces_flyai_runtime_error(mock_flyai_client):
    mock_flyai_client.search_train.side_effect = RuntimeError(
        "Trial limit reached. Please configure FLYAI_API_KEY"
    )

    tool_fn = make_search_trains_tool(mock_flyai_client)

    with pytest.raises(ToolError, match="Trial limit reached"):
        await tool_fn(origin="北京", destination="上海", date="2026-04-15")
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `pytest backend/tests/test_search_trains.py::test_search_trains_surfaces_flyai_runtime_error -q`

Expected: FAIL，因为当前 `search_trains` 会直接抛 `RuntimeError` 或给出不匹配文案，而不是稳定的 `ToolError` 透传。

- [ ] **Step 3: 写最小实现**

修改 `backend/tools/search_trains.py` 的 `raw_list = ...` 调用段：

```python
        try:
            raw_list = await flyai_client.search_train(origin=origin, **kwargs)
        except Exception as exc:
            raise ToolError(
                f"FlyAI train search failed: {exc}",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Check FlyAI CLI quota/auth status or retry later.",
            ) from exc
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `pytest backend/tests/test_search_trains.py -q`

Expected: PASS

- [ ] **Step 5: 提交本任务变更**

```bash
git add backend/tests/test_search_trains.py backend/tools/search_trains.py
git commit -m "fix: surface FlyAI train search errors"
```

### Task 2: 为 `quick_travel_search` 补 FlyAI 真实错误透传测试并实现

**Files:**
- Modify: `backend/tests/test_flyai_new_tools.py`
- Modify: `backend/tools/quick_travel_search.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_flyai_new_tools.py` 末尾新增：

```python
@pytest.mark.asyncio
async def test_quick_travel_search_surfaces_flyai_runtime_error(mock_flyai_client):
    from tools.quick_travel_search import make_quick_travel_search_tool

    mock_flyai_client.fast_search.side_effect = RuntimeError(
        "Trial limit reached. Please configure FLYAI_API_KEY"
    )

    tool_fn = make_quick_travel_search_tool(mock_flyai_client)

    with pytest.raises(ToolError, match="Trial limit reached"):
        await tool_fn(query="杭州三日游")
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `pytest backend/tests/test_flyai_new_tools.py::test_quick_travel_search_surfaces_flyai_runtime_error -q`

Expected: FAIL，因为当前实现没有把真实异常转换成稳定的 `ToolError`。

- [ ] **Step 3: 写最小实现**

修改 `backend/tools/quick_travel_search.py`：

```python
        try:
            raw_list = await flyai_client.fast_search(query=query)
        except Exception as exc:
            raise ToolError(
                f"FlyAI quick travel search failed: {exc}",
                error_code="SERVICE_UNAVAILABLE",
                suggestion="Check FlyAI CLI quota/auth status or retry later.",
            ) from exc
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `pytest backend/tests/test_flyai_new_tools.py -q`

Expected: PASS

- [ ] **Step 5: 提交本任务变更**

```bash
git add backend/tests/test_flyai_new_tools.py backend/tools/quick_travel_search.py
git commit -m "fix: surface FlyAI quick search errors"
```

### Task 3: 为 `get_poi_info` 补双源场景测试并实现

**Files:**
- Modify: `backend/tests/test_get_poi_info.py`
- Modify: `backend/tools/get_poi_info.py`

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_get_poi_info.py` 中新增两个测试：

```python
@respx.mock
@pytest.mark.asyncio
async def test_get_poi_info_surfaces_flyai_error_when_google_empty():
    class StubFlyAIClient:
        available = True

        async def search_poi(self, **kwargs):
            raise RuntimeError("Trial limit reached. Please configure FLYAI_API_KEY")

    keys = ApiKeysConfig(google_maps="test_key")
    fn = make_get_poi_info_tool(keys, StubFlyAIClient())

    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(200, json={"results": []})
    )

    from tools.base import ToolError

    with pytest.raises(ToolError, match="Trial limit reached"):
        await fn(query="金阁寺", location="京都")


@respx.mock
@pytest.mark.asyncio
async def test_get_poi_info_keeps_google_results_when_flyai_fails():
    class StubFlyAIClient:
        available = True

        async def search_poi(self, **kwargs):
            raise RuntimeError("Trial limit reached. Please configure FLYAI_API_KEY")

    keys = ApiKeysConfig(google_maps="test_key")
    fn = make_get_poi_info_tool(keys, StubFlyAIClient())

    respx.get("https://maps.googleapis.com/maps/api/place/textsearch/json").mock(
        return_value=Response(
            200,
            json={
                "results": [
                    {
                        "name": "Kinkaku-ji",
                        "formatted_address": "1 Kinkakujicho, Kyoto",
                        "rating": 4.6,
                        "geometry": {"location": {"lat": 35.04, "lng": 135.73}},
                        "types": ["tourist_attraction", "place_of_worship"],
                    }
                ]
            },
        )
    )

    result = await fn(query="金阁寺", location="京都")

    assert len(result["pois"]) == 1
    assert result["pois"][0]["source"] == "google"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `pytest backend/tests/test_get_poi_info.py -q`

Expected: FAIL，因为当前实现没有在最终无结果时把 FlyAI 真实原因带入 `ToolError`。

- [ ] **Step 3: 写最小实现**

在 `backend/tools/get_poi_info.py` 保持现有并发结构，只调整最终错误拼装逻辑：

```python
        if not google_results and not flyai_results:
            reasons = []
            if not api_keys.google_maps:
                reasons.append("Google Maps API key not configured")
            elif isinstance(results[0], BaseException):
                reasons.append(f"Google error: {results[0]}")

            if not flyai_client or not flyai_client.available:
                reasons.append("FlyAI CLI not available")
            elif isinstance(results[1], BaseException):
                reasons.append(f"FlyAI error: {results[1]}")

            if not api_keys.google_maps:
                raise ToolError(
                    "Google Maps API key not configured"
                    + (f" ({'; '.join(reasons)})" if reasons else ""),
                    error_code="NO_API_KEY",
                    suggestion="Set GOOGLE_MAPS_API_KEY",
                )

            raise ToolError(
                "No POI results from any source"
                + (f" ({'; '.join(reasons)})" if reasons else ""),
                error_code="NO_RESULTS",
                suggestion="Try a different search query",
            )
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `pytest backend/tests/test_get_poi_info.py -q`

Expected: PASS

- [ ] **Step 5: 提交本任务变更**

```bash
git add backend/tests/test_get_poi_info.py backend/tools/get_poi_info.py
git commit -m "fix: surface FlyAI poi errors"
```

### Task 4: 更新文档并做最终验证

**Files:**
- Modify: `PROJECT_OVERVIEW.md`

- [ ] **Step 1: 更新项目概览**

在 `PROJECT_OVERVIEW.md` 的工具相关说明中，把 FlyAI 相关工具的错误处理说明更新为“真实错误透传 + 双源降级”。

建议补充内容：

```md
- `search_flights` / `get_poi_info`：双源查询，单路失败时降级，双路失败时汇总真实错误原因
- `search_trains` / `quick_travel_search`：FlyAI 单源，直接透传 CLI 配额/鉴权/服务错误
```

- [ ] **Step 2: 跑最终定向测试**

Run: `pytest backend/tests/test_search_trains.py backend/tests/test_flyai_new_tools.py backend/tests/test_get_poi_info.py -q`

Expected: PASS

- [ ] **Step 3: 跑补充回归测试**

Run: `pytest backend/tests/test_flyai_client.py backend/tests/test_search_flights.py backend/tests/test_tool_fusion.py -q`

Expected: PASS，确保本轮修改没有破坏已修好的航班逻辑

- [ ] **Step 4: 提交最终文档与验证变更**

```bash
git add PROJECT_OVERVIEW.md
git commit -m "docs: update FlyAI error propagation overview"
```

## 自检

- 规格覆盖：`search_trains`、`quick_travel_search`、`get_poi_info` 三个工具都覆盖到了；单源/双源两种错误路径都在任务中
- 占位符检查：无 TBD/TODO/“自行实现” 之类占位语句
- 类型一致性：统一使用 `RuntimeError` 作为 FlyAI 客户端上抛异常，工具层统一转换为 `ToolError`
