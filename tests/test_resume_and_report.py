import argparse
import asyncio
import json

import pytest

from report import check_text, redact_line
from search_plan import build_plan
from webiq import run_searches
from workloads import get_workload

RECORDS = [
    {"id": f"R{i}", "brand": f"Brand {i}", "market": "US", "claimed_owner": f"Owner {i % 2}", "claimed_owner_hq_country": "US"}
    for i in range(6)
]


def opts(**kw):
    base = dict(via="fake", max_results=3, max_chars=200, language="en", qps=1000.0, concurrency=1, max_attempts=3, max_queries=100)
    base.update(kw)
    return argparse.Namespace(**base)


class FakeWebIQ:
    """Answers every search; can interrupt the run after a number of calls, or fail some queries first."""

    name = "fake"

    def __init__(self, crash_after=None, fail_first=()):
        self.calls, self.crash_after, self.fail_first = [], crash_after, set(fail_first)
        self.main = None

    async def start(self, client):
        self.main = asyncio.current_task()

    async def call(self, client, args, req_id):
        if self.crash_after is not None and len(self.calls) == self.crash_after:
            # Like Ctrl+C under asyncio.run: the main task is cancelled while this call is in flight.
            self.main.cancel()
            await asyncio.sleep(3600)
        self.calls.append(args["query"])
        if args["query"] in self.fail_first:
            self.fail_first.discard(args["query"])
            return 429, None, "rate limited", "0"
        return 200, {"webResults": [{"title": "t", "url": f"https://example.com/{len(self.calls)}", "content": "c"}]}, None, None


def test_interrupted_search_resumes_without_repeating_finished_searches(tmp_path):
    plan = build_plan(RECORDS, get_workload("brand-owner"), "full")
    out = tmp_path / "evidence.jsonl"
    first = FakeWebIQ(crash_after=3)
    with pytest.raises(asyncio.CancelledError):
        run_searches(plan, out, opts(), api=first)
    assert len(out.read_text(encoding="utf-8").splitlines()) == 3  # written as each search finished
    with open(out, "a", encoding="utf-8") as f:
        f.write('{"query_id": "cut-off')  # a line cut short by the interruption is ignored

    second = FakeWebIQ()
    entries, stats = run_searches(plan, out, opts(), api=second)
    assert len(second.calls) == len(plan) - 3
    assert set(first.calls).isdisjoint(second.calls)
    assert stats["already_done"] == 3 and stats["searches_succeeded"] == len(plan) - 3
    assert all(e.get("results") and not e.get("error") for e in entries)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(plan)  # consolidated: one line per planned search
    assert [json.loads(line)["query_id"] for line in lines] == list(plan)


def test_retries_are_counted_by_status(tmp_path):
    plan = build_plan(RECORDS[:1], get_workload("brand-owner"), "full")
    query = next(iter(plan.values()))["query"]
    entries, stats = run_searches(plan, tmp_path / "e.jsonl", opts(), api=FakeWebIQ(fail_first=[query]))
    assert stats["failed_attempts_by_status"] == {"429": 1}
    assert stats["attempts"] == len(plan) + 1
    assert all(not e.get("error") for e in entries)


def test_build_refuses_evidence_with_failed_searches(tmp_path):
    from pipeline import cmd_build

    recs = tmp_path / "r.jsonl"
    recs.write_text("".join(json.dumps(r) + "\n" for r in RECORDS[:1]), encoding="utf-8")
    ev = tmp_path / "e.jsonl"
    ev.write_text(json.dumps({"query_id": "x", "query": "q", "region": None, "purpose": "owner_hq", "used_by": ["R0"], "error": "HTTP 500"}) + "\n", encoding="utf-8")
    a = argparse.Namespace(records=str(recs), workload="brand-owner", allow_problems=False, design="two-stage", plan="lean", evidence=str(ev), out=str(tmp_path / "b.jsonl"))
    with pytest.raises(SystemExit, match="failed"):
        cmd_build(a)


def test_path_checks_dont_match_inside_urls():
    assert not check_text('{"citations": ["https://example.com/home/about", "https://example.com/Users/guide"]}')
    assert check_text('{"source": "/home/alice/records.jsonl"}')
    assert check_text('{"source": "C:\\\\Users\\\\alice\\\\records.jsonl"}')


def test_redaction_removes_personal_urls_and_chosen_records():
    counts = {"personal_urls_removed": 0, "citations_removed": 0, "records_redacted": set()}
    fact = {"status": "verified", "finding": "text", "suggested_value": "US", "citations": ["https://uk.linkedin.com/in/someone", "https://example.com/a"]}
    kept = redact_line({"id": "A", "result": {"brand_owner": fact, "summary": "s"}}, {"B"}, counts)
    assert kept["result"]["brand_owner"]["citations"] == ["https://example.com/a"]
    assert kept["result"]["brand_owner"]["finding"] == "text"
    gone = redact_line({"id": "B", "result": {"brand_owner": fact, "summary": "s"}}, {"B"}, counts)
    assert gone["result"]["brand_owner"] == {"status": "verified", "finding": "[removed before publishing]", "suggested_value": "US", "citations": []}
    assert gone["result"]["summary"] == "[removed before publishing]"
    assert counts["personal_urls_removed"] == 1 and counts["citations_removed"] == 2 and counts["records_redacted"] == {"B"}
