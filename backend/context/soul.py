from __future__ import annotations

import re
from collections.abc import Mapping

_SECTION_START_RE = re.compile(r"^<!--\s*soul:(?P<key>[^>]+?)\s*-->$")
_SECTION_END_RE = re.compile(r"^<!--\s*/soul:(?P<key>[^>]+?)\s*-->$")


def parse_soul_sections(markdown: str) -> dict[str, str]:
    """Parse marker-delimited soul sections from markdown.

    Markers are HTML comments so the file remains readable as normal markdown:
    ``<!-- soul:core -->`` ... ``<!-- /soul:core -->``.
    """
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []

    for line in markdown.splitlines():
        stripped = line.strip()
        start = _SECTION_START_RE.match(stripped)
        if start:
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = start.group("key").strip()
            current_lines = []
            continue

        end = _SECTION_END_RE.match(stripped)
        if end and current_key is not None:
            sections[current_key] = "\n".join(current_lines).strip()
            current_key = None
            current_lines = []
            continue

        if current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    return {key: value for key, value in sections.items() if value}


def select_soul_sections(
    sections: Mapping[str, str],
    *,
    phase: int | None,
    phase2_step: str | None = None,
) -> list[str]:
    keys = ["core"]
    if phase == 2:
        step = phase2_step or "brief"
        keys.extend(["phase:2", f"phase:2:{step}"])
    elif phase is not None:
        keys.append(f"phase:{phase}")

    return [sections[key] for key in keys if key in sections and sections[key].strip()]
