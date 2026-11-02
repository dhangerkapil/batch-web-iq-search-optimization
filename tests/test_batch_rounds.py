"""Global Batch, set and forget, offline: a fake Files and Batches API stands in for the service, so nothing is sent or
billed. Large jobs are split, a full quota is waited out, lines that may pass are sent again in automatic rounds, and
what can't be answered is held and listed."""
import argparse
import json
import time
from types import SimpleNamespace

import httpx
import openai
import pytest

import batch as batchlib
import webiq
from pipeline import cmd_run

RECORDS = [
    {"id": rid, "brand": f"Brand {rid}", "market": "US", "claimed_owner": f"Owner {i % 2}", "claimed_owner_hq_country": "US"}
    for i, rid in enumerate(["R4", "R2", "R0", "R3", "R1"])
]
IDS = [r["id"] for r in RECORDS]


def body(cid, status="completed"):
    answer = {"record_id": cid, "all_verified": True}
    return {
        "status": status,
        "usage": {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 100, "output_tokens_details": {"reasoning_tokens": 10}},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}],
    }


def quota_error():
    """What creating a job returns while the deployment's enqueued-token quota is full."""
    error = {"code": "token_limit_exceeded", "message": "Enqueued token limit reached for this deployment."}
    response = httpx.Response(400, request=httpx.Request("POST", "https://example.invalid/openai/v1/batches"), json={"error": error})
    return openai.BadRequestError("Error code: 400 - token_limit_exceeded", response=response, body=error)


class FakeBatchAPI:
    """Files and Batches API stand-in. `jobs` says how each job created ends, in order: status (default completed),
    answer (ids answered; default all), errors ({id: HTTP status}), incomplete (ids answered with an incomplete
    response), job_errors ([code] of a failed job) and polls (polls still in_progress before it ends; a job this run
    cancels ends at the next poll). The other arguments are exceptions, used once each, in order: `refused`, raised by
    a create before anything is created; `lost`, raised by a create after its job was created (its answer is lost: a
    timeout, or the run is stopped); `list_errors`, raised by listing the jobs. `quota_full` makes that many creates
    fail with token_limit_exceeded. Like the SDK, a create sent with retries on is sent again after a lost answer."""

    def __init__(self, jobs, quota_full=0, lost=(), refused=(), list_errors=()):
        self.jobs, self.quota_full, self.lost, self.refused = list(jobs), quota_full, list(lost), list(refused)
        self.list_errors = list(list_errors)
        self.store, self.by_id, self.submitted, self.creates, self.cancelled, self.uploads = {}, {}, [], [], [], 0
        self.create_retries = []  # max_retries each create was sent with
        self.files = SimpleNamespace(create=self._upload, retrieve=lambda fid: SimpleNamespace(status="processed"),
                                     delete=self._delete, content=lambda fid: SimpleNamespace(text=self.store[fid]))
        self.batches = self._batches(max_retries=6)  # the SDK's default here: [azure_openai] sdk_max_retries

    def _batches(self, max_retries):
        return SimpleNamespace(create=lambda **kw: self._create(**kw, max_retries=max_retries), retrieve=self._retrieve,
                               cancel=self.cancelled.append, list=self._list)

    def with_options(self, max_retries=None, **kw):
        return SimpleNamespace(files=self.files, batches=self._batches(max_retries))

    def _upload(self, file, purpose, extra_body=None):
        assert extra_body == {"expires_after": batchlib.EXPIRES_AFTER}
        self.uploads += 1
        fid = f"file-in-{self.uploads}"
        self.store[fid] = file.read().decode("utf-8")
        return SimpleNamespace(id=fid)

    def _delete(self, fid):
        self.store.pop(fid)

    def _list(self, limit=100):
        if self.list_errors:
            raise self.list_errors.pop(0)
        return [b for b, _, _ in reversed(list(self.by_id.values()))]  # newest first

    def _create(self, input_file_id, endpoint, completion_window, extra_body=None, max_retries=6):
        assert input_file_id in self.store  # the uploaded file is still there
        self.creates.append(input_file_id)
        self.create_retries.append(max_retries)
        if self.refused:
            raise self.refused.pop(0)
        if self.quota_full:
            self.quota_full -= 1
            raise quota_error()
        ids = [json.loads(line)["custom_id"] for line in self.store[input_file_id].splitlines() if line.strip()]
        self.submitted.append(ids)
        n = len(self.submitted)
        b = SimpleNamespace(id=f"batch-{n}", status="validating", input_file_id=input_file_id, output_file_id=None,
                            error_file_id=None, errors=None, request_counts=SimpleNamespace(total=len(ids), completed=0, failed=0),
                            created_at=1000 * n, in_progress_at=None, completed_at=None)
        self.by_id[b.id] = (b, ids, dict(self.jobs.pop(0)))
        if self.lost:
            err = self.lost.pop(0)
            if max_retries and isinstance(err, openai.APIConnectionError):
                # What the SDK does with its retries on: send the same create again, with no idempotency key.
                return self._create(input_file_id, endpoint, completion_window, extra_body, max_retries - 1)
            raise err  # the job was created, but its answer never reached the run
        return b

    def _retrieve(self, bid):
        b, ids, spec = self.by_id[bid]
        if b.status not in batchlib.FINAL_STATES:
            if spec.get("polls", 0) > 0 and bid not in self.cancelled:
                spec["polls"] -= 1
                b.status = "in_progress"
            else:
                self._end(b, ids, spec)
        return b

    def _end(self, b, ids, spec):
        b.status = "cancelled" if b.id in self.cancelled else spec.get("status", "completed")
        if b.status == "failed":
            b.errors = SimpleNamespace(data=[SimpleNamespace(code=c, message=f"{c} in the input file") for c in spec.get("job_errors", ["invalid_request"])])
            return
        answer, errors, incomplete = spec.get("answer", ids), spec.get("errors", {}), spec.get("incomplete", ())
        out, err = [], []
        for cid in ids:
            if cid in errors:
                err.append({"custom_id": cid, "response": {"status_code": errors[cid], "body": {"error": {"code": "e", "message": f"HTTP {errors[cid]}"}}}})
            elif cid in answer:
                out.append({"custom_id": cid, "response": {"status_code": 200, "body": body(cid, "incomplete" if cid in incomplete else "completed")}})
        n = b.id.split("-")[1]
        b.output_file_id, b.error_file_id = f"out-{n}", (f"err-{n}" if err else None)
        self.store[b.output_file_id] = "".join(json.dumps(o) + "\n" for o in out)
        if err:
            self.store[b.error_file_id] = "".join(json.dumps(o) + "\n" for o in err)
        b.request_counts = SimpleNamespace(total=len(ids), completed=len(out), failed=len(err))
        b.completed_at = b.created_at + 600 if b.status == "completed" else None


def run_args(tmp_path, records=RECORDS, **kw):
    path = tmp_path / "records.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    a = dict(
        records=str(path), workload="brand-owner", allow_problems=False, design="web-search", plan="lean", workdir=str(tmp_path / "work"),
        label=None, deployment="dep", price_model="gpt-5.4-mini", prices=None, reasoning_effort="low", max_output_tokens=8000,
        max_tool_calls=2, output_tokens_per_line=None, max_cost=None, base_url="https://example.invalid/openai/v1/", subscription=None,
        poll_seconds=0, stall_minutes=120, cancel_on_stall=False, keep_working_files=False, max_wait_minutes=0, no_auto_retry=False,
        via="fake", toolbox_url=None, toolbox_tool=None, webiq_endpoint=None, webiq_auth="key", max_results=3, max_chars=200,
        language="en", qps=1000.0, concurrency=1, max_attempts=2, max_queries=None,
    )
    a.update(kw)
    return argparse.Namespace(**a)


@pytest.fixture
def fake_batch(monkeypatch):
    """Use `fake = fake_batch(FakeBatchAPI(...))`; batch waits are recorded instead of slept."""
    waits = []
    monkeypatch.setattr(batchlib, "_sleep", waits.append)

    def use(fake):
        fake.waits = waits
        monkeypatch.setattr(batchlib, "make_client", lambda *a, **k: fake)
        return fake

    return use


def work(tmp_path):
    return tmp_path / "work"


def read(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def run_json(tmp_path):
    return json.loads((work(tmp_path) / "run.json").read_text(encoding="utf-8"))


def test_split_parts_cuts_at_the_line_and_byte_limits(monkeypatch):
    lines = [{"custom_id": f"L{i}", "body": {"text": "x" * 50}} for i in range(7)]
    size = batchlib.line_bytes(lines[0])
    assert batchlib.split_parts(lines) == [lines]  # within the limits: one file
    monkeypatch.setattr(batchlib, "MAX_LINES", 3)
    assert [len(p) for p in batchlib.split_parts(lines)] == [3, 3, 1]
    monkeypatch.setattr(batchlib, "MAX_LINES", 100)
    monkeypatch.setattr(batchlib, "MAX_BYTES", 5 * size)
    monkeypatch.setattr(batchlib, "PART_BYTES", 2 * size)
    parts = batchlib.split_parts(lines)
    assert [len(p) for p in parts] == [2, 2, 2, 1] and [x for p in parts for x in p] == lines  # in input order


def test_a_job_too_big_for_one_file_runs_as_several_jobs_merged_in_input_order(tmp_path, fake_batch, monkeypatch):
    monkeypatch.setattr(batchlib, "MAX_LINES", 2)
    fake = fake_batch(FakeBatchAPI([{}, {"polls": 2}, {}]))
    assert cmd_run(run_args(tmp_path)) == 0
    assert fake.submitted == [["R4", "R2"], ["R0", "R3"], ["R1"]]  # each part its own job, submitted before any is waited on
    assert [r["id"] for r in read(work(tmp_path) / "results.jsonl")] == IDS  # merged in the records' input order
    run = run_json(tmp_path)
    assert run["status"] == "done" and [b["part"] for b in run["batches"]] == [1, 2, 3]
    assert run["batch"]["jobs"] == 3 and run["batch"]["request_counts"]["completed"] == 5
    assert run["actual"]["lines_with_usage"] == 5 and fake.store == {}  # every file deleted
    assert not (work(tmp_path) / "state.json").exists() and not (work(tmp_path) / "batch-input-part.jsonl").exists()


def test_a_full_quota_is_waited_out_and_the_upload_kept(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{}], quota_full=3))
    assert cmd_run(run_args(tmp_path)) == 0
    assert fake.waits == [5, 10, 20]  # 5 s, doubling
    assert fake.uploads == 1 and fake.creates == ["file-in-1"] * 4  # the same upload, created once the quota allowed it
    assert run_json(tmp_path)["status"] == "done"


def test_when_the_quota_stays_full_the_run_exits_75_and_a_rerun_creates_the_job_from_the_upload(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{}], quota_full=10**6))
    a = run_args(tmp_path)
    with pytest.raises(SystemExit, match="uploaded file is kept") as ended:
        cmd_run(a)
    assert ended.value.code == 75
    assert sum(fake.waits) == pytest.approx(1440 * 60) and max(fake.waits) == 600  # quota_wait_minutes, waits capped at 600 s
    state = json.loads((work(tmp_path) / "state.json").read_text(encoding="utf-8"))
    assert state["jobs"][0]["input_file_id"] == "file-in-1" and "batch_id" not in state["jobs"][0]
    assert "file-in-1" in fake.store and run_json(tmp_path)["status"] == "waiting for quota"
    fake.quota_full = 0
    assert cmd_run(a) == 0
    assert fake.uploads == 1 and fake.submitted == [IDS]  # no second upload


def test_lines_that_failed_with_429_or_5xx_are_sent_again_in_automatic_rounds(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"errors": {"R2": 429, "R3": 500}}, {"errors": {"R3": 503}}, {}]))
    assert cmd_run(run_args(tmp_path)) == 0
    assert fake.submitted == [IDS, ["R2", "R3"], ["R3"]]
    results = read(work(tmp_path) / "results.jsonl")
    assert [r["id"] for r in results] == IDS and all(r["status"] == "completed" for r in results)
    run = run_json(tmp_path)
    assert run["batch_rounds"] == 3 and len(run["batches"]) == 3 and run["held"]["records"] == 0
    assert run["actual"]["lines_with_usage"] == 5 and fake.store == {}


def test_lines_still_failing_after_the_last_round_are_held_and_listed(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"errors": {"R2": 429}}] * 3))
    a = run_args(tmp_path)
    assert cmd_run(a) == 3
    assert fake.submitted == [IDS, ["R2"], ["R2"]]
    results = {r["id"]: r for r in read(work(tmp_path) / "results.jsonl")}
    assert results["R2"]["status"] == "held" and "after 3 rounds" in results["R2"]["error"] and "429" in results["R2"]["error"]
    assert read(work(tmp_path) / "held-records.jsonl") == [RECORDS[1]]  # the input record, to run again as a new job
    run = run_json(tmp_path)
    assert run["status"] == "done" and run["held"] == {"records": 1, "search_failed": 0, "not_answered": 1, "file": "held-records.jsonl"}
    assert run["actual"]["lines_held"] == 1 and run["actual"]["lines_with_usage"] == 4 and run["actual"]["lines_failed"] == 0
    assert cmd_run(a) == 3 and len(fake.submitted) == 3  # done: a re-run sends nothing and gives the same answer


def test_client_errors_and_incomplete_answers_are_final(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"errors": {"R2": 400}, "incomplete": {"R0"}}]))
    assert cmd_run(run_args(tmp_path)) == 0  # failed for good is done: retrying can't fix it
    assert fake.submitted == [IDS]
    results = {r["id"]: r for r in read(work(tmp_path) / "results.jsonl")}
    assert results["R2"]["status"] == "error" and "400" in results["R2"]["error"]
    assert results["R0"]["status"] == "incomplete"  # answered: sending it again would pay twice
    assert run_json(tmp_path)["actual"]["lines_failed"] == 1


def test_an_expired_job_sends_the_lines_it_never_processed_again(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"status": "expired", "answer": ["R4", "R2"]}, {}]))
    assert cmd_run(run_args(tmp_path)) == 0
    assert fake.submitted == [IDS, ["R0", "R3", "R1"]]
    assert run_json(tmp_path)["actual"]["lines_with_usage"] == 5


def test_a_job_cancelled_outside_the_run_holds_its_unanswered_lines(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"status": "cancelled", "answer": ["R4", "R2", "R0"]}]))
    assert cmd_run(run_args(tmp_path)) == 3
    assert fake.submitted == [IDS]  # someone cancelled it: nothing is sent again automatically
    results = {r["id"]: r for r in read(work(tmp_path) / "results.jsonl")}
    assert results["R3"]["status"] == results["R1"]["status"] == "held" and "cancelled outside this run" in results["R3"]["error"]
    assert [r["id"] for r in read(work(tmp_path) / "held-records.jsonl")] == ["R3", "R1"]


def test_a_job_this_run_cancelled_on_stall_is_sent_again(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"polls": 10**6, "answer": ["R4"]}, {}]))
    assert cmd_run(run_args(tmp_path, stall_minutes=-1, cancel_on_stall=True)) == 0  # stalled at the second poll
    assert fake.cancelled == ["batch-1"] and fake.submitted == [IDS, ["R2", "R0", "R3", "R1"]]
    assert run_json(tmp_path)["batches"][0]["status"] == "cancelled"


def test_a_job_that_failed_validation_needs_a_person_then_a_rerun_sends_it_again(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"status": "failed"}, {}]))
    a = run_args(tmp_path)
    with pytest.raises(SystemExit, match="failed validation") as ended:
        cmd_run(a)
    assert ended.value.code == 1 and fake.submitted == [IDS] and run_json(tmp_path)["status"] == "batch failed"
    assert cmd_run(a) == 0 and fake.submitted == [IDS, IDS]


def test_a_job_that_failed_because_the_quota_was_full_is_created_again_without_using_a_round(tmp_path, fake_batch):
    # Outside the regions that refuse the create at once, a job created while the enqueued-token quota is full validates,
    # then fails with token_limit_exceeded.
    full = {"status": "failed", "job_errors": ["token_limit_exceeded"]}
    fake = fake_batch(FakeBatchAPI([full, full, {"errors": {"R2": 429}}, {"errors": {"R2": 429}}, {}]))
    assert cmd_run(run_args(tmp_path)) == 0
    assert fake.creates[:3] == ["file-in-1"] * 3 and fake.waits == [5, 10]  # the same upload, after the quota backoff
    assert fake.submitted == [IDS, IDS, IDS, ["R2"], ["R2"]]  # R2 still got its 3 rounds
    run = run_json(tmp_path)
    assert run["batch_rounds"] == 3 and [b["status"] for b in run["batches"]][:2] == ["failed", "failed"]
    assert fake.store == {}


def test_when_jobs_keep_failing_for_quota_the_run_exits_75_and_a_rerun_creates_them_again(tmp_path, fake_batch, monkeypatch):
    monkeypatch.setitem(batchlib._B, "quota_wait_minutes", 1)
    full = {"status": "failed", "job_errors": ["token_limit_exceeded"]}
    fake = fake_batch(FakeBatchAPI([full] * 5 + [{}]))
    a = run_args(tmp_path)
    with pytest.raises(SystemExit, match="uploaded files are kept") as ended:
        cmd_run(a)
    assert ended.value.code == 75 and fake.waits == [5, 10, 20, 25] and len(fake.submitted) == 5  # 60 s of waiting in all
    assert run_json(tmp_path)["status"] == "waiting for quota" and "file-in-1" in fake.store
    assert cmd_run(a) == 0  # the re-run creates the job again, from the same upload
    assert fake.uploads == 1 and len(fake.submitted) == 6 and run_json(tmp_path)["batch_rounds"] == 1


def test_a_part_that_failed_validation_still_needs_a_person_after_the_other_parts_finish(tmp_path, fake_batch, monkeypatch):
    monkeypatch.setattr(batchlib, "MAX_LINES", 3)
    fake = fake_batch(FakeBatchAPI([{"status": "failed"}, {"errors": {"R3": 429}}, {}, {"errors": {"R2": 503}}, {}]))
    a = run_args(tmp_path)
    with pytest.raises(SystemExit, match="failed validation") as ended:
        cmd_run(a)
    assert ended.value.code == 1 and fake.submitted == [["R4", "R2", "R0"], ["R3", "R1"], ["R3"]]  # the other part's rounds ran
    # After the fix, only the failed part is sent again, and its lines get every automatic round again: R2's 503 in the
    # first of them is sent again, not held.
    assert cmd_run(a) == 0 and fake.submitted[3:] == [["R4", "R2", "R0"], ["R2"]]


def test_a_rerun_after_no_auto_retry_gets_every_automatic_round(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"errors": {"R2": 429}}] * 3 + [{}]))
    with pytest.raises(SystemExit, match="--no-auto-retry"):
        cmd_run(run_args(tmp_path, no_auto_retry=True))
    assert cmd_run(run_args(tmp_path)) == 0  # rounds 2, 3 and 4 of the job, the first three of this run
    assert fake.submitted == [IDS, ["R2"], ["R2"], ["R2"]]


def test_a_create_whose_answer_was_lost_is_found_and_not_made_twice(tmp_path, fake_batch):
    timeout = openai.APITimeoutError(request=httpx.Request("POST", "https://example.invalid/openai/v1/batches"))
    fake = fake_batch(FakeBatchAPI([{}], lost=[timeout]))  # one job spec: a create the SDK sent again would need a second
    assert cmd_run(run_args(tmp_path)) == 0
    assert len(fake.submitted) == 1 and fake.waits == [5]  # looked for after a pause, found, and waited for
    assert fake.create_retries == [0]  # sent with the SDK's own retries off: it would have sent the create again

    fake = fake_batch(FakeBatchAPI([{}], lost=[KeyboardInterrupt()]))  # the run is stopped while the create is in flight
    a = run_args(tmp_path, workdir=str(tmp_path / "stopped"))
    with pytest.raises(KeyboardInterrupt):
        cmd_run(a)
    state = json.loads((tmp_path / "stopped" / "state.json").read_text(encoding="utf-8"))
    assert state["jobs"][0]["create"] == "sent" and "batch_id" not in state["jobs"][0]
    assert cmd_run(a) == 0 and len(fake.submitted) == 1  # the re-run found the job instead of creating a second one


def test_when_the_jobs_cant_be_listed_after_a_lost_create_nothing_is_deleted_and_the_rerun_lists_first(tmp_path, fake_batch):
    request = httpx.Request("GET", "https://example.invalid/openai/v1/batches")
    fake = fake_batch(FakeBatchAPI([{}], lost=[openai.APITimeoutError(request=request)],
                                   list_errors=[openai.APIConnectionError(request=request)]))
    a = run_args(tmp_path)
    with pytest.raises(SystemExit, match="lists the batch jobs first") as ended:
        cmd_run(a)
    assert ended.value.code == 75  # not 1: the job may exist, so nothing may be deleted or made again yet
    job = json.loads((work(tmp_path) / "state.json").read_text(encoding="utf-8"))["jobs"][0]
    assert job["create"] == "uncertain" and job["input_file_id"] == "file-in-1" and "file-in-1" in fake.store
    assert cmd_run(a) == 0
    assert fake.uploads == 1 and len(fake.submitted) == 1  # the re-run listed the jobs first and waited for batch-1


@pytest.mark.parametrize("refusal", ["HTTP 403", "no Entra ID token"])
def test_when_listing_the_jobs_is_refused_after_a_lost_create_the_run_needs_a_person(tmp_path, fake_batch, refusal):
    from azure.identity import CredentialUnavailableError

    request = httpx.Request("GET", "https://example.invalid/openai/v1/batches")
    err = (openai.PermissionDeniedError("Error code: 403 - forbidden", body={"code": "forbidden"},
                                        response=httpx.Response(403, request=request, json={"error": {"code": "forbidden"}}))
           if refusal == "HTTP 403" else CredentialUnavailableError("Please run 'az login' to set up an account"))
    fake = fake_batch(FakeBatchAPI([{}], lost=[openai.APITimeoutError(request=request)], list_errors=[err]))
    a = run_args(tmp_path)
    with pytest.raises(SystemExit, match="Fix the sign-in") as ended:
        cmd_run(a)
    assert ended.value.code == 1  # not 75: retrying won't fix a sign-in or access problem, so a person is told
    job = json.loads((work(tmp_path) / "state.json").read_text(encoding="utf-8"))["jobs"][0]
    assert job["create"] == "uncertain" and job["maybe_created"] and job["input_file_id"] == "file-in-1" and "file-in-1" in fake.store
    assert cmd_run(a) == 0  # after the fix: the jobs are listed first, and batch-1 is waited for
    assert fake.uploads == 1 and len(fake.submitted) == 1


def test_a_refusal_after_a_create_that_got_no_answer_keeps_the_upload(tmp_path, fake_batch):
    request = httpx.Request("POST", "https://example.invalid/openai/v1/batches")
    refusal = openai.BadRequestError("Error code: 400 - invalid_request", body={"code": "invalid_request"},
                                     response=httpx.Response(400, request=request, json={"error": {"code": "invalid_request"}}))
    fake = fake_batch(FakeBatchAPI([{}], refused=[openai.APITimeoutError(request=request), refusal]))
    a = run_args(tmp_path)
    with pytest.raises(openai.BadRequestError):
        cmd_run(a)
    job = json.loads((work(tmp_path) / "state.json").read_text(encoding="utf-8"))["jobs"][0]
    # The first attempt may have created the job, so its upload and the record of the attempts stay.
    assert job["input_file_id"] == "file-in-1" and job["maybe_created"] and job["attempts"] == 2 and "file-in-1" in fake.store
    assert cmd_run(a) == 0 and fake.uploads == 1 and len(fake.submitted) == 1  # listed first, then created once


def test_when_the_wait_runs_out_after_a_create_that_got_no_answer_it_says_the_job_may_exist(tmp_path, fake_batch, monkeypatch):
    monkeypatch.setitem(batchlib._B, "quota_wait_minutes", 1)
    request = httpx.Request("POST", "https://example.invalid/openai/v1/batches")
    fake = fake_batch(FakeBatchAPI([{}], refused=[openai.APITimeoutError(request=request)], quota_full=10**6))
    with pytest.raises(SystemExit, match="may or may not exist") as ended:
        cmd_run(run_args(tmp_path))
    assert ended.value.code == 75 and "wasn't created" not in str(ended.value) and "file-in-1" in fake.store


def test_max_wait_minutes_stops_waiting_with_exit_75_and_a_rerun_collects(tmp_path, fake_batch, monkeypatch):
    fake = fake_batch(FakeBatchAPI([{"polls": 3}]))
    monkeypatch.setattr(batchlib, "_sleep", lambda seconds: time.sleep(0.002))  # time passes between polls
    a = run_args(tmp_path, max_wait_minutes=1e-6)
    with pytest.raises(SystemExit, match="keep running") as ended:
        cmd_run(a)
    assert ended.value.code == 75 and run_json(tmp_path)["status"] == "batch running"
    assert json.loads((work(tmp_path) / "state.json").read_text(encoding="utf-8"))["jobs"][0]["batch_id"] == "batch-1"
    a.max_wait_minutes = 0  # wait until it ends
    assert cmd_run(a) == 0 and fake.submitted == [IDS]  # collected, not submitted again


def test_without_automatic_rounds_failed_lines_wait_for_a_rerun(tmp_path, fake_batch):
    fake = fake_batch(FakeBatchAPI([{"errors": {"R2": 429}}, {}]))
    a = run_args(tmp_path, no_auto_retry=True)
    with pytest.raises(SystemExit, match="--no-auto-retry") as ended:
        cmd_run(a)
    assert ended.value.code == 75 and fake.submitted == [IDS]
    assert cmd_run(a) == 0 and fake.submitted == [IDS, ["R2"]]


# ---------- Web IQ searches that still fail: their records are held ----------


class FakeWebIQ:
    """Answers every search, except `fail` ({query word: HTTP status}) for searches whose query contains the word."""

    name = "fake"

    def __init__(self, fail=None):
        self.calls, self.fail = [], dict(fail or {})

    async def start(self, client):
        return None

    async def call(self, client, args, req_id):
        self.calls.append(args["query"])
        for word, status in self.fail.items():
            if word in args["query"]:
                return status, None, f"failed with {status}", "0"
        return 200, {"webResults": [{"title": "t", "url": f"https://example.com/{len(self.calls)}", "content": "c"}]}, None, None


@pytest.fixture
def searches(monkeypatch):
    pauses = []

    async def no_wait(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(webiq, "_sleep", no_wait)

    def use(fake):
        fake.pauses = pauses
        monkeypatch.setattr(webiq, "make_client", lambda opts: fake)
        return fake

    return use


def test_records_whose_search_still_failed_are_held_and_the_others_are_sent(tmp_path, fake_batch, searches):
    web = searches(FakeWebIQ(fail={"Brand R2 ": 429}))
    fake = fake_batch(FakeBatchAPI([{}]))
    assert cmd_run(run_args(tmp_path, design="two-stage")) == 3
    failed = [q for q in web.calls if "Brand R2 " in q]
    assert len(failed) == 3 * 2 and web.pauses.count(60) == 1 and web.pauses.count(300) == 1  # 3 rounds of 2 attempts
    assert len(fake.submitted) == 1 and sorted(fake.submitted[0]) == sorted(i for i in IDS if i != "R2")  # R2 isn't sent
    results = {r["id"]: r for r in read(work(tmp_path) / "results.jsonl")}
    assert results["R2"]["status"] == "held" and "Brand R2 brand owner" in results["R2"]["error"] and "429" in results["R2"]["error"]
    assert read(work(tmp_path) / "held-records.jsonl") == [RECORDS[1]]
    run = run_json(tmp_path)
    assert run["held"]["search_failed"] == 1 and run["stage1"]["errors"] == 1 and run["stage1"]["rounds"] == 3
    assert run["actual"]["totals"]["webiq_calls"] == run["plan_summary"]["searches"] - 1  # the failed search isn't priced
    assert not (work(tmp_path) / "evidence.jsonl").exists()


def test_when_every_record_is_held_nothing_is_sent(tmp_path, fake_batch, searches, monkeypatch):
    def no_batch(*a, **k):
        raise AssertionError("nothing may be sent to the model")

    searches(FakeWebIQ(fail={"headquarters": 503}))
    monkeypatch.setattr(batchlib, "make_client", no_batch)
    assert cmd_run(run_args(tmp_path, design="two-stage")) == 3  # retrying may fix it later: held
    assert [r["status"] for r in read(work(tmp_path) / "results.jsonl")] == ["held"] * 5
    assert read(work(tmp_path) / "held-records.jsonl") == RECORDS and run_json(tmp_path)["status"] == "done"

    searches(FakeWebIQ(fail={"headquarters": 403}))
    with pytest.raises(SystemExit, match="retrying can't fix") as ended:
        cmd_run(run_args(tmp_path, design="two-stage", workdir=str(tmp_path / "denied")))
    assert ended.value.code == 1  # sign-in or permissions: a person fixes it, then re-runs the same command


def test_without_automatic_rounds_a_failed_search_stops_the_job_before_submitting(tmp_path, fake_batch, searches):
    web = searches(FakeWebIQ(fail={"Brand R2 ": 429}))
    fake = fake_batch(FakeBatchAPI([{}]))
    with pytest.raises(SystemExit, match="nothing was submitted") as ended:
        cmd_run(run_args(tmp_path, design="two-stage", no_auto_retry=True))
    assert ended.value.code == 75 and fake.submitted == [] and 60 not in web.pauses  # one round only
    web.fail = {}
    assert cmd_run(run_args(tmp_path, design="two-stage", no_auto_retry=True)) == 0  # the re-run retries only that search
    assert sorted(fake.submitted[0]) == sorted(IDS) and sum("Brand R2 " in q for q in web.calls) == 3


def test_the_search_cap_scales_with_the_records(tmp_path, searches):
    from search_plan import build_plan
    from workloads import get_workload

    plan = build_plan(RECORDS, get_workload("brand-owner"), "full")
    assert webiq.query_cap(plan, argparse.Namespace(max_queries=None)) == 3 * len(RECORDS)
    assert webiq.query_cap(plan, argparse.Namespace(max_queries=2)) == 2
    searches(FakeWebIQ())
    with pytest.raises(SystemExit, match="--max-queries"):
        webiq.run_searches(plan, tmp_path / "e.jsonl", run_args(tmp_path, max_queries=2))


class FakeTokens:
    def __init__(self, scope, subscription=None):
        pass

    async def get(self):
        return "token"

    def reset(self):
        pass


def relayed(status, reason):
    """How a Foundry toolbox relays an HTTP error from Web IQ: a JSON-RPC error in an HTTP 200 response."""
    body = json.dumps({"error": {"code": "denied", "message": reason}})
    return {"error": {"code": -32008, "message": "Error encountered while calling tool web from remote server: https://api.microsoft.ai:443/"
                      f"v3/mcp. Details: Response status code does not indicate success: {status} ({reason}). Response body: {body}",
                      "data": {"grpcStatus": "PermissionDenied"}}}


@pytest.fixture
def toolbox(monkeypatch, searches):
    """A Foundry toolbox's MCP endpoint behind httpx's mock transport. `answer(search arguments)` gives the JSON-RPC
    result or error of each tools/call."""
    box = SimpleNamespace(calls=[], answer=None)

    def handler(request):
        msg = json.loads(request.content)
        if msg.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {}}, headers={"mcp-session-id": "s"})
        if msg.get("method") != "tools/call":
            return httpx.Response(202)
        box.calls.append(msg["params"]["arguments"]["query"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], **box.answer(msg["params"]["arguments"])})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kw: real(*args, transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(webiq, "_TokenCache", FakeTokens)
    return box


def test_when_the_toolbox_relays_a_refusal_for_every_search_the_run_needs_a_person(tmp_path, fake_batch, toolbox):
    toolbox.answer = lambda args: relayed(403, "Forbidden")
    fake = fake_batch(FakeBatchAPI([{}]))
    a = run_args(tmp_path, design="two-stage", via="toolbox", toolbox_url="https://example.invalid/toolboxes/t/mcp?api-version=v1",
                 toolbox_tool="webiq___web")
    with pytest.raises(SystemExit, match="HTTP 403") as ended:
        cmd_run(a)
    assert ended.value.code == 1 and run_json(tmp_path)["status"] == "searches failed" and fake.submitted == []
    assert len(toolbox.calls) == 7  # each of the 7 searches once: a refusal isn't sent again
    found = {"webResults": [{"title": "t", "url": "https://example.com/a", "content": "c"}]}
    toolbox.answer = lambda args: {"result": {"content": [{"type": "text", "text": json.dumps(found)}]}}
    assert cmd_run(a) == 0  # after the fix, the same command searches again and sends every record
    assert len(toolbox.calls) == 14 and sorted(fake.submitted[0]) == sorted(IDS)
