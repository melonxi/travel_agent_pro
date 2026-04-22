from __future__ import annotations

from pathlib import Path
import re


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent


def test_legacy_v2_memory_models_are_removed_from_runtime_code():
    assert not (BACKEND_ROOT / "memory" / "models.py").exists()
    assert not (BACKEND_ROOT / "tests" / "test_memory_models.py").exists()

    forbidden_patterns = (
        r"\bfrom\s+memory\.models\b",
        r"\bimport\s+memory\.models\b",
        r"\bMemoryCandidate\b",
        r"\bMemoryItem\b",
        r"\bTripEpisode\b",
        r"\bFileMemoryStore\b",
        r"\bMemoryRetriever\b",
        r"\brecall_query_adapter\b",
        r"\bplan_to_legacy_recall_query\b",
    )
    scanned_roots = (
        BACKEND_ROOT / "agent",
        BACKEND_ROOT / "harness",
        BACKEND_ROOT / "memory",
        BACKEND_ROOT / "state",
        BACKEND_ROOT / "storage",
        BACKEND_ROOT / "tools",
        BACKEND_ROOT / "main.py",
        REPO_ROOT / "scripts",
    )

    offenders: list[str] = []
    for root in scanned_roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else root.rglob("*.py")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden_patterns:
                if re.search(pattern, text):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)} matches {pattern}"
                    )

    assert offenders == []
