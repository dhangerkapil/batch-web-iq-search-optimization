"""What every design's batch lines share: the design names, and the prompt text of a line as counted for forecasts.

Each design builds its own lines: option-1-bounded-web-search/web_search_lines.py (the model searches) and
option-2-two-stage-web-iq/two_stage_lines.py (evidence in, no tools).
"""
from __future__ import annotations

DESIGNS = ("two-stage", "web-search", "bounded-web-search")


def line_text(line: dict) -> str:
    """System plus user text of a line, as counted for token forecasts."""
    parts = []
    for m in line["body"].get("input") or []:
        c = m.get("content")
        parts.append(c if isinstance(c, str) else "".join(p.get("text", "") for p in c or [] if isinstance(p, dict)))
    return "\n".join(parts)
