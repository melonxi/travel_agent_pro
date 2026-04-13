from __future__ import annotations

import importlib.util
from pathlib import Path

from evals.failure_report import ScenarioResult


ROOT_DIR = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT_DIR / "scripts" / "failure-analysis" / "run_and_analyze.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("failure_analysis_runner", RUNNER_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_result_json_scrubs_xiaohongshu_xsec_tokens():
    runner = _load_runner()
    result = ScenarioResult(
        scenario_id="failure-token",
        name="token scrub",
        user_input="demo",
        passed_assertions=1,
        total_assertions=1,
        responses=[
            "https://www.xiaohongshu.com/explore/abc?xsec_token=SECRET&xsec_source=pc_search"
        ],
        stats={
            "messages": [
                {
                    "content": (
                        '{"url":"https://www.xiaohongshu.com/explore/abc?'
                        'xsec_token=SECRET&xsec_source=pc_search"}'
                    )
                }
            ],
        },
    )

    payload = runner.result_to_json(result)
    rendered = str(payload)

    assert "SECRET" not in rendered
    assert "xsec_token=<redacted>" in rendered


def test_runner_exit_code_only_fails_for_fatal_errors():
    runner = _load_runner()
    assertion_failure = ScenarioResult(
        scenario_id="failure-assertion",
        name="expected product boundary",
        user_input="demo",
        passed_assertions=0,
        total_assertions=1,
        failures=["[contains_text] expected boundary"],
    )
    fatal_failure = ScenarioResult(
        scenario_id="failure-fatal",
        name="runner broke",
        user_input="demo",
        passed_assertions=0,
        total_assertions=1,
        failures=["FATAL: backend unavailable"],
    )

    assert runner.exit_code_for_results([assertion_failure]) == 0
    assert runner.exit_code_for_results([fatal_failure]) == 1
