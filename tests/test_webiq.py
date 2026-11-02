"""The Web IQ client, offline: httpx's mock transport stands in for the toolbox and the REST API, and fakes for Web IQ."""
import argparse
import asyncio
import json

import httpx
import pytest

import webiq
from search_plan import build_plan
from workloads import get_workload

RECORDS = [{"id": "R0", "brand": "Brand 0", "market": "US", "claimed_owner": "Owner 0", "claimed_owner_hq_country": "US"}]
# How a Microsoft Foundry toolbox relays Web IQ's HTTP 429 (seen 2 Nov 2026): an HTTP 200 response whose JSON-RPC
# message has an error member, and no Retry-After header.
THROTTLED = json.dumps({"error": {"code": "auth_user_throttled", "message": "Rate limit exceeded. Please reduce your request "
                                  "frequency. (Details: Exceeded rate limit) (Retry after: 60s)"}})
RELAYED_429 = {"jsonrpc": "2.0", "id": 101, "error": {
    "code": -32008,
    "message": "Error encountered while calling tool web from remote server: https://api.microsoft.ai:443/v3/mcp. Details: Response "
               f"status code does not indicate success: 429 (Too Many Requests). Response body: {THROTTLED}",
    "data": {"grpcStatus": "ResourceExhausted"}}}


def opts(**kw):
    base = dict(via="fake", max_results=3, max_chars=200, language="en", qps=1000.0, concurrency=1, max_attempts=2, max_queries=None)
    base.update(kw)
    return argparse.Namespace(**base)


class FakeTokens:
    def __init__(self, scope, subscription=None):
        pass

    async def get(self):
        return "token"

    def reset(self):
        pass


def call(api, handler):
    """One api.call through httpx's mock transport."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await api.call(client, {"query": "q"}, 101)

    return asyncio.run(go())


@pytest.fixture
def toolbox(monkeypatch):
    monkeypatch.setattr(webiq, "_TokenCache", FakeTokens)
    return webiq.ToolboxClient("https://example.invalid/toolboxes/t/mcp?api-version=v1", tool="webiq___web")


@pytest.fixture
def pauses(monkeypatch):
    """Waits between attempts and rounds are recorded, not slept."""
    seen = []

    async def record(seconds):
        seen.append(seconds)

    monkeypatch.setattr(webiq, "_sleep", record)
    return seen


def test_a_toolbox_relayed_429_is_retried_after_the_wait_it_asks_for(toolbox):
    status, data, err, wait = call(toolbox, lambda request: httpx.Response(200, json=RELAYED_429))
    assert (status, data, wait) == (429, None, 60.0)
    assert json.dumps(RELAYED_429["error"]).index("Retry after") > 300 and len(err) == 300  # read before the text was cut short
    other = {"jsonrpc": "2.0", "id": 101, "error": {"code": -32603, "message": "Internal error"}}
    assert call(toolbox, lambda request: httpx.Response(200, json=other))[0::3] == (502, None)  # as before


def test_a_rate_limited_tool_result_gives_its_wait(toolbox):
    limited = {"jsonrpc": "2.0", "id": 101, "result": {"isError": True, "content": [{"type": "text", "text": "HTTP 429: rate limit exceeded (Retry after: 30s)"}]}}
    assert call(toolbox, lambda request: httpx.Response(200, json=limited))[0::3] == (429, 30.0)
    bad = {"jsonrpc": "2.0", "id": 101, "result": {"isError": True, "content": [{"type": "text", "text": "query is too long"}]}}
    assert call(toolbox, lambda request: httpx.Response(200, json=bad))[0::3] == (400, None)


def test_rest_reads_the_wait_from_the_429_body_when_there_is_no_header(monkeypatch):
    monkeypatch.setenv("WEBIQ_API_KEY", "not-a-real-key")
    rest = webiq.RestClient("https://example.invalid/v3/search/web")
    body = {"error": {"code": "auth_user_throttled", "message": "Rate limit exceeded", "retryAfter": "60s"}}
    assert call(rest, lambda request: httpx.Response(429, json=body))[0::3] == (429, 60.0)
    assert call(rest, lambda request: httpx.Response(429, json=body, headers={"retry-after": "7"}))[0::3] == (429, "7")


def test_backoff_honours_the_wait_asked_for_in_full_up_to_the_cap():
    assert webiq._backoff(1, "60") == 60 and webiq._backoff(1, 60.0) == 60 and webiq._backoff(1, "0") == 0
    assert webiq._backoff(1, 600) == 600  # max_retry_after_seconds; a longer wait is left for the next round (below)
    assert 2 <= webiq._backoff(1, None) < 3 and 2 <= webiq._backoff(1, "Mon, 02 Nov 2026 07:28:00 GMT") < 3  # our own backoff


def relayed(status, reason):
    """How a Foundry toolbox relays an HTTP error from Web IQ: a JSON-RPC error in an HTTP 200 response."""
    return {"jsonrpc": "2.0", "id": 101, "error": {
        "code": -32008, "message": "Error encountered while calling tool web from remote server: https://api.microsoft.ai:443/v3/mcp. "
        f"Details: Response status code does not indicate success: {status} ({reason}). Response body: {{\"error\": \"{reason}\"}}"}}


def test_a_relayed_webiq_error_keeps_its_status(toolbox):
    for status, reason in ((400, "Bad Request"), (401, "Unauthorized"), (403, "Forbidden"), (404, "Not Found"),
                           (422, "Unprocessable Entity"), (503, "Service Unavailable")):
        assert call(toolbox, lambda request: httpx.Response(200, json=relayed(status, reason)))[0::3] == (status, None)
    tool_error = {"jsonrpc": "2.0", "id": 101, "result": {"isError": True, "content": [
        {"type": "text", "text": "Response status code does not indicate success: 404 (Not Found)."}]}}
    assert call(toolbox, lambda request: httpx.Response(200, json=tool_error))[0::3] == (404, None)


def test_the_toolbox_gets_one_more_try_with_a_new_token_after_a_401(toolbox):
    found = {"jsonrpc": "2.0", "id": 101, "result": {"structuredContent": {"webResults": []}}}
    answers = iter([httpx.Response(401, text="token expired"), httpx.Response(200, json=found)])
    assert call(toolbox, lambda request: next(answers))[0] == 200
    assert call(toolbox, lambda request: httpx.Response(401, text="no access"))[0] == 401  # twice: given up


class Searches:
    """Web IQ stand-in. `script` maps a query to what its first calls get: (status, wait) or an exception."""

    name = "fake"

    def __init__(self, script=None):
        self.script = {q: list(v) for q, v in (script or {}).items()}
        self.calls = []

    async def start(self, client):
        return None

    async def call(self, client, args, req_id):
        self.calls.append(args["query"])
        step = self.script.get(args["query"]) and self.script[args["query"]].pop(0)
        if isinstance(step, Exception):
            raise step
        if step:
            return step[0], None, f"failed with {step[0]}", step[1]
        return 200, {"webResults": [{"title": "t", "url": f"https://example.com/{len(self.calls)}", "content": "c"}]}, None, None


def test_searches_keep_idle_connections_open(tmp_path, monkeypatch):
    made = {}
    real = httpx.AsyncClient

    def client(*args, **kw):
        made.update(kw)
        return real(*args, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    webiq.run_searches(build_plan(RECORDS, get_workload("brand-owner"), "lean"), tmp_path / "e.jsonl", opts(), api=Searches())
    assert made["limits"].keepalive_expiry == 120  # keepalive_seconds, not httpx's 5 s


def test_searches_that_still_fail_run_again_in_automatic_rounds(tmp_path, pauses, capsys):
    plan = build_plan(RECORDS, get_workload("brand-owner"), "full")
    brand, owner = (e["query"] for e in plan.values())
    api = Searches({brand: [(429, 90.0)] * 4, owner: [httpx.ConnectError("connection refused")] * 2})
    entries, stats = webiq.run_searches(plan, tmp_path / "e.jsonl", opts(), api=api)
    assert all(not e.get("error") and e["results"] for e in entries) and stats["rounds"] == 3
    assert api.calls.count(brand) == 5 and api.calls.count(owner) == 3
    # Between attempts: the 90 s Web IQ asked for. Before round 2: 60 s, or the 90 s asked for; before round 3: 300 s.
    assert pauses[0] == 90 and pauses[2:] == [90, 90, 300]
    out = capsys.readouterr().out
    assert "Stage 1, round 2 of 3: 2 searches failed" in out and "Stage 1, round 3 of 3: 1 searches failed" in out
    e = next(e for e in entries if e["query"] == brand)
    assert e["attempts"] == 5 and e["failed_attempts"] == ["429"] * 4 and stats["failed_attempts_by_status"] == {"429": 4, "network": 2}


def test_a_bad_query_isnt_run_again_and_no_auto_retry_runs_one_round(tmp_path, pauses):
    plan = build_plan(RECORDS, get_workload("brand-owner"), "full")
    brand = next(iter(plan.values()))["query"]
    api = Searches({brand: [(400, None)] * 10})
    entries, stats = webiq.run_searches(plan, tmp_path / "a.jsonl", opts(), api=api)
    assert api.calls.count(brand) == 1 and stats["rounds"] == 1  # a 400 fails the same way every time
    assert next(e for e in entries if e["query"] == brand)["http_status"] == 400
    api = Searches({brand: [(429, "0")] * 10})
    entries, stats = webiq.run_searches(plan, tmp_path / "b.jsonl", opts(no_auto_retry=True), api=api)
    assert api.calls.count(brand) == 2 and stats["rounds"] == 1 and 60 not in pauses
    assert webiq.retry_in_round(next(e for e in entries if e["query"] == brand))  # left for a re-run


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_refused_search_is_sent_once(tmp_path, pauses, status):
    plan = build_plan(RECORDS, get_workload("brand-owner"), "full")
    brand = next(iter(plan.values()))["query"]
    api = Searches({brand: [(status, None)] * 10})
    entries, stats = webiq.run_searches(plan, tmp_path / "e.jsonl", opts(max_attempts=6), api=api)
    assert api.calls.count(brand) == 1 and stats["rounds"] == 1  # not in this round's attempts, nor in a later round
    assert next(e for e in entries if e["query"] == brand)["http_status"] == status


def test_a_wait_longer_than_the_cap_is_waited_out_before_the_next_round(tmp_path, pauses):
    plan = build_plan(RECORDS, get_workload("brand-owner"), "full")
    brand = next(iter(plan.values()))["query"]
    api = Searches({brand: [(429, 5000.0)]})
    entries, stats = webiq.run_searches(plan, tmp_path / "e.jsonl", opts(), api=api)
    assert api.calls.count(brand) == 2 and stats["rounds"] == 2 and all(not e.get("error") for e in entries)
    assert pauses == [3600]  # not 600 s and a retry in the same round: round 2 waits, for up to max_round_wait_seconds
