import argparse
import json
from types import SimpleNamespace

import pytest

import batch as batchlib
from pipeline import cmd_run, remaining_lines
from report import check_text

RECORDS = [
    {"id": f"R{i}", "brand": f"Brand {i}", "market": "US", "claimed_owner": "Owner Inc.", "claimed_owner_hq_country": "US"} for i in range(3)
]


def body(custom_id: str) -> dict:
    answer = {"record_id": custom_id, "all_verified": True}
    return {
        "status": "completed",
        "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 10}},
        "tool_usage": {"web_search": {"num_requests": 2}},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
    }


class FakeOpenAI:
    """Files and Batches API stand-in. `outcomes` lists (final status, lines that complete) for each batch created."""

    def __init__(self, outcomes):
        self.outcomes, self.store, self.batches_by_id, self.submitted = list(outcomes), {}, {}, []
        self.files = SimpleNamespace(create=self._create, retrieve=lambda fid: SimpleNamespace(status="processed"), delete=self._delete, content=self._content)
        self.batches = SimpleNamespace(create=self._batch, retrieve=lambda bid: self.batches_by_id[bid], cancel=lambda bid: None)

    def _create(self, file, purpose, extra_body=None):
        assert extra_body == {"expires_after": batchlib.EXPIRES_AFTER}
        fid = f"file-{len(self.store)}-{len(self.submitted)}"
        self.store[fid] = file.read().decode("utf-8")
        return SimpleNamespace(id=fid)

    def _delete(self, fid):
        self.store.pop(fid)

    def _content(self, fid):
        return SimpleNamespace(text=self.store[fid])

    def _batch(self, input_file_id, endpoint, completion_window, extra_body=None):
        assert extra_body == {"output_expires_after": batchlib.EXPIRES_AFTER}
        ids = [json.loads(line)["custom_id"] for line in self.store[input_file_id].splitlines() if line.strip()]
        self.submitted.append(ids)
        status, n_done = self.outcomes.pop(0)
        out_id = f"out-{len(self.submitted)}"
        self.store[out_id] = "".join(json.dumps({"custom_id": i, "response": {"status_code": 200, "body": body(i)}}) + "\n" for i in ids[:n_done])
        b = SimpleNamespace(
            id=f"batch-{len(self.submitted)}", status=status, output_file_id=out_id, error_file_id=None, errors=None,
            request_counts=SimpleNamespace(total=len(ids), completed=n_done, failed=0), created_at=1000, in_progress_at=1010, completed_at=1100,
        )
        self.batches_by_id[b.id] = b
        return b


def run_args(tmp_path, records_file):
    return argparse.Namespace(
        records=str(records_file), workload="brand-owner", allow_problems=False, design="web-search", plan="lean",
        workdir=str(tmp_path / "work"), label=None, deployment="dep", price_model="gpt-5.4-mini", prices=None,
        reasoning_effort="low", max_output_tokens=8000, max_tool_calls=2, output_tokens_per_line=None, max_cost=None,
        base_url="https://example.invalid/openai/v1/", subscription=None, poll_seconds=0, stall_minutes=120,
        cancel_on_stall=False, keep_working_files=False,
    )


def test_remaining_lines():
    lines = [{"custom_id": "a"}, {"custom_id": "b"}]
    assert remaining_lines(lines, {"a"}) == [{"custom_id": "b"}]


def test_cancelled_batch_keeps_completed_lines_and_resubmits_only_the_rest(tmp_path, monkeypatch):
    records_file = tmp_path / "records.jsonl"
    records_file.write_text("".join(json.dumps(r) + "\n" for r in RECORDS), encoding="utf-8")
    fake = FakeOpenAI([("cancelled", 2), ("completed", 1)])
    monkeypatch.setattr(batchlib, "make_client", lambda *a, **k: fake)
    a = run_args(tmp_path, records_file)
    work = tmp_path / "work"

    with pytest.raises(SystemExit, match="cancelled"):
        cmd_run(a)
    run = json.loads((work / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "batch cancelled" and run["incomplete_batches"][0]["lines_kept"] == 2
    assert not (work / "state.json").exists() and (work / "kept-lines.jsonl").exists()

    cmd_run(a)
    assert fake.submitted == [["R0", "R1", "R2"], ["R2"]]  # the re-run submits only the line that didn't complete
    run = json.loads((work / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "done"
    assert run["actual"]["lines_with_usage"] == 3  # lines billed by the cancelled batch are counted once
    assert run["actual"]["totals"]["web_search_requests"] == 6
    results = [json.loads(line) for line in (work / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["id"] for r in results] == ["R0", "R1", "R2"]
    assert not (work / "kept-lines.jsonl").exists() and not (work / "batch-input.jsonl").exists() and not (work / "state.json").exists()
    assert fake.store == {}  # every input and output file was deleted from the file store


def test_a_resumed_job_keeps_the_prices_it_started_with(tmp_path, monkeypatch, capsys):
    records_file = tmp_path / "records.jsonl"
    records_file.write_text("".join(json.dumps(r) + "\n" for r in RECORDS), encoding="utf-8")
    fake = FakeOpenAI([("cancelled", 2), ("completed", 1)])
    monkeypatch.setattr(batchlib, "make_client", lambda *a, **k: fake)
    a = run_args(tmp_path, records_file)
    work = tmp_path / "work"
    with pytest.raises(SystemExit, match="cancelled"):
        cmd_run(a)
    started = json.loads((work / "run.json").read_text(encoding="utf-8"))["prices"]
    table = json.loads((work / "prices-used.json").read_text(encoding="utf-8"))
    assert started["file"] == "prices-used.json" and started["sha256"]
    assert not check_text((work / "prices-used.json").read_text(encoding="utf-8"))  # shareable: no endpoints or paths

    dearer = json.loads(json.dumps(table))
    dearer["models"]["gpt-5.4-mini"]["global_batch"]["input_per_1m"] *= 10
    (tmp_path / "dearer.json").write_text(json.dumps(dearer), encoding="utf-8")
    a.prices = str(tmp_path / "dearer.json")  # list prices changed between the two parts of the job
    cmd_run(a)
    run = json.loads((work / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "done" and run["prices"] == started
    assert run["actual"]["prices"]["rates"] == table["models"]["gpt-5.4-mini"]["global_batch"]
    assert "applies to new jobs only" in capsys.readouterr().out
    assert (work / "prices-used.json").exists()  # kept with run.json after the working files are deleted
