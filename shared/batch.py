"""Azure OpenAI Batch: validate, split, upload, create (waiting while the quota is full), wait, collect per-line usage,
decide what each line needs next, and delete the job's files."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import time

from settings import SETTINGS
from util import write_jsonl

_B, _A = SETTINGS["batch"], SETTINGS["azure_openai"]
MAX_BYTES = int(_B["max_file_bytes"])
MAX_LINES = int(_B["max_lines_per_file"])
PART_BYTES = int(_B["part_bytes"])  # where a job too big for one file is cut, with room below MAX_BYTES
FINAL_STATES = ("completed", "failed", "expired", "cancelled")
# 14 days, the shortest expiry the Batch API accepts for input and output files.
EXPIRES_AFTER = {"seconds": int(_B["file_expiry_seconds"]), "anchor": "created_at"}
QUOTA_FULL = "token_limit_exceeded"  # the deployment's enqueued-token quota is full: nothing was created or billed
NOT_PROCESSED = ("batch_expired", "batch_cancelled")  # error codes of lines a job ended without processing


class QuotaWaitOver(Exception):
    """The enqueued-token quota stayed full for quota_wait_minutes (or until the run's deadline)."""


class WaitOver(Exception):
    """The run's --max-wait-minutes ran out before every job ended. The jobs keep running."""


def _sleep(seconds: float) -> None:
    """Every wait in this module. Tests replace it."""
    time.sleep(seconds)


def make_client(base_url: str, subscription: str | None = None, timeout: float = 900, max_retries: int | None = None):
    """OpenAI client for an Azure OpenAI v1 endpoint (https://<resource>.openai.azure.com/openai/v1/).

    Uses Microsoft Entra ID (Azure CLI pinned to --subscription, else DefaultAzureCredential). Set
    AZURE_OPENAI_API_KEY to use a key instead. max_retries defaults to [azure_openai] sdk_max_retries; Flex passes 0
    because it counts its attempts itself.
    """
    from openai import OpenAI

    retries = int(_A["sdk_max_retries"]) if max_retries is None else max_retries
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    if key:
        return OpenAI(base_url=base_url, api_key=key, timeout=timeout, max_retries=retries)
    from azure.identity import AzureCliCredential, DefaultAzureCredential, get_bearer_token_provider

    cred = AzureCliCredential(subscription=subscription) if subscription else DefaultAzureCredential()
    provider = get_bearer_token_provider(cred, _A["token_scope"])
    return OpenAI(base_url=base_url, api_key=provider, timeout=timeout, max_retries=retries)


def ts(epoch) -> str | None:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds") if epoch else None


def check_lines(lines: list[dict]) -> list[str]:
    """Problems that would make the Batch API reject the file."""
    problems = []
    models = {r["body"].get("model") for r in lines}
    urls = {r.get("url") for r in lines}
    ids = [r.get("custom_id") for r in lines]
    if len(models) != 1:
        problems.append(f"every line must target one deployment, found {sorted(map(str, models))}")
    if len(urls) != 1:
        problems.append(f"every line must use one endpoint, found {sorted(map(str, urls))}")
    if len(ids) != len(set(ids)):
        problems.append("custom_id values are not unique")
    if len(lines) > MAX_LINES:
        problems.append(f"{len(lines):,} lines exceeds {MAX_LINES:,} per file")
    return problems


def line_bytes(line: dict) -> int:
    """A line's size in the uploaded file: its JSON and newline, as util.write_jsonl writes it."""
    return len(json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1


def split_parts(lines: list[dict]) -> list[list[dict]]:
    """The lines as one batch input file if they fit the limits (100,000 lines and 200 MB), else as several parts cut at
    100,000 lines and part_bytes (180 MB), in input order. Each part is submitted as its own batch job."""
    sizes = [line_bytes(line) for line in lines]
    ids = [line.get("custom_id") for line in lines]
    if len(ids) != len(set(ids)):
        raise SystemExit("Refusing to submit: custom_id values are not unique")
    if len(lines) <= MAX_LINES and sum(sizes) <= MAX_BYTES:
        return [lines] if lines else []
    parts: list[list[dict]] = []
    part: list[dict] = []
    size = 0
    for line, n in zip(lines, sizes):
        if n > PART_BYTES:
            raise SystemExit(f"Line {line.get('custom_id')} is {n:,} bytes, more than a batch input file part ({PART_BYTES:,}).")
        if part and (len(part) >= MAX_LINES or size + n > PART_BYTES):
            parts.append(part)
            part, size = [], 0
        part.append(line)
        size += n
    return parts + [part] if part else parts


def upload(client, path: str | os.PathLike, lines: list[dict], label: str) -> str:
    """Check the lines, upload the batch input file at `path` and wait until the service has processed it. Returns
    the file id.

    Files are uploaded with a 14-day expiry, and the job's output files get one too, as a backstop in case cleanup
    never runs. An explicit expiry also raises the per-resource input-file limit from 500 to 10,000 (see the Batch docs).
    """
    problems = check_lines(lines)
    if problems:
        raise SystemExit("Refusing to submit: " + "; ".join(problems))
    size = pathlib.Path(path).stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{size:,} bytes exceeds the 200 MB input file limit")
    with open(path, "rb") as f:
        up = client.files.create(file=f, purpose="batch", extra_body={"expires_after": EXPIRES_AFTER})
    try:
        status = None
        for _ in range(120):
            status = client.files.retrieve(up.id).status
            if status in ("processed", "error"):
                break
            _sleep(5)
        if status != "processed":
            raise SystemExit(f"Input file {label} ended in status '{status}'")
    except BaseException:
        # The uploaded file holds the job's prompts (and, for two-stage, Web IQ content): don't leave it behind.
        delete_files(client, [up.id])
        raise
    return up.id


def quota_full(err: BaseException) -> bool:
    """HTTP 400 token_limit_exceeded: the deployment's enqueued-token quota is full, and nothing was created or billed."""
    return QUOTA_FULL in f"{getattr(err, 'code', None)} {err}"


def failed_for_quota(batch) -> bool:
    """A job that was created, then failed because the enqueued-token quota was full (token_limit_exceeded)."""
    errors = batch.errors.data if batch.errors and batch.errors.data else []
    return batch.status == "failed" and any(QUOTA_FULL in f"{e.code} {e.message}" for e in errors)


def refused_for_now(err: BaseException) -> bool:
    """A create the service refused, creating nothing, for a reason that passes: the enqueued-token quota is full
    (HTTP 400 token_limit_exceeded) or too many requests (HTTP 429)."""
    import openai

    return quota_full(err) or isinstance(err, openai.RateLimitError)


def ambiguous(err: BaseException) -> bool:
    """A create whose outcome is unknown, so the job may have been created: no answer (a timeout or a dropped
    connection), HTTP 5xx, or HTTP 408 or 409."""
    import openai

    return isinstance(err, (openai.APIConnectionError, openai.InternalServerError)) or (
        isinstance(err, openai.APIStatusError) and err.status_code in (408, 409))


def passing(err: BaseException) -> bool:
    """An error that may pass on its own: no answer (a timeout or a dropped connection), or HTTP 408, 409, 429 or 5xx.
    Anything else, such as HTTP 401, 403 or 404 or a failed Microsoft Entra ID sign-in, needs a person."""
    import openai

    return ambiguous(err) or isinstance(err, openai.RateLimitError)


class CreateUncertain(Exception):
    """A create may have gone through, and the batch jobs couldn't be listed to find out, for now (an error that may
    pass): the next run lists them again."""


class ListRefused(Exception):
    """A create may have gone through, and listing the batch jobs to find out was refused: the sign-in or access (HTTP
    401, 403 or 404, or no Microsoft Entra ID token), or another error that won't pass on its own."""


class QuotaBudget:
    """How long one run waits to create batch jobs while the service refuses for now (a full enqueued-token quota,
    HTTP 429) or creating a job gets no answer: 5 s at first, doubling up to 600 s, for up to quota_wait_minutes in
    all, and never past the run's deadline (--max-wait-minutes). Then wait() raises QuotaWaitOver. Each run starts
    with the whole budget, so a re-run always tries again."""

    def __init__(self, deadline: float | None = None):
        self.left = float(_B["quota_wait_minutes"]) * 60
        self.delay, self.deadline, self.waited = float(_B["quota_retry_first_seconds"]), deadline, 0.0

    def wait(self, why: str) -> None:
        left = self.left if self.deadline is None else min(self.left, self.deadline - time.time())
        if left <= 0:
            raise QuotaWaitOver(f"{why}, and this run has waited {self.waited / 60:.0f} minutes for it")
        pause = min(self.delay, left)
        print(f"  {ts(int(time.time()))} {why}: trying again in {pause:.0f} s")
        _sleep(pause)
        self.left -= pause
        self.waited += pause
        self.delay = min(float(_B["quota_retry_max_seconds"]), self.delay * 2)


def create(client, file_id: str, endpoint: str):
    """Send one create for the batch job of an uploaded file, with the SDK's own retries off. The SDK would send it
    again after a timeout, a dropped connection or HTTP 408, 409, 429 or 5xx, with no idempotency key, so a create that
    went through but whose answer was lost would make a second job, billed twice. pipeline.create_job makes every
    retry instead, and looks for a job already made from the file before each one."""
    return client.with_options(max_retries=0).batches.create(
        input_file_id=file_id, endpoint=endpoint, completion_window="24h", extra_body={"output_expires_after": EXPIRES_AFTER})


def find_job(client, file_id: str, skip: list[str] | tuple = ()):
    """The batch job created from this uploaded file, if there is one, other than the jobs in `skip`. A create can go
    through without its answer reaching the run (the process was stopped, or the call timed out), so this is checked
    before creating a job again: two jobs from one file would bill twice. An error listing the jobs is raised: it
    means "no answer yet", not "no job"."""
    for b in client.batches.list(limit=100):  # 100 per page, the most the API returns; the pages are read in turn
        if b.input_file_id == file_id and b.id not in skip:
            return b
    return None


def wait_all(client, batch_ids: list[str], poll_seconds: float = 60, stall_minutes: float = 120, cancel_on_stall: bool = False,
             deadline: float | None = None, on_cancel=None) -> dict:
    """Poll the jobs until every one reaches a final state, with no time limit unless `deadline` is given (then
    WaitOver is raised when it passes, and the jobs keep running). Returns {batch id: the final batch}.

    Warns when none of a job's lines completes or fails for stall_minutes; with cancel_on_stall it cancels that job,
    calling on_cancel(batch id) first so the caller can record that this run cancelled it. A status that can't be read
    (a dropped connection, or HTTP 429 or 5xx after the SDK's own retries) is read again at the next poll.
    """
    import openai

    transient = (openai.APIConnectionError, openai.RateLimitError, openai.InternalServerError)
    track = {bid: {"last": None, "changed": time.time(), "warned": False} for bid in batch_ids}
    ended: dict = {}
    while True:
        for bid in batch_ids:
            if bid in ended:
                continue
            try:
                b = client.batches.retrieve(bid)
            except transient as ex:
                print(f"  {ts(int(time.time()))} {bid}: couldn't read its status ({type(ex).__name__}); trying again at the next poll")
                continue
            rc, t = b.request_counts, track[bid]
            progress = (rc.completed + rc.failed) if rc else 0
            print(f"  {ts(int(time.time()))} {b.id}: {b.status} completed={rc.completed if rc else '?'} failed={rc.failed if rc else '?'} total={rc.total if rc else '?'}")
            if b.status in FINAL_STATES:
                ended[bid] = b
            elif progress != t["last"]:
                t.update(last=progress, changed=time.time(), warned=False)
            elif b.status == "in_progress" and time.time() - t["changed"] > stall_minutes * 60 and not t["warned"]:
                t["warned"] = True
                print(f"  WARNING: no line of {bid} completed or failed for {stall_minutes} minutes. Small jobs often report progress only at the end.")
                if cancel_on_stall:
                    print("  Cancelling it (--cancel-on-stall). Lines that completed are kept, and the others are sent again in a new job.")
                    if on_cancel:
                        on_cancel(bid)
                    client.batches.cancel(bid)
        if len(ended) == len(batch_ids):
            return ended
        if deadline is not None and time.time() >= deadline:
            raise WaitOver(f"{len(batch_ids) - len(ended)} of {len(batch_ids)} batch jobs still running")
        _sleep(poll_seconds)


def response_metrics(body: dict) -> dict:
    """Cost-relevant numbers from one Responses API response body (a batch output line's, or a Flex response's).

    cache_write_tokens (prompt-cache writes, GPT-5.6 and later) is part of input_tokens, like cached_tokens.
    service_tier is the tier the response says it was processed on (Flex responses report "flex").
    """
    usage = body.get("usage") or {}
    items = body.get("output") or []
    calls = [i for i in items if i.get("type") == "web_search_call"]
    actions: dict[str, int] = {}
    for c in calls:
        t = (c.get("action") or {}).get("type", "unknown")
        actions[t] = actions.get(t, 0) + 1
    text = "".join(
        p.get("text", "") for i in items if i.get("type") == "message" for p in i.get("content") or [] if p.get("type") == "output_text"
    )
    try:
        parsed, valid = json.loads(text), True
    except (json.JSONDecodeError, TypeError):
        parsed, valid = None, False
    tool_usage = body.get("tool_usage") or {}
    return {
        "status": body.get("status"),
        "incomplete_reason": (body.get("incomplete_details") or {}).get("reason"),
        "service_tier": body.get("service_tier"),
        "input_tokens": usage.get("input_tokens", 0),
        "cached_tokens": (usage.get("input_tokens_details") or {}).get("cached_tokens", 0),
        "cache_write_tokens": (usage.get("input_tokens_details") or {}).get("cache_write_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("reasoning_tokens", 0),
        # Billing unit for the built-in web search tool; web_search_call items don't reliably match it.
        "web_search_requests": ((tool_usage.get("web_search") or {}) if isinstance(tool_usage, dict) else {}).get("num_requests", 0),
        "web_search_calls": len(calls),
        "web_search_actions": actions,
        "json_valid": valid,
        "output": parsed if valid else None,
        "output_text": None if valid else text[:2000],
    }


def _error_row(status, err) -> dict:
    """A metrics row for a line that failed, with its HTTP status and error code when the service gives them."""
    row: dict = {"status": "error", "error": json.dumps(err)[:500]}
    if isinstance(status, int):
        row["http_status"] = status
    if isinstance(err, dict) and err.get("code"):
        row["error_code"] = str(err["code"])
    return row


def collect(client, batch, out_path: str | os.PathLike | None = None) -> list[dict]:
    """Download output and error files; return (and, with out_path, write) one metrics row per custom_id."""
    rows = []
    if batch.output_file_id:
        for line in client.files.content(batch.output_file_id).text.splitlines():
            if not line.strip():
                continue
            o = json.loads(line)
            resp = o.get("response") or {}
            body = resp.get("body") or {}
            if resp.get("status_code") == 200 and body:
                m = response_metrics(body)
            else:
                m = _error_row(resp.get("status_code"), body.get("error") or o.get("error"))
            m["custom_id"] = o.get("custom_id")
            rows.append(m)
    if batch.error_file_id:
        for line in client.files.content(batch.error_file_id).text.splitlines():
            if line.strip():
                o = json.loads(line)
                resp = o.get("response") or {}
                rows.append({"custom_id": o.get("custom_id"), **_error_row(resp.get("status_code"), (resp.get("body") or {}).get("error") or o.get("error"))})
    rows.sort(key=lambda r: str(r.get("custom_id")))
    if out_path:
        write_jsonl(out_path, rows)
    return rows


def retryable(row: dict) -> bool:
    """A line error that may pass if the line is sent again: HTTP 408, 429 or 5xx, or a full enqueued-token quota."""
    s = row.get("http_status")
    return s in (408, 429) or (isinstance(s, int) and 500 <= s <= 599) or row.get("error_code") == QUOTA_FULL


def sort_lines(custom_ids: list[str], rows: list[dict], batch, cancelled_by_run: bool) -> dict[str, list[dict]]:
    """What each line of a job that ended needs next, as metrics rows in four lists:

    settled  never sent again: answered (an incomplete response too: sending it again repeats or pays twice), or failed
             with an error retrying can't fix (HTTP 400-class other than 408 and 429);
    retry    sent again: failed with HTTP 408, 429 or 5xx, or never processed because the job expired or was cancelled
             by this run (--cancel-on-stall);
    held     not sent again automatically: someone cancelled the job outside this run;
    blocked  the job failed validation: a person must fix the input before it's sent again.

    A job that failed because the enqueued-token quota was full isn't sorted: the caller creates it again
    (failed_for_quota).
    """
    by_id = {r.get("custom_id"): r for r in rows}
    errors = [f"{e.code}: {e.message}" for e in (batch.errors.data if batch.errors and batch.errors.data else [])]
    out: dict[str, list[dict]] = {"settled": [], "retry": [], "held": [], "blocked": []}
    for cid in custom_ids:
        r = by_id.get(cid)
        if r and r.get("status") != "error":
            out["settled"].append(r)
            continue
        if r and r.get("error_code") not in NOT_PROCESSED and not retryable(r):
            out["settled"].append(r)
            continue
        why = r or {"custom_id": cid, "status": "error", "error": f"not processed: the batch job ended '{batch.status}'"
                    + (f" ({'; '.join(errors)[:300]})" if errors else "")}
        if batch.status == "cancelled" and not cancelled_by_run:
            out["held"].append(why)
        elif batch.status == "failed" and not retryable(why):
            out["blocked"].append(why)
        else:
            out["retry"].append(why)
    return out


def delete_files(client, file_ids: list[str | None]) -> list[str]:
    deleted = []
    for fid in [f for f in dict.fromkeys(file_ids) if f]:
        try:
            client.files.delete(fid)
            deleted.append(fid)
        except Exception as ex:
            print(f"  could not delete {fid}: {ex}")
    return deleted


def batch_timing(b) -> dict:
    started = b.in_progress_at or b.created_at
    return {
        "status": b.status,
        "created_at": ts(b.created_at),
        "completed_at": ts(b.completed_at),
        "minutes_end_to_end": round((b.completed_at - b.created_at) / 60, 1) if b.completed_at else None,
        "minutes_in_progress": round((b.completed_at - started) / 60, 1) if b.completed_at and started else None,
        "request_counts": {"total": b.request_counts.total, "completed": b.request_counts.completed, "failed": b.request_counts.failed}
        if b.request_counts
        else None,
        "errors": [f"{e.code}: {e.message}" for e in (b.errors.data if b.errors and b.errors.data else [])],
    }
