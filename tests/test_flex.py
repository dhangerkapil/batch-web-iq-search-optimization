"""Flex processing, offline: a fake Responses API stands in for the service, so nothing is sent or billed."""
import _thread
import argparse
import json
import pathlib
import re
import signal
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

import batch as batchlib
import flex
import webiq
from batch import response_metrics
from cost import actual, forecast
from pipeline import cmd_run
from prices import line_cost, load_prices, model_rates
from report import NEVER_COPY, check_text

PRICES = load_prices()
SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "samples" / "brand-owner"
REQUEST = httpx.Request("POST", "https://example.invalid/openai/v1/responses")
NOT_SUPPORTED = "Flex tier is not supported for this deployment"
RECORDS = [
    {"id": f"R{i}", "brand": f"Brand {i}", "market": "US", "claimed_owner": f"Owner {i % 2}", "claimed_owner_hq_country": "US"}
    for i in range(3)
]


def http_error(status, message, headers=None):
    """The exception the OpenAI SDK raises for an HTTP error response."""
    response = httpx.Response(status, headers=headers or {}, request=REQUEST, json={"error": {"message": message}})
    cls = {400: openai.BadRequestError, 401: openai.AuthenticationError, 429: openai.RateLimitError}.get(
        status, openai.InternalServerError if status >= 500 else openai.APIStatusError)
    return cls(f"Error code: {status} - {message}", response=response, body={"error": {"message": message}})


def answer(cid, tier="flex", input_tokens=1000, cached=0, written=0, output_tokens=100, searches=0):
    """A Responses API response body."""
    return {
        "status": "completed",
        "service_tier": tier,
        "usage": {"input_tokens": input_tokens, "input_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
                  "output_tokens": output_tokens, "output_tokens_details": {"reasoning_tokens": 10}},
        "tool_usage": {"web_search": {"num_requests": searches}},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"record_id": cid, "all_verified": True})}]}],
    }


class Response:
    """What responses.create returns: an object with model_dump(), like the SDK's Response."""

    def __init__(self, body):
        self.body = body

    def model_dump(self):
        return json.loads(json.dumps(self.body))


def record_id(body):
    """The record a request is for: the id in the RECORD part of its prompt (the last one in the text)."""
    return re.findall(r'"id":"([^"]+)"', "".join(m["content"] for m in body["input"]))[-1]


class FakeFlex:
    """Responses API stand-in. `script` maps a record id to what its requests get, in order: an exception to raise or a
    response body; after that its requests succeed on the Flex tier. `during(id)` runs while a request is in flight."""

    def __init__(self, script=None, during=None, searches=0):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.during, self.searches = during, searches
        self.calls, self.bodies = [], []
        self.lock = threading.Lock()
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **body):
        cid = record_id(body)
        with self.lock:
            self.calls.append(cid)
            self.bodies.append(body)
            step = self.script[cid].pop(0) if self.script.get(cid) else None
        if self.during:
            self.during(cid)
        if isinstance(step, BaseException):
            raise step
        return Response(step or answer(cid, searches=self.searches))


def lines(*ids):
    return [{"custom_id": i, "method": "POST", "url": "/v1/responses",
             "body": {"model": "placeholder", "input": [{"role": "user", "content": f'RECORD:\n{{"id":"{i}"}}'}], "store": False}}
            for i in ids]


def ledger(workdir, event):
    return [r for r in flex.read_ledger(workdir) if r["event"] == event]


@pytest.fixture
def waits(monkeypatch):
    """Retries don't really wait: the waits are recorded instead."""
    seen = []
    monkeypatch.setattr(flex, "_sleep", lambda seconds, stop: seen.append(seconds))
    return seen


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """Automatic rounds don't really pause: the pauses are recorded instead."""
    seen = []
    monkeypatch.setattr(flex, "_pause", seen.append)
    return seen


def test_each_line_is_sent_once_on_the_flex_tier_and_recorded_before_and_after(tmp_path):
    fake = FakeFlex()
    out = flex.run_lines(lines("B", "A", "C"), tmp_path, deployment="flex-dep", client=fake)
    assert sorted(fake.calls) == ["A", "B", "C"] and out["stopped"] is None
    assert all(b["model"] == "flex-dep" and b["service_tier"] == "flex" and b["store"] is False for b in fake.bodies)
    rows = out["rows"]
    assert [r["custom_id"] for r in rows] == ["A", "B", "C"]  # sorted by custom_id, like the batch path's metrics.jsonl
    assert rows[0] == dict(response_metrics(answer("A")), custom_id="A")  # the batch path's metrics row, exactly
    written = flex.read_ledger(tmp_path)
    for cid, body in zip(fake.calls, fake.bodies):
        start = next(i for i, r in enumerate(written) if r["event"] == "start" and r["custom_id"] == cid)
        outcome = next(i for i, r in enumerate(written) if r["event"] == "outcome" and r["custom_id"] == cid)
        s, o = written[start], written[outcome]
        assert start < outcome and (s["round"], s["attempt"]) == (1, 1) and s["at"]
        assert s["request_sha256"] == o["request_sha256"] == flex.request_sha256(body)
        assert (o["http_status"], o["billing"], o["service_tier"], o["result"]) == (200, "measured", "flex", "completed")
        assert o["usage"] == {"input_tokens": 1000, "cached_tokens": 0, "cache_write_tokens": 0, "output_tokens": 100,
                              "reasoning_tokens": 10, "web_search_requests": 0}
    s = out["summary"]
    assert s["lines"]["completed"] == 3 and s["attempts"]["requests"] == 3 and s["attempts"]["by_http_status"] == {"200": 3}


def test_a_429_or_5xx_is_retried_after_at_least_the_wait_the_service_asks(tmp_path, waits):
    fake = FakeFlex({
        "A": [http_error(429, "rate limited", {"retry-after": "30"})],
        "B": [http_error(503, "busy", {"retry-after-ms": "1500"}), http_error(500, "server error")],
    })
    out = flex.run_lines(lines("A", "B"), tmp_path, deployment="d", client=fake, concurrency=1)
    assert fake.calls == ["A", "A", "B", "B", "B"]
    assert [r["status"] for r in out["rows"]] == ["completed", "completed"]
    assert waits[0] >= 30  # the service's Retry-After, in full
    assert 5 <= waits[1] <= 15 and 10 <= waits[2] <= 30  # our backoff (10 s, doubling, with jitter) when it asks for less
    outcomes = {(r["custom_id"], r["attempt"]): r for r in ledger(tmp_path, "outcome")}
    a1 = outcomes["A", 1]
    assert (a1["http_status"], a1["billing"], a1["result"], a1["retry_after_s"]) == (429, "not_billed", "retrying", 30)
    assert a1["service_tier"] is None and a1["usage"] is None and "rate limited" in a1["error"]  # every outcome row has the same fields
    assert (outcomes["B", 2]["http_status"], outcomes["B", 2]["billing"], outcomes["B", 2]["result"]) == (500, "unknown", "retrying")
    a = out["summary"]["attempts"]
    assert a["requests"] == 5 and a["not_billed_429"] == 1 and a["unknown_charges"] == 2
    assert a["by_http_status"] == {"200": 2, "429": 1, "500": 1, "503": 1}


@pytest.mark.parametrize("error, kind", [
    (openai.APITimeoutError(request=REQUEST), "timeout"),
    (openai.APIConnectionError(request=REQUEST), "connection"),
    (TimeoutError("timed out"), "timeout"),
    (ConnectionResetError("connection reset by peer"), "connection"),
], ids=["APITimeoutError", "APIConnectionError", "TimeoutError", "ConnectionResetError"])
def test_a_timeout_or_dropped_connection_holds_the_line_and_the_others_carry_on(tmp_path, waits, error, kind):
    fake = FakeFlex({"B": [error]})
    out = flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="d", client=fake, concurrency=1)
    # No second attempt for B (it may have been processed and billed), the job isn't stopped, and C is still sent.
    assert fake.calls == ["A", "B", "C"] and waits == [] and out["stopped"] is None
    b = next(r for r in ledger(tmp_path, "outcome") if r["custom_id"] == "B")
    assert (b["http_status"], b["error_kind"], b["billing"], b["result"]) == (None, kind, "unknown", "unknown")
    assert flex.line_state([r for r in flex.read_ledger(tmp_path) if r["custom_id"] == "B"], 3)["state"] == "held"
    assert out["summary"]["lines"] == {"total": 3, "completed": 2, "failed": 0, "used_up": 0, "held_in_flight": 1, "not_finished": 0}
    assert out["summary"]["attempts"]["unknown_charges"] == 1 and out["summary"]["attempts"]["by_http_status"][kind] == 1
    assert out["rows"][1]["status"] == "held" and "--retry-ambiguous" in out["rows"][1]["error"]
    flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="d", client=fake)
    flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="d", client=fake, retry_failed=True)
    assert fake.calls == ["A", "B", "C"]  # held: neither a plain re-run nor --retry-failed sends it again
    out = flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="d", client=fake, retry_ambiguous=True)
    assert fake.calls == ["A", "B", "C", "B"] and [r["status"] for r in out["rows"]] == ["completed"] * 3
    assert [(r["round"], r["attempt"]) for r in ledger(tmp_path, "start") if r["custom_id"] == "B"] == [(1, 1), (2, 1)]
    assert out["summary"]["attempts"]["unknown_charges"] == 1  # the timed-out attempt: counted, never priced


def test_a_longer_retry_after_than_the_cap_isnt_cut_short_the_line_is_left_for_a_rerun(tmp_path, waits):
    fake = FakeFlex({"A": [http_error(429, "busy", {"retry-after": "3600"})]})
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake, auto_rounds=1)  # no automatic round to wait for
    assert fake.calls == ["A"] and waits == []
    assert out["summary"]["lines"]["not_finished"] == 1 and "left for a re-run after 20" in out["rows"][0]["error"]
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake)  # a plain re-run continues the round
    assert fake.calls == ["A", "A"] and out["rows"][0]["status"] == "completed"
    assert [(r["round"], r["attempt"]) for r in ledger(tmp_path, "start")] == [(1, 1), (1, 2)]


def test_a_deployment_without_flex_stops_the_whole_job(tmp_path):
    fake = FakeFlex({"A": [http_error(400, NOT_SUPPORTED)]})
    out = flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="dep-54", client=fake, concurrency=1)
    assert fake.calls == ["A"]  # every other request would fail the same way
    assert "'dep-54' doesn't support Flex processing" in out["stopped"] and "gpt-6-sol" in out["stopped"]
    assert ledger(tmp_path, "outcome")[0]["billing"] == "rejected_no_usage"
    assert out["summary"]["lines"]["not_finished"] == 3
    out = flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="dep-6", client=fake, concurrency=1)
    assert fake.calls == ["A", "A", "B", "C"] and out["summary"]["lines"]["completed"] == 3


def test_other_client_errors_are_final_and_not_retried(tmp_path, waits):
    # The deployment's name mentions flex: that mustn't make this 400 look like "Flex tier is not supported".
    fake = FakeFlex({"A": [http_error(400, "Unsupported parameter: 'max_tool_calls' is not supported by gpt-flex")]})
    out = flex.run_lines(lines("A", "B"), tmp_path, deployment="gpt-flex", client=fake)
    assert fake.calls.count("A") == 1 and waits == [] and out["stopped"] is None
    assert out["rows"][0]["status"] == "error" and "max_tool_calls" in out["rows"][0]["error"]
    assert out["summary"]["lines"] == {"total": 2, "completed": 1, "failed": 1, "used_up": 0, "held_in_flight": 0, "not_finished": 0}
    flex.run_lines(lines("A", "B"), tmp_path, deployment="gpt-flex", client=fake, retry_failed=True)
    assert fake.calls.count("A") == 1  # retrying can't fix it


def test_a_response_on_another_tier_is_an_error_and_isnt_priced_as_flex(tmp_path):
    fake = FakeFlex({"A": [answer("A", tier="default")]})
    out = flex.run_lines(lines("A", "B"), tmp_path, deployment="d", client=fake)
    assert out["rows"][0] == {"custom_id": "A", "status": "error", "error": "processed on tier default, not flex"}
    assert out["summary"]["attempts"]["other_tier"] == 1 and out["summary"]["lines"]["failed"] == 1
    cost = actual(out["rows"], records=2, prices=PRICES, price_model="gpt-6-sol", webiq_calls=0, tier="flex")
    assert cost["lines_with_usage"] == 1 and cost["lines_failed"] == 1
    assert cost["usd"]["tokens_input"] == pytest.approx(1000 * 1.0 / 1e6)  # B's input only
    flex.run_lines(lines("A", "B"), tmp_path, deployment="d", client=fake)
    assert fake.calls.count("A") == 1  # not sent again: it was processed, on another tier


def test_a_rerun_skips_completed_lines_and_holds_a_request_that_was_in_flight(tmp_path, capsys):
    fake = FakeFlex()
    flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake)
    b = flex.flex_body(lines("B")[0], "d")
    with open(tmp_path / flex.LEDGER, "a", encoding="utf-8", newline="\n") as f:
        # B was in flight when the process died: its start row, then an outcome row cut short mid-write.
        f.write(json.dumps({"event": "start", "custom_id": "B", "round": 1, "attempt": 1, "request_sha256": flex.request_sha256(b),
                            "at": "2026-11-01T00:00:00+00:00"}) + "\n")
        f.write('{"event":"outcome","custom_id":"B","rou')
    out = flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="d", client=fake)
    assert fake.calls == ["A", "C"]
    assert "1 held because a request's outcome is unknown" in capsys.readouterr().out
    assert out["summary"]["lines"]["held_in_flight"] == 1 and out["summary"]["attempts"]["held_in_flight"] == 1
    assert out["summary"]["attempts"]["by_http_status"]["no_outcome"] == 1
    assert out["rows"][1]["status"] == "held" and "--retry-ambiguous" in out["rows"][1]["error"]
    out = flex.run_lines(lines("A", "B", "C"), tmp_path, deployment="d", client=fake, retry_ambiguous=True)
    assert fake.calls == ["A", "C", "B"] and [r["status"] for r in out["rows"]] == ["completed"] * 3
    assert [(r["round"], r["attempt"]) for r in ledger(tmp_path, "start") if r["custom_id"] == "B"] == [(1, 1), (2, 1)]
    assert out["summary"]["attempts"]["unknown_charges"] == 1  # the request with no outcome: counted, not priced


def test_without_automatic_rounds_a_used_up_line_is_sent_again_only_with_retry_failed(tmp_path, waits):
    fake = FakeFlex({"A": [http_error(429, "busy") for _ in range(3)]})
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake, auto_rounds=1)
    assert fake.calls == ["A"] * 3 and len(waits) == 2 and out["summary"]["lines"]["used_up"] == 1
    assert out["rows"][0]["status"] == "held" and "--retry-failed" in out["rows"][0]["error"]
    flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake, auto_rounds=1)
    assert len(fake.calls) == 3  # held
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake, retry_failed=True, auto_rounds=1)
    assert len(fake.calls) == 4 and out["rows"][0]["status"] == "completed"
    assert [(r["round"], r["attempt"]) for r in ledger(tmp_path, "start")] == [(1, 1), (1, 2), (1, 3), (2, 1)]
    assert out["summary"]["attempts"]["not_billed_429"] == 3


def test_ctrl_c_sends_nothing_new_and_records_the_requests_in_flight(tmp_path, capsys):
    def press_ctrl_c(cid):
        if cid == "B":
            _thread.interrupt_main()  # what Ctrl+C does to the main thread, while B is in flight
            time.sleep(1.5)  # still in flight when the main thread notices

    # Ctrl+C raises KeyboardInterrupt, as in a terminal, even if whatever started the tests ignores SIGINT.
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        fake = FakeFlex(during=press_ctrl_c)
        out = flex.run_lines(lines("A", "B", "C", "D"), tmp_path, deployment="d", client=fake, concurrency=1)
    finally:
        signal.signal(signal.SIGINT, previous)
    assert fake.calls == ["A", "B"] and out["stopped"].startswith("Interrupted")
    assert [r["status"] for r in out["rows"]] == ["completed", "completed", "error", "error"]  # B's outcome was recorded
    assert "Stopping: no new requests are sent" in capsys.readouterr().err
    out = flex.run_lines(lines("A", "B", "C", "D"), tmp_path, deployment="d", client=FakeFlex())
    assert [r["status"] for r in out["rows"]] == ["completed"] * 4 and out["summary"]["attempts"]["requests"] == 4


def test_flex_rates_price_cache_writes_at_the_cache_write_rate():
    rates = model_rates(PRICES, "gpt-6-sol", "flex")
    assert rates == {"input_per_1m": 1.0, "cached_input_per_1m": 0.1, "cache_write_per_1m": 1.25, "output_per_1m": 5.0}
    m = response_metrics(answer("A", input_tokens=10_000, cached=4_000, written=5_000, output_tokens=1_000, searches=2))
    assert m["cache_write_tokens"] == 5_000 and m["service_tier"] == "flex"
    c = line_cost(m, rates, PRICES)
    assert c["input"] == pytest.approx((1_000 * 1.0 + 4_000 * 0.1 + 5_000 * 1.25) / 1e6)
    assert c["cache_write"] == pytest.approx(5_000 * 1.25 / 1e6)
    assert c["output"] == pytest.approx(1_000 * 5.0 / 1e6) and c["web_search"] == pytest.approx(2 * 14 / 1000)
    batch = line_cost(m, model_rates(PRICES, "gpt-5.4-mini"), PRICES)  # no cache-write rate: written tokens at the input rate
    assert batch["input"] == pytest.approx((6_000 * 0.375 + 4_000 * 0.0375) / 1e6) and batch["cache_write"] == 0
    a = actual([dict(m, custom_id="A")], records=1, prices=PRICES, price_model="gpt-6-sol", webiq_calls=0, tier="flex")
    assert a["prices"]["tier"] == "flex" and a["prices"]["rates"] == rates and "open question" in a["prices"]["cache_writes"]
    assert a["totals"]["cache_write_tokens"] == 5_000
    assert a["usd"]["total_if_cache_writes_billed_on_top"] == pytest.approx(a["usd"]["total"] + 5_000 * 1.0 / 1e6)
    with pytest.raises(SystemExit, match="gpt-6-sol"):
        model_rates(PRICES, "gpt-5.4-mini", "flex")  # the message names the models that have Flex prices


def test_forecasts_at_the_flex_tier():
    f = forecast(prices=PRICES, price_model="gpt-6-sol", lines=1000, input_tokens=3_000_000, exact=True, webiq_calls=1_200,
                 output_per_line=500, tier="flex")
    assert f["usd"]["webiq"] == pytest.approx(8.4)
    assert f["usd"]["input"] == pytest.approx(3.75)  # at the $1.25 cache-write rate, so the forecast errs high
    assert f["usd"]["output"] == pytest.approx(2.5)
    assert "gpt-6-sol flex rates" in f["note"] and "cache-write rate" in f["note"]
    batch = forecast(prices=PRICES, price_model="gpt-5.4-mini", lines=1000, input_tokens=3_000_000, exact=True, webiq_calls=1_200,
                     output_per_line=500)
    assert batch["usd"]["input"] == pytest.approx(1.125) and "gpt-5.4-mini global_batch rates" in batch["note"]


def test_plan_prints_a_forecast_at_flex_rates(monkeypatch, capsys):
    import flex_job
    import two_stage

    records = str(SAMPLE / "records.jsonl")
    on_flex = ["flex_job.py", "plan", records, "--design", "two-stage", "--plan", "lean"]
    monkeypatch.setattr(sys, "argv", on_flex + ["--price-model", "gpt-6-sol"])
    flex_job.main()
    assert "Forecast before searching at Flex rates (gpt-6-sol): 23 records, 26 Web IQ calls" in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["two_stage.py", "plan", records, "--plan", "lean"])
    two_stage.main()
    assert "Forecast before searching: 23 records" in capsys.readouterr().out  # Global Batch, as before
    monkeypatch.setattr(sys, "argv", on_flex)  # the default price model has no Flex prices
    with pytest.raises(SystemExit, match="No flex price for 'gpt-5.4-mini'"):
        flex_job.main()


# ---------- the run command on Flex (flex_job.py) ----------


class FakeWebIQ:
    """Answers every Web IQ search with one result."""

    name = "fake"

    def __init__(self):
        self.calls = []

    async def start(self, client):
        return None

    async def call(self, client, args, req_id):
        self.calls.append(args["query"])
        return 200, {"webResults": [{"title": "t", "url": f"https://example.com/{len(self.calls)}", "content": "c"}]}, None, None


def run_args(tmp_path, design, **kw):
    records = tmp_path / "records.jsonl"
    records.write_text("".join(json.dumps(r) + "\n" for r in RECORDS), encoding="utf-8")
    a = dict(
        records=str(records), workload="brand-owner", allow_problems=False, design=design, plan="lean", workdir=str(tmp_path / "work"),
        label=None, executor="flex", deployment="flex-dep", price_model="gpt-6-sol", prices=None, reasoning_effort="low",
        max_output_tokens=8000, max_tool_calls=2, output_tokens_per_line=None, max_cost=None, base_url="https://example.invalid/openai/v1/",
        subscription=None, poll_seconds=0, stall_minutes=120, cancel_on_stall=False, keep_working_files=False, flex_concurrency=2,
        retry_failed=False, retry_ambiguous=False, via="fake", toolbox_url=None, toolbox_tool=None, webiq_endpoint=None,
        webiq_auth="key", max_results=3, max_chars=200, language="en", qps=1000.0, concurrency=1, max_attempts=3, max_queries=100,
    )
    a.update(kw)
    return argparse.Namespace(**a)


def read_run(work):
    return json.loads((work / "run.json").read_text(encoding="utf-8"))


@pytest.fixture
def flex_only(monkeypatch):
    """Use `fake = flex_only(FakeFlex(...))` for the Flex client; the Batch API must not be touched."""

    def no_batch(*a, **k):
        raise AssertionError("the Batch API must not be used for a Flex job")

    monkeypatch.setattr(batchlib, "make_client", no_batch)

    def use(fake):
        monkeypatch.setattr(flex, "make_client", lambda *a, **k: fake)
        return fake

    return use


def test_run_bounded_web_search_on_flex(tmp_path, flex_only):
    fake = flex_only(FakeFlex(searches=2))
    a = run_args(tmp_path, "bounded-web-search")
    cmd_run(a)
    work = tmp_path / "work"
    run = read_run(work)
    assert run["status"] == "done" and run["executor"] == "flex" and run["tier"] == "flex" and run["deployment"] == "flex-dep"
    assert run["flex"]["lines"]["completed"] == 3 and run["flex"]["attempts"]["requests"] == 3
    assert run["flex"]["attempts"]["held_in_flight"] == 0 and run["flex"]["concurrency"] == 2
    assert run["actual"]["prices"]["tier"] == "flex" and run["actual"]["lines_with_usage"] == 3
    usd = run["actual"]["usd"]
    assert usd["web_search"] == pytest.approx(3 * 2 * 14 / 1000)  # Bing requests, billed as on Batch
    assert usd["tokens_input"] == pytest.approx(3 * 1000 * 1.0 / 1e6) and usd["tokens_output"] == pytest.approx(3 * 100 * 5.0 / 1e6)
    assert all(b["tools"][0]["type"] == "web_search" and b["max_tool_calls"] == 2 and b["service_tier"] == "flex" for b in fake.bodies)
    metrics = [json.loads(x) for x in (work / "metrics.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [m["custom_id"] for m in metrics] == ["R0", "R1", "R2"] and all(m["service_tier"] == "flex" for m in metrics)
    results = [json.loads(x) for x in (work / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(r["status"] == "completed" and r["error"] is None and r["result"]["all_verified"] for r in results)
    assert not (work / "batch-input.jsonl").exists() and (work / flex.LEDGER).exists() and (work / "prices-used.json").exists()
    assert flex.LEDGER in NEVER_COPY and not check_text((work / "run.json").read_text(encoding="utf-8"))
    cmd_run(a)  # done: nothing is sent again
    assert len(fake.calls) == 3


def test_run_two_stage_with_webiq_evidence_on_flex(tmp_path, monkeypatch, flex_only, capsys):
    searches = FakeWebIQ()
    monkeypatch.setattr(webiq, "make_client", lambda opts: searches)
    fake = flex_only(FakeFlex())
    with pytest.raises(SystemExit, match="above --max-cost"):
        cmd_run(run_args(tmp_path, "two-stage", max_cost=0.0001, workdir=str(tmp_path / "capped")))
    assert searches.calls == [] and fake.calls == []  # stopped by the forecast at Flex rates, before anything was called
    a = run_args(tmp_path, "two-stage")
    cmd_run(a)
    work = tmp_path / "work"
    run = read_run(work)
    assert run["status"] == "done" and run["executor"] == "flex"
    assert all("gpt-6-sol flex rates" in run[k]["note"] for k in ("forecast_before_search", "forecast_before_submit"))
    assert "Forecast before submitting at Flex rates (gpt-6-sol)" in capsys.readouterr().out
    n = run["plan_summary"]["searches"]
    assert len(searches.calls) == n and run["actual"]["totals"]["webiq_calls"] == n
    assert run["actual"]["usd"]["webiq"] == pytest.approx(n * 7 / 1000)
    assert sorted(fake.calls) == ["R0", "R1", "R2"]
    assert all("tools" not in b and b["service_tier"] == "flex" and b["model"] == "flex-dep" for b in fake.bodies)
    assert not (work / "evidence.jsonl").exists() and not (work / "batch-input.jsonl").exists() and (work / flex.LEDGER).exists()


def test_run_stops_with_a_message_naming_a_deployment_without_flex(tmp_path, flex_only):
    fake = flex_only(FakeFlex({"R0": [http_error(400, NOT_SUPPORTED)]}))
    a = run_args(tmp_path, "bounded-web-search", deployment="gpt-54-global", flex_concurrency=1)
    with pytest.raises(SystemExit, match="'gpt-54-global' doesn't support Flex processing"):
        cmd_run(a)
    work = tmp_path / "work"
    run = read_run(work)
    assert fake.calls == ["R0"] and run["status"] == "flex stopped" and run["flex"]["lines"]["not_finished"] == 3
    assert (work / "batch-input.jsonl").exists()  # kept for the re-run
    a.deployment = "gpt-6-sol-global"  # a Flex-capable deployment: the re-run sends every line
    cmd_run(a)
    assert read_run(work)["status"] == "done" and fake.calls == ["R0", "R0", "R1", "R2"]


def test_a_flex_job_with_lines_left_isnt_done_until_they_complete(tmp_path, flex_only, waits):
    flex_only(FakeFlex({"R1": [http_error(429, "busy") for _ in range(3)]}))
    a = run_args(tmp_path, "bounded-web-search", no_auto_retry=True)  # the manual flow: no automatic rounds
    assert cmd_run(a) == 3  # held: R1 used up its attempts
    work = tmp_path / "work"
    run = read_run(work)
    assert run["status"] == "flex incomplete" and run["flex"]["lines"]["used_up"] == 1
    assert run["actual"]["lines_with_usage"] == 2 and (work / "batch-input.jsonl").exists()
    assert [json.loads(x)["id"] for x in (work / "held-records.jsonl").read_text(encoding="utf-8").splitlines()] == ["R1"]
    a.retry_failed = True
    assert cmd_run(a) == 0
    run = read_run(work)
    assert run["status"] == "done" and run["actual"]["lines_with_usage"] == 3 and len(run["flex_sessions"]) == 2
    assert run["flex"]["attempts"]["not_billed_429"] == 3 and run["flex"]["attempts"]["requests"] == 6
    assert not (work / "held-records.jsonl").exists() and run["held"]["records"] == 0


def test_batch_and_flex_jobs_dont_share_a_work_folder(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "state.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="holds a Batch job"):
        cmd_run(run_args(tmp_path, "bounded-web-search"))
    (work / "state.json").unlink()
    (work / flex.LEDGER).write_text("", encoding="utf-8")
    with pytest.raises(SystemExit, match="holds a Flex job"):
        cmd_run(run_args(tmp_path, "bounded-web-search", executor="batch"))


# ---------- automatic rounds ----------


def test_lines_that_used_up_their_attempts_are_sent_again_in_automatic_rounds(tmp_path, waits, pauses, capsys):
    fake = FakeFlex({"A": [http_error(429, "busy") for _ in range(6)]})
    out = flex.run_lines(lines("A", "B"), tmp_path, deployment="d", client=fake, concurrency=1)
    assert fake.calls.count("A") == 7 and [r["status"] for r in out["rows"]] == ["completed", "completed"]
    assert pauses == [60, 300]  # round_waits_seconds, before round 2 and round 3
    assert [(r["round"], r["attempt"]) for r in ledger(tmp_path, "start") if r["custom_id"] == "A"] == [
        (1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3), (3, 1)]
    assert "Flex, automatic round 2 of 3: 1 lines" in capsys.readouterr().out
    assert out["summary"]["attempts"]["not_billed_429"] == 6 and out["summary"]["auto_rounds"] == 3


def test_after_the_automatic_rounds_a_line_is_held_until_retry_failed(tmp_path, waits, pauses):
    fake = FakeFlex({"A": [http_error(503, "busy") for _ in range(9)]})
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake)
    assert len(fake.calls) == 9 and out["rows"][0]["status"] == "held" and out["summary"]["lines"]["used_up"] == 1
    assert "round 3" in out["rows"][0]["error"]
    flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake)
    assert len(fake.calls) == 9  # it has had its 3 rounds: a plain re-run doesn't send it
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake, retry_failed=True)
    assert len(fake.calls) == 10 and out["rows"][0]["status"] == "completed"


def test_a_long_retry_after_is_waited_out_before_the_next_automatic_round(tmp_path, waits, pauses):
    fake = FakeFlex({"A": [http_error(429, "busy", {"retry-after": "7200"})]})
    out = flex.run_lines(lines("A"), tmp_path, deployment="d", client=fake)
    assert fake.calls == ["A", "A"] and out["rows"][0]["status"] == "completed" and waits == []
    assert pauses == [3600]  # what was left of the 7,200 s, capped at max_round_wait_seconds
    assert [(r["round"], r["attempt"]) for r in ledger(tmp_path, "start")] == [(1, 1), (1, 2)]  # its round, continued


def test_a_flex_run_ends_with_the_exit_code_of_what_is_left(tmp_path, flex_only, waits, pauses):
    flex_only(FakeFlex({"R1": [http_error(429, "busy", {"retry-after": "7200"})]}))
    with pytest.raises(SystemExit, match="Re-run the same command later") as ended:
        cmd_run(run_args(tmp_path, "bounded-web-search", no_auto_retry=True))
    assert ended.value.code == 75 and pauses == []  # asked to wait 2 hours, with no automatic round to wait it out in
    assert read_run(tmp_path / "work")["status"] == "flex incomplete"
    flex_only(FakeFlex({"R1": [http_error(429, "busy", {"retry-after": "7200"}) for _ in range(3)]}))
    assert cmd_run(run_args(tmp_path, "bounded-web-search", workdir=str(tmp_path / "held"))) == 3
    assert pauses == [3600, 3600]  # waited out, up to an hour, before rounds 2 and 3; then its 3 attempts were used up
    flex_only(FakeFlex({"R0": [http_error(401, "no access")]}))
    with pytest.raises(SystemExit, match="Sign in again") as ended:
        cmd_run(run_args(tmp_path, "bounded-web-search", workdir=str(tmp_path / "denied")))
    assert ended.value.code == 1  # needs a person