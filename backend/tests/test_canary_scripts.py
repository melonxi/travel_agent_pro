from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_full_phase_canary_grade_report_marks_ran_and_counts_failures():
    module = _load_script("run-full-phase-canary.py")

    report = module._grade_report(
        True,
        [
            {"rubric_id": "ok", "status": "pass"},
            {"rubric_id": "bad", "status": "fail"},
        ],
    )

    assert report == {
        "grader_ran": True,
        "grade_count": 2,
        "grade_fails": ["bad"],
    }


def test_adaptive_canary_grade_report_marks_not_ran():
    module = _load_script("run-adaptive-canary.py")

    report = module._grade_report(False, [])

    assert report == {
        "grader_ran": False,
        "grade_count": 0,
        "grade_fails": [],
    }
