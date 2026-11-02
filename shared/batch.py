"""Azure OpenAI Batch: validate, upload, submit, wait, collect per-line usage, and delete the job's files."""
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
FINAL_STATES = ("completed", "failed", "expired", "cancelled")
# 14 days, the shortest expiry the Batch API accepts for input and output files.
EXPIRES_AFTER = {"seconds": int(_B["file_expiry_seconds"]), "anchor": "created_at"}


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


def submit(client, path: str | os.PathLike, lines: list[dict], label: str) -> dict:
    """Upload the batch input file at `path` and create the job. Returns ids for the state file.

    Files are uploaded with a 14-day expiry, and the job's output files get one too, as a backstop in case cleanup
    never runs. An explicit expiry also raises the per-resource input-file limit from 500 to 10,000 (see the Batch docs).
    """
    problems = check_lines(lines)
    if problems:
        raise SystemExit("Refusing to submit: " + "; ".join(problems))
    size = pathlib.Path(path).stat().st_size
    if size > MAX_BYTES:
        raise SystemExit(f"{size:,} bytes exceeds the 200 MB input file limit; split the job")
    with open(path, "rb") as f:
        up = client.files.create(file=f, purpose="batch", extra_body={"expires_after": EXPIRES_AFTER})
    try:
        status = None
        for _ in range(120):
            status = client.files.retrieve(up.id).status
            if status in ("processed", "error"):
                break
            time.sleep(5)
        if status != "processed":
            raise SystemExit(f"Input file {label} ended in status '{status}'")
        b = client.batches.create(
            input_file_id=up.id, endpoint=lines[0]["url"], completion_window="24h", extra_body={"output_expires_after": EXPIRES_AFTER}
        )
    except BaseException:
        # The uploaded file holds the job's prompts (and, for two-stage, Web IQ content): don't leave it behind.
        delete_files(client, [up.id])
        raise
    print(f"Submitted batch {b.id}: {len(lines)} lines, deployment {lines[0]['body']['model']}, status {b.status}")
    return {"input_file_id": up.id, "batch_id": b.id, "submitted_at": ts(int(time.time()))}


def wait(client, batch_id: str, poll_seconds: int = 60, stall_minutes: int = 120, cancel_on_stall: bool = False):
    """Poll until the job reaches a final state. Warns (and optionally cancels) when no line completes or fails for a while."""
    last, changed, warned = None, time.time(), False
    while True:
        b = client.batches.retrieve(batch_id)
        rc = b.request_counts
        progress = (rc.completed + rc.failed) if rc else 0
        print(f"  {ts(int(time.time()))} {b.id}: {b.status} completed={rc.completed if rc else '?'} failed={rc.failed if rc else '?'} total={rc.total if rc else '?'}")
        if b.status in FINAL_STATES:
            return b
        if progress != last:
            last, changed, warned = progress, time.time(), False
        elif b.status == "in_progress" and time.time() - changed > stall_minutes * 60 and not warned:
            warned = True
            print(f"  WARNING: no line completed or failed for {stall_minutes} minutes. Small jobs often report progress only at the end.")
            if cancel_on_stall:
                print("  Cancelling (--cancel-on-stall). Lines that completed are kept; re-run the same command to resubmit only the rest.")
                client.batches.cancel(b.id)
        time.sleep(poll_seconds)


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


def collect(client, batch, out_path: str | os.PathLike) -> list[dict]:
    """Download output and error files; write one metrics row per custom_id."""
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
                m = {"status": "error", "error": json.dumps(body.get("error") or o.get("error"))[:500]}
            m["custom_id"] = o.get("custom_id")
            rows.append(m)
    if batch.error_file_id:
        for line in client.files.content(batch.error_file_id).text.splitlines():
            if line.strip():
                o = json.loads(line)
                err = ((o.get("response") or {}).get("body") or {}).get("error") or o.get("error")
                rows.append({"custom_id": o.get("custom_id"), "status": "error", "error": json.dumps(err)[:500]})
    rows.sort(key=lambda r: str(r.get("custom_id")))
    write_jsonl(out_path, rows)
    return rows


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
