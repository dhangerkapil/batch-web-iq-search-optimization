"""One request per line: send batch-format lines as single Responses API requests, and measure what happened inside each.

A line's body is the JSON a Global Batch job runs. Sent on its own to the Responses API, it's one HTTPS request: the
model runs as many web searches as it decides to inside that request, and one response comes back. Nothing here answers
tool calls or loops: the web_search tool runs on the service side
(https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search).

send() follows Learn's guidance for Flex processing and rate limits
(https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#handle-flex-errors,
https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#rate-limit-best-practices) with flex.py's rules: HTTP
408, 429 and 5xx are retried a bounded number of times, after exponential backoff with jitter or the Retry-After the
service asks for, whichever is longer, within a wait budget; a request that timed out or lost its connection is never
sent again, because the service may have processed and billed it. Each line keeps the rate-limit headers of its last
response and the cause of its HTTP 429s. measure() reads a response with shared/batch.py's response_metrics, the code the
Batch and Flex paths use, and keeps only METRIC_FIELDS: counts, tokens, times, statuses and service tiers. Response text,
URLs, response IDs and request IDs never reach metrics.jsonl or summary.md. Their rows are keyed by each line's
custom_id, so look at those before you share the files.
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
from settings import SETTINGS

RETRY_STATUS = (408, 429, 500, 502, 503, 504)
# Errors every request would get: they stop the run instead of failing each line in turn.
STOP_STATUS = (401, 403, 404)
# The longest Retry-After waited out before another try: flex.py's [flex] max_server_wait_seconds.
MAX_SERVER_WAIT = float(SETTINGS["flex"]["max_server_wait_seconds"])
WEB_SEARCH_TOOLS = ("web_search", "web_search_preview")
SOURCES = "web_search_call.action.sources"
# Output items the client would have to run or answer itself. With only the built-in web_search tool there are none.
CLIENT_TOOL_ITEMS = ("function_call", "custom_tool_call", "computer_call", "local_shell_call", "mcp_approval_request")
# The rate-limit headers every response carries (Learn, "Rate limit response headers"): numbers, kept in metrics.jsonl.
RATE_HEADERS = {
    "ratelimit_limit_tokens": "x-ratelimit-limit-tokens",
    "ratelimit_remaining_tokens": "x-ratelimit-remaining-tokens",
    "ratelimit_limit_requests": "x-ratelimit-limit-requests",
    "ratelimit_remaining_requests": "x-ratelimit-remaining-requests",
}
# Request IDs, for a support request: kept in responses.jsonl only, because they identify requests.
ID_HEADERS = ("x-request-id", "apim-request-id")
# Everything metrics.jsonl and summary.md may hold.
METRIC_FIELDS = (
    "id", "mode", "status", "http_status", "http_requests", "seconds", "held", "error_kind", "service_tier",
    "web_search_calls", "web_search_actions", "web_search_queries", "web_search_requests", "sources", "url_citations",
    "client_tool_calls", "input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "json_valid",
    "tier_requested", "fallback", "throttled_attempts", "throttle", "error_code", "retry_after_seconds", "waited_seconds",
    *RATE_HEADERS,
)


class Stop(Exception):
    """An error every request of the run would get (sign-in, permission, a missing deployment, Flex not supported, an
    unexpected error). outcome is what the request that got it records; kind says which."""

    def __init__(self, message: str, outcome: dict | None = None, kind: str | None = None):
        super().__init__(message)
        self.outcome = outcome
        self.kind = kind


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


def _headers(obj) -> dict:
    """The HTTP headers of a raw response or of an HTTP error, with lower-case names; {} when there are none."""
    headers = getattr(obj, "headers", None)
    if headers is None:
        headers = getattr(getattr(obj, "response", None), "headers", None)
    try:
        return {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    except Exception:
        return {}


def _number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return int(n) if n.is_integer() else n


def rate_limits(headers: dict) -> dict:
    """The rate-limit headers, as numbers (None when a header is missing, as it may be through a project endpoint)."""
    return {field: _number(headers.get(name)) for field, name in RATE_HEADERS.items()}


def throttle(status: int | None, text: str) -> str | None:
    """For an HTTP 429, its likely cause read from the error message, after Learn's table of 429 types: rate_limit (the
    deployment's tokens or requests per minute: more quota helps), capacity (the service or Flex capacity is busy:
    waiting helps, more quota doesn't), or unknown. Read x-ratelimit-limit-tokens too: below the deployment's quota, the
    limit is lowered for a while (https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#types-of-429-errors)."""
    if status != 429:
        return None
    low = text.lower()
    if "rate limit" in low or "have been limited" in low:
        return "rate_limit"
    if any(s in low for s in ("temporarily unable", "high demand", "capacity", "unavailable")):
        return "capacity"
    return "unknown"


def _code(err: BaseException) -> str | None:
    code = getattr(err, "code", None)
    return str(code)[:80] if code else None


def send(client, request: dict, *, retries: int = 0, extra_body: dict | None = None, sleep=time.sleep, stop=None,
         budget: float | None = None, deployment: str | None = None) -> dict:
    """Send one request with responses.create, one HTTPS request per try. Returns the response (a dict) or what went
    wrong, how many HTTP requests that took, the rate-limit headers of the last one, and the request IDs of each.

    Retries: only after HTTP 408, 429 or 5xx, at most `retries` more tries, each after max(backoff with jitter,
    Retry-After). A Retry-After longer than [flex] max_server_wait_seconds isn't waited out, and a line's waits stay
    within `budget` seconds in all: past either, the line fails with its last error, so nothing waits without a bound.
    stop, a threading.Event, ends a wait when the run stops, and the request isn't sent again. A request that timed out,
    lost its connection or failed with an unexpected error is never sent again: the service may have processed it.
    deployment, when the request doesn't name it (agent mode), keeps its name from being read as part of an error."""
    fx = _flex()
    t0, attempts, planned, waited, refused = time.monotonic(), 0, 0.0, 0.0, 0
    cause: str | None = None
    kwargs = {"extra_body": extra_body} if extra_body else {}
    ids: list[dict] = []
    tier = request.get("service_tier")

    def outcome(**more) -> dict:
        return {"http_requests": attempts, "seconds": round(time.monotonic() - t0, 2), "waited_seconds": round(waited, 1),
                "tier_requested": tier, "fallback": False, "throttled_attempts": refused, "throttle": cause,
                "request_ids": ids, **more}

    while True:
        attempts += 1
        try:
            raw = client.responses.with_raw_response.create(**request, **kwargs)
        except Exception as err:
            status, kind = fx._http_status(err), fx._kind(err)
            headers = _headers(err)
            if kind == "auth":
                attempts -= 1  # no token, so this try sent nothing
            else:
                ids.append({"http_status": status, **{h: headers.get(h) for h in ID_HEADERS}})
            text = f"{type(err).__name__}: {err}"
            if status == 429:
                refused, cause = refused + 1, throttle(status, text)
            server = fx.retry_after(err)
            failed = outcome(http_status=status, response=None, error_code=_code(err),
                             retry_after_seconds=None if server is None else round(server, 3), **rate_limits(headers))
            error = f"HTTP {status} {text[:300]}" if status else text[:300]
            if fx._flex_unsupported(status, text, request.get("model") or deployment or ""):
                hint = ("Flex processing isn't available for this request: use a deployment of a model that supports it, "
                        "such as gpt-6-sol, or --service-tier default for Standard")
                raise Stop(f"{error} ({hint})", {**failed, "held": False, "error_kind": "flex_unsupported", "error": error},
                           "flex_unsupported") from err
            if kind == "auth" or status in STOP_STATUS:
                raise Stop(error, {**failed, "held": False, "error_kind": f"http_{status}" if status else kind, "error": error},
                           "access") from err
            if kind in ("timeout", "connection"):
                # The service may have processed it, and billed it, without the answer reaching us: never resent here.
                return {**failed, "held": True, "error_kind": kind, "error": error}
            if status is None:
                # Not an HTTP error: whether the request reached the service is unknown. flex.py holds such a line and
                # stops the job; so does this.
                raise Stop(f"an unexpected error: {error}. Whether that request reached the service is unknown, so its line "
                           "is held; nothing more is sent", {**failed, "held": True, "error_kind": "exception", "error": error},
                           "exception") from err
            if status in RETRY_STATUS and attempts <= retries:
                pause = max(fx.backoff(attempts), server or 0.0)
                if (server is None or server <= MAX_SERVER_WAIT) and (budget is None or planned + pause <= budget):
                    planned += pause
                    w0 = time.monotonic()
                    if stop is None:
                        sleep(pause)
                    elif stop.wait(pause):  # True as soon as the run stops: no more tries
                        waited += time.monotonic() - w0
                        return {**outcome(http_status=status, response=None, error_code=_code(err),
                                          retry_after_seconds=failed["retry_after_seconds"], **rate_limits(headers)),
                                "held": False, "error_kind": f"http_{status}", "error": error}
                    waited += time.monotonic() - w0
                    continue
            return {**failed, "held": False, "error_kind": f"http_{status}", "error": error}
        headers = _headers(raw)
        status = getattr(raw, "status_code", 200)
        ids.append({"http_status": status, **{h: headers.get(h) for h in ID_HEADERS}})
        try:
            body = raw.parse()
            body = body if isinstance(body, dict) else body.model_dump()
        except Exception as err:
            # The service answered, and may have billed it, but the answer can't be read: held, and the run stops, as in
            # flex.py. The request IDs and headers are kept, for a support request.
            error = f"the response couldn't be read: {type(err).__name__}: {str(err)[:300]}"
            raise Stop(error, outcome(http_status=status, response=None, held=True, error_kind="unreadable", error=error,
                                      error_code=None, retry_after_seconds=None, **rate_limits(headers)), "exception") from err
        return outcome(http_status=status, response=body, held=False, error_kind=None, error=None, error_code=None,
                       retry_after_seconds=None, **rate_limits(headers))


def falls_back(outcome: dict) -> bool:
    """Whether --fallback-default sends this line again on Standard: Flex was asked for, and Flex refused it with an error
    retrying may fix (HTTP 408, 429 or 5xx, after its retries) or doesn't take this line (HTTP 400, Flex not supported)
    while it took other lines. Not after a 429 whose message reads as a quota limit: Flex and Standard share the
    deployment's quota, so Standard would likely be refused too, and Learn says to limit fallback to capacity-related
    responses (https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#fall-back-to-standard-processing).
    Never after a timeout or a dropped connection: that request may have been processed."""
    if outcome.get("tier_requested") != "flex" or outcome.get("response") is not None or outcome.get("held"):
        return False
    kind = str(outcome.get("error_kind") or "")
    if kind == "http_429" and outcome.get("throttle") == "rate_limit":
        return False
    return kind == "flex_unsupported" or kind in {f"http_{s}" for s in RETRY_STATUS}


def with_fallback(first: dict, second: dict) -> dict:
    """A line sent again with service_tier "default" after Flex refused it: the second send's result, with both sends'
    requests, times, refusals and request IDs. tier_requested stays what the run asked for; service_tier in the metrics
    is what the response reports."""
    return {**second, "fallback": True, "tier_requested": first.get("tier_requested"),
            "http_requests": (first.get("http_requests") or 0) + (second.get("http_requests") or 0),
            "seconds": round((first.get("seconds") or 0) + (second.get("seconds") or 0), 2),
            "waited_seconds": round((first.get("waited_seconds") or 0) + (second.get("waited_seconds") or 0), 1),
            "throttled_attempts": (first.get("throttled_attempts") or 0) + (second.get("throttled_attempts") or 0),
            "throttle": second.get("throttle") or first.get("throttle"),
            "request_ids": [*(first.get("request_ids") or []), *(second.get("request_ids") or [])]}


def _queries(action: dict) -> int:
    """Queries in one search action: its query list, or its single query."""
    return max(len(action.get("queries") or []), 1 if action.get("query") else 0)


def measure(line_id: str, mode: str, outcome: dict) -> dict:
    """The shareable numbers for one request. web_search_requests, the billing count, is None when the response has no
    tool_usage (responses through a Foundry project endpoint, with or without an agent, didn't carry it): not 0."""
    row = {k: outcome.get(k) for k in ("http_status", "http_requests", "seconds", "held", "error_kind", "tier_requested",
                                       "fallback", "throttled_attempts", "throttle", "error_code", "retry_after_seconds",
                                       "waited_seconds", *RATE_HEADERS)}
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
    """What a run shows, over its rows: one request each, the searches inside them, their size, and the tiers."""
    answered = [r for r in rows if r.get("status") is not None]
    actions: dict[str, int] = {}
    for r in answered:
        for k, v in (r.get("web_search_actions") or {}).items():
            actions[k] = actions.get(k, 0) + v
    tiers: dict[str, int] = {}
    for r in answered:
        tiers[str(r.get("service_tier"))] = tiers.get(str(r.get("service_tier")), 0) + 1
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
        "tiers_requested": sorted({str(r.get("tier_requested")) for r in rows if r.get("tier_requested")}),
        "service_tiers": tiers,
        "fallbacks": sum(1 for r in rows if r.get("fallback")),
        # Flex asked for, another tier reported, and not a fallback: Learn says to track the processed tier.
        "other_tier": sum(1 for r in answered if r.get("tier_requested") == "flex" and r.get("service_tier") != "flex"
                          and not r.get("fallback")),
        "throttled_attempts": sum(r.get("throttled_attempts") or 0 for r in rows),
        "waited_seconds": round(sum(r.get("waited_seconds") or 0 for r in rows), 1),
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


def _tier(r: dict) -> str:
    tier = r.get("service_tier") if r.get("status") is not None else r.get("tier_requested")
    return f"{_cell(tier)} (fallback)" if r.get("fallback") else _cell(tier)


def table(rows: list[dict]) -> str:
    """One row per request: what was sent, what came back, and the searches inside it."""
    head = ("Line", "Status", "Tier", "HTTP requests", "Refused (429)", "Search calls (actions)", "Queries",
            "Billed search requests", "Input tokens", "Output tokens", "Seconds")
    out = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    for r in rows:
        answered = r.get("status") is not None
        if answered:
            status = str(r["status"])
        else:
            status = f"{'held' if r.get('held') else 'failed'}: {r.get('error_kind')}"
        calls = f"{r.get('web_search_calls')} ({_cell(r.get('web_search_actions'))})" if answered else "-"
        billed = "not reported" if answered and r.get("web_search_requests") is None else _cell(r.get("web_search_requests"))
        refused = _cell(r.get("throttled_attempts")) + (f" ({r['throttle']})" if r.get("throttle") else "")
        out.append("| " + " | ".join([
            _cell(r.get("id")), status, _tier(r), _cell(r.get("http_requests")), refused, calls,
            _cell(r.get("web_search_queries")), billed, _cell(r.get("input_tokens")), _cell(r.get("output_tokens")),
            _cell(r.get("seconds")),
        ]) + " |")
    return "\n".join(out)


def summary_markdown(run: dict, rows: list[dict]) -> str:
    """summary.md: the run's settings that matter, the per-request table and what it shows. Counts only."""
    s = summarize(rows)
    billed = _spread_text(s["web_search_requests"]) if s["billed_requests_reported"] else "not reported in these responses; read them on the bill"
    agent = " (a prompt agent with the web_search tool attached directly)" if run.get("mode") == "agent" else ""
    lowest = min((r["ratelimit_remaining_tokens"] for r in rows if isinstance(r.get("ratelimit_remaining_tokens"), (int, float))),
                 default=None)
    limits = sorted({r["ratelimit_limit_tokens"] for r in rows if isinstance(r.get("ratelimit_limit_tokens"), (int, float))})
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
        f"| Service tier asked for | {', '.join(s['tiers_requested']) or 'none: the deployment decides'} |",
        f"| Service tier, as the responses report it | {_cell(s['service_tiers']) if s['service_tiers'] else '-'} |",
        f"| Answered on another tier than asked, not as a fallback | {s['other_tier']} |",
        f"| Sent again with service_tier default (--fallback-default) | {s['fallbacks']} |",
        f"| Tries refused with HTTP 429, and time spent waiting to retry | {s['throttled_attempts']}; {_cell(s['waited_seconds'])} s |",
        f"| Rate limit (x-ratelimit-limit-tokens) and lowest remaining tokens seen | "
        f"{', '.join(_cell(x) for x in limits) or 'not reported'}; {_cell(lowest)} |",
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
