"""One request per line: send batch-format lines as single Responses API requests, and measure what happened inside each.

A line's body is the JSON a Global Batch job runs. Sent on its own to the Responses API, it's one HTTPS request: the
model runs as many web searches as it decides to inside that request, and one response comes back. Nothing here answers
tool calls or loops: the web_search tool runs on the service side
(https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search).

send() uses flex.py's rules for errors: HTTP 408, 429 and 5xx can be retried (only with --retries), honoring Retry-After;
a request that timed out or lost its connection is never sent again, because the service may have processed and billed
it. measure() reads a response with shared/batch.py's response_metrics, the code the Batch and Flex paths use, and keeps
only METRIC_FIELDS: counts, tokens, times and statuses. Response text, URLs and response IDs never reach metrics.jsonl
or summary.md. Their rows are keyed by each line's custom_id, so look at those before you share the files.
"""
from __future__ import annotations

import copy
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

import batch as batchlib
import folders

RETRY_STATUS = (408, 429, 500, 502, 503, 504)
# Errors every request would get: they stop the run instead of failing each line in turn.
STOP_STATUS = (401, 403, 404)
WEB_SEARCH_TOOLS = ("web_search", "web_search_preview")
SOURCES = "web_search_call.action.sources"
# Output items the client would have to run or answer itself. With only the built-in web_search tool there are none.
CLIENT_TOOL_ITEMS = ("function_call", "custom_tool_call", "computer_call", "local_shell_call", "mcp_approval_request")
# Everything metrics.jsonl and summary.md may hold.
METRIC_FIELDS = (
    "id", "mode", "status", "http_status", "http_requests", "seconds", "held", "error_kind", "service_tier",
    "web_search_calls", "web_search_actions", "web_search_queries", "web_search_requests", "sources", "url_citations",
    "client_tool_calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "json_valid",
)


class Stop(Exception):
    """An error every request of the run would get (sign-in, permission, a missing deployment, Flex not supported).
    outcome is what the request that got it records."""

    def __init__(self, message: str, outcome: dict | None = None):
        super().__init__(message)
        self.outcome = outcome


def _flex():
    """flex.py's error rules: Retry-After, backoff with jitter, and which errors mean "no answer, maybe billed"."""
    return folders.load(folders.FLEX, "flex")


def apply_overrides(body: dict, *, deployment: str | None = None, tool: str | None = None, max_tool_calls: int | None = None,
                    reasoning_effort: str | None = None, max_output_tokens: int | None = None, service_tier: str | None = None,
                    count_sources: bool = False) -> tuple[dict, list[str]]:
    """The request for a line: a copy of its body with only the changes asked for, and what each one changed.

    store is always false, so the service doesn't keep the response; a line that doesn't say so gets it as a change."""
    b = copy.deepcopy(body)
    changes: list[str] = []

    def put(key, value):
        if b.get(key) != value:
            changes.append(f"{key}: {b.get(key)!r} -> {value!r}")
            b[key] = value

    if deployment:
        put("model", deployment)
    if tool:
        for t in b.get("tools") or []:
            if t.get("type") in WEB_SEARCH_TOOLS and t["type"] != tool:
                changes.append(f"tool: {t['type']!r} -> {tool!r}")
                t["type"] = tool
    if max_tool_calls is not None:
        put("max_tool_calls", max_tool_calls)
    if reasoning_effort:
        put("reasoning", {**(b.get("reasoning") or {}), "effort": reasoning_effort})
    if max_output_tokens is not None:
        put("max_output_tokens", max_output_tokens)
    if service_tier:
        put("service_tier", service_tier)
    if count_sources and SOURCES not in (b.get("include") or []):
        put("include", [*(b.get("include") or []), SOURCES])
    put("store", False)
    return b, changes


def send(client, request: dict, *, retries: int = 0, extra_body: dict | None = None, sleep=time.sleep, stop=None) -> dict:
    """Send one request with responses.create. Returns the response (a dict) or what went wrong, and how many HTTP
    requests that took: one, unless retries allows another try after HTTP 408, 429 or 5xx. stop, a threading.Event, ends
    the wait before a retry when the run stops, and then the request isn't sent again."""
    fx = _flex()
    t0, attempts = time.monotonic(), 0
    kwargs = {"extra_body": extra_body} if extra_body else {}
    while True:
        attempts += 1
        try:
            resp = client.responses.create(**request, **kwargs)
        except Exception as err:
            status, kind = fx._http_status(err), fx._kind(err)
            done = {"http_status": status, "http_requests": attempts, "seconds": round(time.monotonic() - t0, 2), "response": None}
            if kind == "auth" or status in STOP_STATUS or fx._flex_unsupported(status, str(err), request.get("model") or ""):
                error = f"HTTP {status or ''} {type(err).__name__}: {str(err)[:300]}".replace("HTTP  ", "")
                raise Stop(error, {**done, "held": False, "error_kind": f"http_{status}" if status else kind, "error": error}) from err
            if kind in ("timeout", "connection"):
                # The service may have processed it, and billed it, without the answer reaching us: never resent here.
                return {**done, "held": True, "error_kind": kind, "error": str(err)[:300]}
            if status in RETRY_STATUS and attempts <= retries:
                wait = fx.retry_after(err)
                pause = fx.backoff(attempts) if wait is None else wait
                if stop is None:
                    sleep(pause)
                    continue
                if not stop.wait(pause):  # True as soon as the run stops: no more tries
                    continue
            return {**done, "held": False, "error_kind": f"http_{status}" if status else type(err).__name__, "error": str(err)[:300]}
        body = resp if isinstance(resp, dict) else resp.model_dump()
        return {"http_status": 200, "http_requests": attempts, "seconds": round(time.monotonic() - t0, 2), "response": body,
                "held": False, "error_kind": None, "error": None}


def _queries(action: dict) -> int:
    """Queries in one search action: its query list, or its single query."""
    return max(len(action.get("queries") or []), 1 if action.get("query") else 0)


def measure(line_id: str, mode: str, outcome: dict) -> dict:
    """The shareable numbers for one request. web_search_requests, the billing count, is None when the response has no
    tool_usage (responses through a Foundry project endpoint, with or without an agent, didn't carry it): not 0."""
    row = {k: outcome.get(k) for k in ("http_status", "http_requests", "seconds", "held", "error_kind")}
    row.update(id=line_id, mode=mode)
    body = outcome.get("response")
    if body is not None:
        m = batchlib.response_metrics(body)
        items = body.get("output") or []
        actions = [(i.get("action") or {}) for i in items if i.get("type") == "web_search_call"]
        row.update(
            status=m["status"],
            service_tier=m["service_tier"],
            web_search_calls=m["web_search_calls"],
            web_search_actions=m["web_search_actions"],
            web_search_queries=sum(_queries(a) for a in actions if a.get("type") == "search"),
            web_search_requests=m["web_search_requests"] if isinstance(body.get("tool_usage"), dict) else None,
            sources=sum(len(a.get("sources") or []) for a in actions),
            url_citations=sum(1 for i in items if i.get("type") == "message" for p in i.get("content") or []
                              for an in p.get("annotations") or [] if an.get("type") == "url_citation"),
            client_tool_calls=sum(1 for i in items if i.get("type") in CLIENT_TOOL_ITEMS),
            input_tokens=m["input_tokens"],
            cached_tokens=m["cached_tokens"],
            output_tokens=m["output_tokens"],
            reasoning_tokens=m["reasoning_tokens"],
            json_valid=m["json_valid"],
        )
    return {k: row.get(k) for k in METRIC_FIELDS}


def _spread(values: list) -> dict | None:
    nums = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return None
    return {"min": min(nums), "median": statistics.median(nums), "max": max(nums), "total": round(sum(nums), 2)}


def summarize(rows: list[dict]) -> dict:
    """What a run shows, over its rows: one request each, the searches inside them, and their size."""
    answered = [r for r in rows if r.get("status") is not None]
    actions: dict[str, int] = {}
    for r in answered:
        for k, v in (r.get("web_search_actions") or {}).items():
            actions[k] = actions.get(k, 0) + v
    billed = [r.get("web_search_requests") for r in answered]
    reported = bool(answered) and all(b is not None for b in billed)
    return {
        "lines": len(rows),
        "answered": len(answered),
        "completed": sum(1 for r in answered if r.get("status") == "completed"),
        "held": sum(1 for r in rows if r.get("held")),
        "failed": sum(1 for r in rows if r.get("status") is None and not r.get("held")),
        "http_requests": sum(r.get("http_requests") or 0 for r in rows),
        "one_request_each": all(r.get("http_requests") == 1 for r in rows),
        "client_tool_calls": sum(r.get("client_tool_calls") or 0 for r in answered),
        "web_search_actions": actions,
        "web_search_calls": _spread([r.get("web_search_calls") for r in answered]),
        "web_search_queries": _spread([r.get("web_search_queries") for r in answered]),
        "web_search_requests": _spread(billed) if reported else None,
        "billed_requests_reported": reported,
        "seconds": _spread([r.get("seconds") for r in answered]),
        "input_tokens": _spread([r.get("input_tokens") for r in answered]),
        "output_tokens": _spread([r.get("output_tokens") for r in answered]),
    }


def _cell(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, dict):
        return ", ".join(f"{k} {n}" for k, n in sorted(v.items())) or "0"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _spread_text(d: dict | None) -> str:
    return "-" if not d else f"{_cell(d['min'])} to {_cell(d['max'])} (median {_cell(d['median'])}, total {_cell(d['total'])})"


def table(rows: list[dict]) -> str:
    """One row per request: what was sent, what came back, and the searches inside it."""
    head = ("Line", "Status", "HTTP requests", "Search calls (actions)", "Queries", "Billed search requests", "Input tokens",
            "Output tokens", "Seconds")
    out = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    for r in rows:
        answered = r.get("status") is not None
        if answered:
            status = str(r["status"])
        else:
            status = f"{'held' if r.get('held') else 'failed'}: {r.get('error_kind')}"
        calls = f"{r.get('web_search_calls')} ({_cell(r.get('web_search_actions'))})" if answered else "-"
        billed = "not reported" if answered and r.get("web_search_requests") is None else _cell(r.get("web_search_requests"))
        out.append("| " + " | ".join([
            _cell(r.get("id")), status, _cell(r.get("http_requests")), calls, _cell(r.get("web_search_queries")), billed,
            _cell(r.get("input_tokens")), _cell(r.get("output_tokens")), _cell(r.get("seconds")),
        ]) + " |")
    return "\n".join(out)


def summary_markdown(run: dict, rows: list[dict]) -> str:
    """summary.md: the run's settings that matter, the per-request table and what it shows. Counts only."""
    s = summarize(rows)
    billed = _spread_text(s["web_search_requests"]) if s["billed_requests_reported"] else "not reported in these responses; read them on the bill"
    agent = " (a prompt agent with the web_search tool attached directly)" if run.get("mode") == "agent" else ""
    lines = [
        f"# One request per line: {run.get('mode')} mode",
        "",
        f"Run {run.get('finished_utc') or run.get('started_utc')}. {s['lines']} lines, sent as {s['http_requests']} HTTP "
        f"request(s): {s['completed']} completed, {s['held']} held, {s['failed']} failed.",
        "",
        "| What | Result |",
        "| --- | --- |",
        f"| Mode | {run.get('mode')}{agent} |",
        f"| Model, as the responses report it | {', '.join(run.get('models') or []) or '-'} |",
        f"| Service tier, as the responses report it | {', '.join(run.get('service_tiers') or []) or '-'} |",
        f"| Web search tool in the request | {', '.join(run.get('tools') or []) or '-'} |",
        f"| One HTTP request per line | {'yes' if s['one_request_each'] else 'no: see HTTP requests (retries)'} |",
        f"| Tool calls the client had to answer | {s['client_tool_calls']} |",
        f"| Web search calls per response | {_spread_text(s['web_search_calls'])} |",
        f"| Search actions, all responses | {_cell(s['web_search_actions'])} |",
        f"| Search queries per response | {_spread_text(s['web_search_queries'])} |",
        f"| Billed web search requests per response | {billed} |",
        f"| Input tokens per response | {_spread_text(s['input_tokens'])} |",
        f"| Seconds per response | {_spread_text(s['seconds'])} |",
        "",
        table(rows),
        "",
        "Billed web search requests are `tool_usage.web_search.num_requests`, the count Learn says web search is billed on; "
        "counting `web_search_call` items doesn't give the bill "
        "(https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing).",
    ]
    if run.get("changes"):
        lines += ["", "Changes made to the lines before sending them: " + "; ".join(run["changes"]) + "."]
    return "\n".join(lines) + "\n"
