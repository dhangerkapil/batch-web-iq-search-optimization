"""Option 1: batch lines in which the model searches with the built-in web_search tool, capped or not.

bounded-web-search (option 1) caps and steers the search; web-search, the common starting point, doesn't, and is kept
to measure a job as it runs today.
"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

from workloads import Workload


def web_search_line(
    workload: Workload,
    rec: dict,
    *,
    deployment: str,
    bounded: bool,
    max_tool_calls: int = 2,
    reasoning_effort: str = "low",
    max_output_tokens: int = 16000,
) -> dict:
    """A line where the model searches with the built-in web_search tool.

    Unbounded: the common starting point - a prompt that says "use web search", no caps, no schema and no reasoning effort, so the model's default applies ("none" on gpt-5.4 and gpt-5.4-mini).
    Bounded: the cheapest change that keeps search inside the batch - max_tool_calls, low reasoning
    effort, a strict schema, user_location and a search budget in the prompt.
    """
    system, user = workload.web_search_prompts(rec, bounded)
    tool: dict = {"type": "web_search"}
    body: dict = {
        "model": deployment,
        "input": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "tools": [tool],
        "max_output_tokens": max_output_tokens,
        "store": False,
    }
    if bounded:
        country = workload.user_country(rec)
        if country:
            tool["user_location"] = {"type": "approximate", "country": country}
        body["max_tool_calls"] = max_tool_calls
        body["reasoning"] = {"effort": reasoning_effort}
        body["text"] = {"format": workload.output_format()}
    return {"custom_id": workload.record_id(rec), "method": "POST", "url": "/v1/responses", "body": body}
