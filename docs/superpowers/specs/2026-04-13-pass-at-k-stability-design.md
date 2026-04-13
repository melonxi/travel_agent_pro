# Spec 2: pass@k 稳定性评估

> **目标**：同一 golden case 多次运行，量化 Agent 行为一致性，回答面试追问"跑 5 次结果一样吗"。
>
> **隔离边界**：仅新增/修改 `backend/evals/` 和 `scripts/`。不碰 harness/、tools/、main.py、frontend/。

---

## 1. 背景与动机

当前 eval pipeline 有 23 个 golden cases 和完整的断言评估，但每个 case 只跑一次。LLM 输出具有随机性，单次通过不代表稳定通过。

pass@k 是行业标准的稳定性度量：同一任务跑 k 次，统计通过比例。面试时能说"这个场景 pass@5 = 0.8"比"这个场景通过了"有力得多。

---

## 2. 数据结构

### 2.1 StabilityResult — `backend/evals/models.py` 末尾追加

```python
@dataclass
class StabilityMetrics:
    """Aggregated stability metrics for a single case over k runs."""
    case_id: str
    k: int
    pass_rate: float                           # k 次中 passed 的比例
    assertion_consistency: dict[str, float]     # 每条断言的通过率 {断言描述: 0.0-1.0}
    tool_overlap_ratio: float                   # 工具调用集合的一致性 (交集/并集)
    cost_stats: dict[str, float]               # min/max/mean/stddev of estimated_cost_usd
    duration_stats: dict[str, float]           # min/max/mean/stddev of duration_ms
    runs: list[CaseResult]                     # 原始 k 次 CaseResult


@dataclass
class StabilitySuiteResult:
    """Stability results for all cases."""
    total_cases: int = 0
    k: int = 3
    results: list[StabilityMetrics] = field(default_factory=list)
    overall_pass_rate: float = 0.0             # 所有 case 的平均 pass_rate
    unstable_cases: list[str] = field(default_factory=list)  # pass_rate < 1.0 的 case IDs
    highly_unstable_cases: list[str] = field(default_factory=list)  # pass_rate < 0.6 的 case IDs
```

追加位置：`models.py` 文件末尾，不修改已有的 GoldenCase / CaseResult / SuiteResult 等类。

---

## 3. 核心模块

### 3.1 `backend/evals/stability.py` — 新建

**核心函数**：

```python
def run_stability(
    case: GoldenCase,
    executor: GoldenCaseExecutor,
    k: int = 3,
) -> StabilityMetrics:
```

流程：
1. 调用 `runner.run_case(case, executor)` 共 k 次，收集 `list[CaseResult]`
2. 计算 `pass_rate` = 通过次数 / k
3. 计算 `assertion_consistency`：遍历每条断言，统计其在 k 次中的通过率。断言标识用 `f"{assertion.type.value}:{assertion.target}"` 作为 key
4. 计算 `tool_overlap_ratio`：将每次运行的 tool_calls 转为集合，计算 k 次的交集 / 并集比率。如果 k 次工具调用完全一致，ratio = 1.0
5. 计算 `cost_stats` 和 `duration_stats`：从每次 CaseResult.stats 中提取 `estimated_cost_usd` 和 `duration_ms`，计算 min/max/mean/stddev（使用 `statistics.mean` 和 `statistics.stdev`，k < 2 时 stddev = 0）

**Suite 函数**：

```python
def run_stability_suite(
    cases: list[GoldenCase],
    executor: GoldenCaseExecutor,
    k: int = 3,
) -> StabilitySuiteResult:
```

遍历所有 case 调用 `run_stability`，汇总 `overall_pass_rate`，识别 `unstable_cases` 和 `highly_unstable_cases`。

**报告函数**：

```python
def save_stability_report(
    suite: StabilitySuiteResult,
    output_path: str | Path,
) -> tuple[Path, Path]:
```

输出两份文件：
- `{output_path}.json`：完整结构化数据
- `{output_path}.md`：Markdown 报告

### 3.2 Markdown 报告格式

```markdown
# pass@k 稳定性评估报告

- 运行时间：2026-04-13 14:30
- k = 5
- 覆盖 case 数：23

## 总览

| 指标 | 值 |
|------|-----|
| 总体 pass@k | 0.82 |
| 不稳定 case（pass_rate < 1.0） | 5 |
| 高度不稳定 case（pass_rate < 0.6） | 1 |

## 按 Case 详情

| Case | 难度 | pass@5 | 不稳定断言 | 工具一致性 | 平均成本 |
|------|------|--------|-----------|-----------|---------|
| easy-tokyo-5d | easy | 5/5 | — | 1.00 | $0.08 |
| hard-round-trip | hard | 3/5 | tool_called:search_flights (0.6) | 0.75 | $0.24 |
| ... | ... | ... | ... | ... | ... |

## 高方差断言（一致性 < 0.6）

| Case | 断言 | 一致性 | 说明 |
|------|------|--------|------|
| hard-round-trip | tool_called:search_flights | 0.6 | 3/5 次调用了航班搜索 |

## 成本与延迟统计

| Case | 成本 min | 成本 max | 成本 stddev | 延迟 mean | 延迟 stddev |
|------|---------|---------|------------|----------|------------|
| ... | ... | ... | ... | ... | ... |
```

---

## 4. 执行脚本

### 4.1 `scripts/eval-stability.py` — 新建

```
用法：
  python scripts/eval-stability.py --k 3 --base-url http://127.0.0.1:8000
  python scripts/eval-stability.py --cases easy-tokyo-5d,hard-round-trip --k 5
  python scripts/eval-stability.py --difficulty easy,medium --k 3
```

参数：
- `--k`：每个 case 运行次数，默认 3
- `--base-url`：后端地址，默认 `http://127.0.0.1:8000`
- `--cases`：逗号分隔的 case ID 列表，默认 all
- `--difficulty`：按难度过滤，逗号分隔
- `--output`：输出路径前缀，默认 `docs/eval-stability-report`

脚本流程：
1. 加载 golden cases（复用 `runner.load_golden_cases`）
2. 按 `--cases` 或 `--difficulty` 过滤
3. 构建 live executor（复用 `scripts/failure-analysis/run_and_analyze.py` 中的会话创建 + SSE 收集逻辑，提取为可导入函数）
4. 调用 `run_stability_suite(cases, executor, k)`
5. 调用 `save_stability_report` 输出报告
6. 打印摘要到 stdout
7. 如果有 `highly_unstable_cases`，exit code = 1（可选用于 CI）

### 4.2 关于 executor 复用

`scripts/failure-analysis/run_and_analyze.py` 中有 `create_session` + `run_scenario` 逻辑。为避免跨脚本 import 复杂度，stability 脚本自包含一个轻量 executor：
- 创建会话 → 逐条发送 case.messages → 收集 SSE → 提取 tool_calls + state + stats
- 逻辑与 `run_and_analyze.py` 类似但独立，不 import 对方

---

## 5. 文件清单

| 文件 | 改动类型 | 内容 |
|------|---------|------|
| `backend/evals/models.py` | 修改（末尾追加） | `StabilityMetrics` + `StabilitySuiteResult` 数据类 |
| `backend/evals/stability.py` | 新建 | `run_stability` + `run_stability_suite` + `save_stability_report` |
| `scripts/eval-stability.py` | 新建 | CLI 脚本，参数解析 + live executor + 报告生成 |
| `backend/tests/test_stability.py` | 新建 | 用 mock executor 测试统计计算逻辑 |

**不碰的文件**：`backend/evals/runner.py`（只导入其函数，不修改）、`backend/harness/`、`backend/tools/`、`backend/main.py`、`frontend/`。

---

## 6. 测试策略

### 6.1 test_stability.py

用 mock executor 返回预设的 CaseResult，测试统计计算的正确性：

| 测试场景 | 输入 | 期望 |
|---------|------|------|
| k=3 全部通过 | 3 个 passed=True 的 CaseResult | pass_rate=1.0, unstable_cases=[] |
| k=5 部分通过 | 3 passed + 2 failed | pass_rate=0.6, assertion_consistency 反映具体断言差异 |
| k=1 | 1 个 CaseResult | pass_rate=1.0 或 0.0, stddev=0 |
| 工具调用一致 | 3 次都调了 [web_search, update_plan_state] | tool_overlap_ratio=1.0 |
| 工具调用不一致 | 第 1 次 [web_search], 第 2 次 [web_search, search_flights], 第 3 次 [search_flights] | tool_overlap_ratio=0 (交集空) / 并集 2 → 0.0 |
| 成本统计 | costs=[0.1, 0.2, 0.3] | mean=0.2, stddev≈0.1 |
| Suite 汇总 | 2 个 case，pass_rate 分别 1.0 和 0.4 | overall=0.7, highly_unstable=[case2] |

---

## 7. 验收标准

1. `pytest backend/tests/test_stability.py` 全部通过
2. `pytest backend/` 全量回归无新增失败
3. `python scripts/eval-stability.py --help` 正常输出使用说明
4. mock 模式下 `scripts/eval-stability.py` 能生成 JSON + Markdown 报告
5. StabilityMetrics 的所有统计字段计算正确（有测试覆盖）
