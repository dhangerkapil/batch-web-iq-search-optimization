"""The one-request sample: each line is one Responses API request, sent directly or through a prompt agent that has the
web_search tool attached directly, and the searches happen inside that one request."""
import json
import pathlib
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import openai
import pytest
from azure.core.exceptions import ClientAuthenticationError

import agent_web_search as aws
import batch as batchlib
import exits
import one_request as one
import pipeline
import prompt_agent
from settings import SETTINGS
from workloads import get_workload

REPO = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = REPO / "agent-web-search-one-request"
RECORDS = REPO / "samples" / "brand-owner" / "records.jsonl"
REQUEST = httpx.Request("POST", "https://example.invalid/openai/v1/responses")
_D = SETTINGS["defaults"]


def http_error(status, message="error", headers=None):
    """The exception the OpenAI SDK raises for an HTTP error response."""
    response = httpx.Response(status, headers=headers or {}, request=REQUEST, json={"error": {"message": message}})
    cls = {400: openai.BadRequestError, 401: openai.AuthenticationError, 429: openai.RateLimitError}.get(
        status, openai.InternalServerError if status >= 500 else openai.APIStatusError)
    return cls(f"Error code: {status} - {message}", response=response, body={"error": {"message": message}})


def answered(searches=3, billed=4, tool_usage=True, tier="default"):
    """A response with `searches` web_search_call items (search, open_page, search, ...) and a cited answer."""
    items = [{"type": "reasoning", "summary": []}]
    for i in range(searches):
        kind = ("search", "open_page")[i % 2]
        action = {"type": kind}
        if kind == "search":
            action.update(query=f"query {i}", sources=[{"type": "url", "url": f"https://example.com/{i}"}])
        items.append({"type": "web_search_call", "id": f"ws_{i}", "status": "completed", "action": action})
    items.append({"type": "message", "role": "assistant", "content": [{
        "type": "output_text", "text": "The answer, from https://example.com/0.",
        "annotations": [{"type": "url_citation", "url": "https://example.com/0", "title": "Example", "start_index": 0, "end_index": 3}]}]})
    body = {"status": "completed", "model": "gpt-test", "service_tier": tier, "output": items,
            "usage": {"input_tokens": 9000, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 300,
                      "output_tokens_details": {"reasoning_tokens": 120}}}
    if tool_usage:
        body["tool_usage"] = {"web_search": {"num_requests": billed}}
    return body


class FakeRaw:
    """What responses.with_raw_response.create returns: the parsed body, and the HTTP headers."""

    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def parse(self):
        return self.body


class FakeClient:
    """Responses API stand-in: every create call is recorded; each gets the next item of `script`, a body or an exception.
    Answers carry `headers`."""

    def __init__(self, script, headers=None):
        self.script, self.calls, self.headers = list(script), [], dict(headers or {})
        self.responses = self.with_raw_response = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item if isinstance(item, FakeRaw) else FakeRaw(item, self.headers)


class Unreadable(FakeRaw):
    """An HTTP 200 whose body can't be parsed, such as a proxy's HTML page."""

    def parse(self):
        raise ValueError("not JSON")


def options(**kw):
    a = dict(records=str(RECORDS), lines=None, first=3, workload="brand-owner", allow_problems=False, design="web-search",
             deployment="my-deployment", max_tool_calls=None, reasoning_effort=None, max_output_tokens=None, tool=None,
             service_tier=None, count_sources=False, mode="request", workdir=None, base_url=None, project_endpoint=None,
             agent_name=None, keep_agent=False, retries=0, concurrency=1, subscription=None, fallback_default=False)
    a.update(kw)
    return SimpleNamespace(**a)


@pytest.mark.parametrize("design", ["web-search", "bounded-web-search"])
def test_requests_built_from_records_are_option_1s_batch_lines_unchanged(design):
    a = options(design=design, first=0)
    reqs, changes = aws.requests_for(a, aws.load_lines(a))
    records = [json.loads(x) for x in RECORDS.read_text(encoding="utf-8").splitlines() if x.strip()]
    opts = SimpleNamespace(deployment="my-deployment", max_tool_calls=int(_D["max_tool_calls"]),
                           reasoning_effort=_D["reasoning_effort"], max_output_tokens=int(_D["max_output_tokens"]))
    lines = pipeline.build_lines(get_workload("brand-owner"), records, design, opts)
    assert changes == []
    assert [(cid, body) for cid, body in reqs] == [(line["custom_id"], line["body"]) for line in lines]


def test_a_batch_file_is_sent_as_it_is_apart_from_the_changes_it_lists(tmp_path):
    line = {"custom_id": "request-1", "method": "POST", "url": "/v1/responses",
            "body": {"model": "my-batch-deployment", "input": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}],
                     "max_output_tokens": 32000, "tools": [{"type": "web_search_preview"}]}}
    path = tmp_path / "lines.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    a = options(records=None, lines=str(path), deployment="my-standard-deployment", tool="web_search")
    [(cid, body)], changes = aws.requests_for(a, aws.load_lines(a))
    assert cid == "request-1"
    assert body == {**line["body"], "model": "my-standard-deployment", "tools": [{"type": "web_search"}], "store": False}
    assert len(changes) == 3 and any("web_search_preview" in c for c in changes) and any(c.startswith("store") for c in changes)

    a = options(records=None, lines=str(path), deployment=None)
    [(_, body)], changes = aws.requests_for(a, aws.load_lines(a))
    assert body == {**line["body"], "store": False} and len(changes) == 1


def test_each_line_is_one_request_and_its_searches_are_inside_the_response():
    client = FakeClient([answered(searches=3, billed=4)])
    outcome = one.send(client, {"model": "m", "input": "x", "tools": [{"type": "web_search"}], "store": False})
    row = one.measure("R01", "request", outcome)
    assert len(client.calls) == 1 and outcome["http_requests"] == 1
    assert "previous_response_id" not in client.calls[0]
    assert row["web_search_calls"] == 3 and row["web_search_actions"] == {"search": 2, "open_page": 1}
    assert row["web_search_queries"] == 2 and row["sources"] == 2 and row["url_citations"] == 1
    assert row["web_search_requests"] == 4 and row["client_tool_calls"] == 0


def test_billed_search_requests_are_not_reported_when_the_response_has_no_tool_usage():
    row = one.measure("R01", "agent", one.send(FakeClient([answered(tool_usage=False)]), {"input": "x"}))
    assert row["web_search_requests"] is None and row["web_search_calls"] == 3
    summary = one.summarize([row])
    assert summary["billed_requests_reported"] is False and summary["web_search_requests"] is None
    assert "not reported" in one.table([row])


def test_only_408_429_and_5xx_are_retried_within_bounds_and_a_timeout_is_never_sent_again(monkeypatch):
    waits = []
    client = FakeClient([http_error(429, headers={"retry-after": "7"}), http_error(408), http_error(503), answered()])
    outcome = one.send(client, {"input": "x"}, retries=3, sleep=waits.append)
    assert outcome["http_requests"] == 4 and outcome["response"] is not None and len(waits) == 3
    assert waits[0] >= 7.0 and outcome["throttled_attempts"] == 1  # max(backoff with jitter, Retry-After)

    outcome = one.send(FakeClient([http_error(429)]), {"input": "x"})  # retries=0: sent once
    assert outcome["http_requests"] == 1 and outcome["response"] is None and outcome["error_kind"] == "http_429"

    longer = one.MAX_SERVER_WAIT + 1  # a Retry-After longer than [flex] max_server_wait_seconds isn't waited out
    client = FakeClient([http_error(429, headers={"retry-after": str(longer)}), answered()])
    outcome = one.send(client, {"input": "x"}, retries=3, sleep=waits.append)
    assert len(client.calls) == 1 and outcome["retry_after_seconds"] == longer

    monkeypatch.setattr(one._flex(), "backoff", lambda attempt: 100.0)
    client = FakeClient([http_error(503), http_error(503), http_error(503), answered()])
    outcome = one.send(client, {"input": "x"}, retries=3, sleep=waits.append, budget=250)  # a third 100 s wait would pass 250
    assert len(client.calls) == 3 and outcome["error_kind"] == "http_503"

    client = FakeClient([http_error(400, "bad request"), answered()])
    outcome = one.send(client, {"input": "x"}, retries=3, sleep=waits.append)
    assert len(client.calls) == 1 and outcome["error_kind"] == "http_400"

    client = FakeClient([openai.APITimeoutError(request=REQUEST), answered()])
    outcome = one.send(client, {"input": "x"}, retries=3, sleep=waits.append)
    assert len(client.calls) == 1 and outcome["held"] is True and outcome["error_kind"] == "timeout"

    stopped = threading.Event()
    stopped.set()  # the run has stopped: a request waiting to retry isn't sent again, and the wait ends at once
    client = FakeClient([http_error(429, headers={"retry-after": "30"}), answered()])
    outcome = one.send(client, {"input": "x"}, retries=3, stop=stopped)
    assert len(client.calls) == 1 and outcome["error_kind"] == "http_429" and outcome["seconds"] < 5

    with pytest.raises(one.Stop):
        one.send(FakeClient([http_error(401)]), {"input": "x"})
    with pytest.raises(one.Stop) as caught:  # no token: nothing was sent
        one.send(FakeClient([ClientAuthenticationError("Failed to invoke the Azure CLI")]), {"input": "x"})
    assert caught.value.kind == "access" and caught.value.outcome["http_requests"] == 0
    with pytest.raises(one.Stop) as caught:
        one.send(FakeClient([http_error(400, "Flex tier is not supported for this deployment")]), {"model": "d", "input": "x"})
    assert caught.value.kind == "flex_unsupported" and "--service-tier default" in str(caught.value)
    with pytest.raises(one.Stop) as caught:  # not an HTTP error: it may have reached the service, so it's held
        one.send(FakeClient([ValueError("boom")]), {"input": "x"})
    assert caught.value.kind == "exception" and caught.value.outcome["held"] is True
    with pytest.raises(one.Stop) as caught:  # answered but unreadable: held, with its request ID kept
        one.send(FakeClient([Unreadable({}, {"x-request-id": "req-9"})]), {"input": "x"})
    assert caught.value.outcome["held"] is True and caught.value.outcome["request_ids"][0]["x-request-id"] == "req-9"


def test_flex_by_default_with_an_explicit_fallback_and_the_rate_limit_headers(tmp_path, monkeypatch):
    monkeypatch.setattr(one._flex(), "backoff", lambda attempt: 0.0)
    busy = "The service is temporarily unable to process your request. Please try again later."
    client = FakeClient([
        answered(tier="flex"),  # R01 on Flex
        http_error(429, busy), http_error(429, busy), answered(tier="default"),  # R02: Flex refuses it twice, then Standard
        http_error(400, "Flex tier is not supported for this request"), answered(tier="default"),  # R03: this line only
    ], headers={"x-ratelimit-limit-tokens": "600000", "x-ratelimit-remaining-tokens": "591488", "x-request-id": "req-123"})
    monkeypatch.setattr(batchlib, "make_client", lambda *args, **kwargs: client)
    wd = tmp_path / "flex"
    a = options(first=3, workdir=str(wd), base_url="https://example.invalid/openai/v1/", service_tier="flex", retries=1,
                fallback_default=True)
    assert aws.cmd_run(a) == 0
    assert [c.get("service_tier") for c in client.calls] == ["flex", "flex", "flex", "default", "flex", "default"]
    rows = {r["id"]: r for r in map(json.loads, (wd / "metrics.jsonl").read_text(encoding="utf-8").splitlines())}
    assert rows["R01"]["service_tier"] == "flex" and not rows["R01"]["fallback"]
    assert rows["R01"]["ratelimit_limit_tokens"] == 600000 and rows["R01"]["ratelimit_remaining_tokens"] == 591488
    assert rows["R02"]["fallback"] and rows["R02"]["service_tier"] == "default" and rows["R02"]["tier_requested"] == "flex"
    assert rows["R02"]["http_requests"] == 3 and rows["R02"]["throttled_attempts"] == 2 and rows["R02"]["throttle"] == "capacity"
    assert rows["R03"]["fallback"] and rows["R03"]["service_tier"] == "default"
    run = json.loads((wd / "run.json").read_text(encoding="utf-8"))
    assert run["stopped"] is None and run["summary"]["fallbacks"] == 2 and run["summary"]["other_tier"] == 0
    shareable = (wd / "metrics.jsonl").read_text(encoding="utf-8") + (wd / "summary.md").read_text(encoding="utf-8")
    assert "req-123" not in shareable and "req-123" in (wd / "responses.jsonl").read_text(encoding="utf-8")

    client = FakeClient([http_error(429, busy), http_error(429, busy), http_error(401)])  # the fallback stops the run
    wd = tmp_path / "fallback-stopped"
    a = options(first=1, workdir=str(wd), base_url="https://example.invalid/openai/v1/", service_tier="flex", retries=1,
                fallback_default=True)
    assert aws.cmd_run(a) == 1
    [row] = [json.loads(x) for x in (wd / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert row["http_requests"] == 3 and row["fallback"] and row["tier_requested"] == "flex" and row["throttled_attempts"] == 2

    limited = "Requests to the Responses_Create Operation have exceeded token rate limit of your current pricing tier."
    client = FakeClient([http_error(429, limited), http_error(429, limited)])  # a quota limit: Standard shares it
    wd = tmp_path / "no-fallback-on-quota"
    a = options(first=1, workdir=str(wd), base_url="https://example.invalid/openai/v1/", service_tier="flex", retries=1,
                fallback_default=True)
    assert aws.cmd_run(a) == 1
    [row] = [json.loads(x) for x in (wd / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [c.get("service_tier") for c in client.calls] == ["flex", "flex"] and row["throttle"] == "rate_limit"
    assert not row["fallback"]


def test_the_agent_gets_the_model_instructions_and_tool_and_a_toolbox_is_refused():
    body = {"model": "dep", "input": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "REC"}],
            "tools": [{"type": "web_search", "user_location": {"type": "approximate", "country": "US"}}],
            "text": {"format": {"type": "json_schema", "name": "x", "schema": {}}}, "reasoning": {"effort": "low"},
            "max_tool_calls": 2, "max_output_tokens": 8000, "store": False}
    definition, given, call, dropped = prompt_agent.split_body(body)
    assert definition == {"kind": "prompt", "model": "dep", "tools": body["tools"], "instructions": "SYS", "text": body["text"],
                          "reasoning": {"effort": "low"}}
    assert given == [{"role": "user", "content": "REC"}]
    assert call == {"max_output_tokens": 8000, "max_tool_calls": 2, "store": False}
    assert dropped == []
    toolbox = {**body, "tools": [{"type": "mcp", "server_label": "toolbox", "server_url": "https://example.invalid/toolboxes/t/mcp"}]}
    with pytest.raises(SystemExit, match="toolbox"):
        prompt_agent.split_body(toolbox)


def test_agent_mode_lists_the_line_settings_it_has_no_place_for(tmp_path, capsys):
    line = {"custom_id": "request-1", "method": "POST", "url": "/v1/responses",
            "body": {"model": "m", "input": "U", "tools": [{"type": "web_search"}], "temperature": 0.2, "truncation": "auto",
                     "metadata": {"k": "v"}}}
    path = tmp_path / "lines.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert aws.cmd_plan(options(records=None, lines=str(path), deployment=None, mode="agent")) == 0
    out = capsys.readouterr().out
    assert "not sent in agent mode, which has no place for them: metadata, truncation" in out
    assert '"temperature": 0.2' in out  # a definition setting: it goes into the agent


def test_ctrl_c_stops_sending_and_pressing_it_again_loses_no_answer(tmp_path, monkeypatch, capsys):
    original = signal.getsignal(signal.SIGINT)
    in_flight, pressed = threading.Event(), threading.Event()

    class InFlight(FakeClient):
        def create(self, **kwargs):
            if self.calls:  # the second line, in flight when Ctrl+C comes
                in_flight.set()
                assert pressed.wait(5)
                if signal.getsignal(signal.SIGINT) is not original:  # only to the run's handler, which can't stop pytest
                    signal.raise_signal(signal.SIGINT)  # Ctrl+C again, while the run waits for this line
                time.sleep(0.5)  # the run stops sending before this line ends
            return super().create(**kwargs)

    client = InFlight([answered(), answered(), answered()])
    monkeypatch.setattr(batchlib, "make_client", lambda *args, **kwargs: client)
    real, seen = aws._record, []

    def record(*args):
        seen.append(args[2])
        if len(seen) == 1:
            assert in_flight.wait(5)
            try:
                signal.raise_signal(signal.SIGINT)  # Ctrl+C while the first answer is recorded and the second line is in flight
            finally:
                pressed.set()
        return real(*args)

    monkeypatch.setattr(aws, "_record", record)
    wd = tmp_path / "run"
    assert aws.cmd_run(options(first=3, workdir=str(wd), base_url="https://example.invalid/openai/v1/")) == exits.INTERRUPTED
    assert len(client.calls) == 2  # the line in flight finished; the third was never sent
    run = json.loads((wd / "run.json").read_text(encoding="utf-8"))
    assert run["stopped"] == "interrupted (Ctrl+C)" and run["not_sent"] == 1
    assert [json.loads(x)["id"] for x in (wd / "metrics.jsonl").read_text(encoding="utf-8").splitlines()] == ["R01", "R02"]
    err = capsys.readouterr().err
    assert aws._CtrlC.NOTE in err and "doesn't resume" in err
    assert signal.getsignal(signal.SIGINT) is original


class FakeProject:
    """A Foundry project stand-in: agent versions it creates and deletes, and a Responses client."""

    last = None

    def __init__(self, endpoint, subscription=None):
        self.created, self.deleted = [], []
        self.client = FakeClient(FakeProject.script)
        FakeProject.last = self

    def exists(self, name):
        return False

    def create_version(self, name, definition):
        self.created.append((name, definition))
        return str(len(self.created))

    def delete(self, name, versions):
        self.deleted.append((name, list(versions)))
        return "deleted (GET returns 404)"

    def openai_client(self, timeout):
        return self.client


def test_agent_mode_calls_each_line_once_by_reference_and_deletes_the_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_agent, "Project", FakeProject)
    FakeProject.script = [answered(), answered(searches=1)]
    a = options(mode="agent", first=2, workdir=str(tmp_path / "agent"), project_endpoint="https://example.invalid/api/projects/p")
    assert aws.cmd_run(a) == 0
    project = FakeProject.last
    [(name, definition)] = project.created  # one definition for both lines: one version
    assert definition["tools"] == [{"type": "web_search"}] and definition["model"] == "my-deployment"
    assert len(project.client.calls) == 2
    for call in project.client.calls:
        assert call["extra_body"] == {"agent_reference": {"type": "agent_reference", "name": name, "version": "1"}}
        assert "model" not in call and "tools" not in call and call["store"] is False
        assert [m["role"] for m in call["input"]] == ["user"]
    assert project.deleted == [(name, ["1"])]

    FakeProject.script = [http_error(401)]  # an error every request would get stops the run; the agent is still deleted
    wd = tmp_path / "stopped"
    a = options(mode="agent", first=2, workdir=str(wd), project_endpoint="https://example.invalid/api/projects/p")
    assert aws.cmd_run(a) == 1
    assert FakeProject.last.deleted and len(FakeProject.last.client.calls) == 1  # the second line was never sent
    [row] = [json.loads(x) for x in (wd / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert row["id"] == "R01" and row["error_kind"] == "http_401"
    assert json.loads((wd / "run.json").read_text(encoding="utf-8"))["not_sent"] == 1


def test_a_run_shares_counts_only_and_keeps_whole_responses_local(tmp_path, monkeypatch):
    client = FakeClient([answered(), answered(searches=1, billed=2)])
    monkeypatch.setattr(batchlib, "make_client", lambda *args, **kwargs: client)
    wd = tmp_path / "run"
    a = options(first=2, workdir=str(wd), base_url="https://example.invalid/openai/v1/")
    assert aws.cmd_run(a) == 0
    shareable = (wd / "metrics.jsonl").read_text(encoding="utf-8") + (wd / "summary.md").read_text(encoding="utf-8")
    for text in ("example.com", "The answer", "query 0", "example.invalid"):
        assert text not in shareable
    rows = [json.loads(x) for x in (wd / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(set(r) == set(one.METRIC_FIELDS) and r["http_requests"] == 1 for r in rows)
    assert "example.com" in (wd / "responses.jsonl").read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="already holds a run"):
        aws.cmd_run(a)


def test_plan_prints_the_request_and_sends_nothing():
    r = subprocess.run([sys.executable, "agent_web_search.py", "plan", "../samples/brand-owner/records.jsonl", "--first", "2"],
                       cwd=SAMPLE, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "POST <base url>responses" in r.stdout and '"type": "web_search"' in r.stdout and "plan sends nothing" in r.stdout
    assert '"service_tier": "flex"' in r.stdout  # Flex processing unless --service-tier says otherwise
