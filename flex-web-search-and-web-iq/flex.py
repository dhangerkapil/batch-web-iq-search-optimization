"""Flex processing: run batch-format lines as synchronous Responses API requests with service_tier "flex".

Flex processing (preview) is a per-request setting on a Global Standard deployment of a Flex-capable model, for example
gpt-6-sol: about half the Standard token price, no SLA, and HTTP 429 when capacity is short. A deployment whose model
doesn't support Flex answers HTTP 400 (https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing).
run_lines() sends the lines the Batch path would upload, one request per line and a few at a time, and returns one
metrics row per line in the shape the Batch path writes to metrics.jsonl.

Every request is recorded in an append-only attempt ledger, <workdir>/flex-attempts.jsonl, as two rows: a start row,
written and flushed before the request is sent, and an outcome row with the HTTP status, the error, the tier the
response reports, its usage, how the attempt is billed and what happens to the line next. A re-run reads the ledger:
  - a completed line is never sent again;
  - a line whose request may have been processed and billed without an answer reaching us is held, and sent again, in
    a new round, only with retry_ambiguous: a client timeout or a dropped connection (Flex requests can queue, so the
    service may still process, and bill, a request the client stopped waiting for), or a request that was in flight
    when a run stopped (a start row with no outcome). Resending it automatically could pay twice for one line;
  - a line that used up its attempts on errors that may pass (HTTP 408, 429, 5xx) is held, and sent again, in a new
    round, only with retry_failed;
  - a line that failed with an error retrying can't fix (another 4xx, or a response on another tier) stays failed;
  - every other line is sent: not sent yet, or a round cut short (Ctrl+C, a stopped job, or a long Retry-After).
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import datetime as dt
import email.utils
import hashlib
import json
import os
import pathlib
import random
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

import batch as batchlib
from settings import SETTINGS
from util import now

_F = SETTINGS["flex"]
LEDGER = "flex-attempts.jsonl"
RETRY_STATUS = (408, 429, 500, 502, 503, 504)
# Errors every request of the job would get, like HTTP 400 "Flex tier is not supported for this deployment": they stop
# the job instead of failing each line in turn.
STOP_STATUS = (401, 403, 404)
USAGE = ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "web_search_requests")
PROGRESS_SECONDS = 30

# How an attempt is billed ("billing" in the ledger).
MEASURED = "measured"  # a Flex response: priced from its usage
NOT_BILLED = "not_billed"  # HTTP 429: refused, with no usage
REJECTED = "rejected_no_usage"  # another 4xx (or no token to send it with): refused, with no usage
UNKNOWN = "unknown"  # HTTP 408 or 5xx (retried), a timeout or dropped connection (held), or no outcome recorded: counted, not priced
OTHER_TIER = "other_tier"  # a response processed on a tier other than flex: counted, not priced at Flex rates

# What an attempt means for its line ("result" in the ledger).
COMPLETED = "completed"  # the line has its response (the response's own status is in its metrics): never sent again
RETRYING = "retrying"  # another attempt follows; if this run stopped first, a re-run continues the round
USED_UP = "used_up"  # the round's attempts are used up: sent again, in a new round, only with retry_failed
FAILED = "failed"  # an error retrying can't fix: never sent again
# No answer was recorded, but the request may have been processed and billed: a client timeout, a dropped connection, an
# unreadable response or an unexpected error. The line is held, and sent again, in a new round, only with retry_ambiguous.
OUTCOME_UNKNOWN = "unknown"


def flex_body(line: dict, deployment: str) -> dict:
    """The request sent for a line: its body, on the Flex deployment, with service_tier "flex"."""
    body = copy.deepcopy(line["body"])
    body["model"] = deployment
    body["service_tier"] = "flex"
    return body


def request_sha256(body: dict) -> str:
    """Identifies the exact request in the ledger (canonical JSON with sorted keys)."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def check_lines(lines: list[dict]) -> list[str]:
    problems = []
    ids = [line.get("custom_id") for line in lines]
    if len(ids) != len(set(ids)):
        problems.append("custom_id values are not unique")
    urls = {line.get("url") for line in lines} - {"/v1/responses"}
    if urls:
        problems.append(f"Flex runs Responses API lines (url /v1/responses), found {sorted(map(str, urls))}")
    return problems


def make_client(base_url: str, subscription: str | None = None):
    """OpenAI client for the Azure OpenAI v1 endpoint, with the SDK's own retries off: run_lines counts every attempt."""
    return batchlib.make_client(base_url, subscription, timeout=float(_F["client_timeout_seconds"]), max_retries=0)


def retry_after(err: BaseException) -> float | None:
    """Seconds an HTTP error asked us to wait: retry-after-ms, or retry-after in seconds or as an HTTP date."""
    headers = getattr(getattr(err, "response", None), "headers", None)
    if not headers:
        return None
    try:
        if headers.get("retry-after-ms"):
            return max(0.0, float(headers.get("retry-after-ms")) / 1000)
        value = headers.get("retry-after")
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            when = email.utils.parsedate_to_datetime(value)
            return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def backoff(attempt: int) -> float:
    """Our own wait after failed attempt number `attempt`: exponential, with jitter, capped at max_delay_seconds."""
    return min(float(_F["max_delay_seconds"]), float(_F["backoff_base_seconds"]) * 2 ** (attempt - 1) * random.uniform(0.5, 1.5))


def _sleep(seconds: float, stop: threading.Event) -> None:
    """The wait between attempts; it ends early when the run is stopping. Tests replace it."""
    stop.wait(seconds)


def _http_status(err: BaseException) -> int | None:
    status = getattr(err, "status_code", None)
    return status if isinstance(status, int) else None


def _kind(err: BaseException) -> str | None:
    """timeout or connection (no answer, but the request may have been processed: the line is held), auth (no token, so
    nothing was sent: the job stops), or None for any other error."""
    try:
        import openai

        if isinstance(err, openai.APITimeoutError):
            return "timeout"
        if isinstance(err, openai.APIConnectionError):
            return "connection"
    except ImportError:  # pragma: no cover - openai is a dependency
        pass
    if isinstance(err, TimeoutError):
        return "timeout"
    if isinstance(err, ConnectionError):
        return "connection"
    try:
        from azure.core.exceptions import ClientAuthenticationError

        if isinstance(err, ClientAuthenticationError):
            return "auth"
    except ImportError:  # pragma: no cover - azure-identity brings azure-core
        pass
    return None


def _flex_unsupported(status: int | None, text: str, deployment: str = "") -> bool:
    """HTTP 400 "Flex tier is not supported for this deployment": every request to the deployment would get it. The
    deployment's own name is ignored, so a name such as "gpt-flex" can't make another 400 look like this one."""
    low = text.lower().replace(deployment.lower(), " ") if deployment else text.lower()
    return status == 400 and "flex" in low and "support" in low


def read_ledger(workdir: str | os.PathLike) -> list[dict]:
    """Every ledger row, oldest first. A last row cut short when a run was killed is skipped."""
    path = pathlib.Path(workdir) / LEDGER
    rows: list[dict] = []
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return rows


def _key(row: dict) -> tuple:
    return row.get("custom_id"), row.get("round"), row.get("attempt")


def line_state(rows: list[dict], max_attempts: int, retry_failed: bool = False, retry_ambiguous: bool = False) -> dict:
    """What a run does with one line, from its ledger rows (oldest first).

    Returns {"state", "send", "round", "used", "row"}. state is completed, pending (not sent yet, or a round cut
    short), held (a request whose outcome is unknown), used_up or failed. send says whether this run sends the line, in
    round `round`, with `used` of its attempts already made. row is the outcome row that decided, if any.
    """
    outcomes = [r for r in rows if r.get("event") == "outcome"]
    done = [r for r in outcomes if r.get("result") == COMPLETED]
    if done:
        return {"state": "completed", "send": False, "round": done[-1].get("round"), "used": 0, "row": done[-1]}
    if not rows:
        return {"state": "pending", "send": True, "round": 1, "used": 0, "row": None}
    rnd = max(int(r.get("round") or 1) for r in rows)
    starts = [r for r in rows if r.get("event") == "start" and int(r.get("round") or 1) == rnd]
    answered = {r.get("attempt"): r for r in outcomes if int(r.get("round") or 1) == rnd}
    last = answered[max(answered)] if answered else None
    result = (last or {}).get("result")
    if any(s.get("attempt") not in answered for s in starts) or result == OUTCOME_UNKNOWN:
        state = "held"
    elif result == FAILED:
        state = "failed"
    elif result == USED_UP or len(starts) >= max_attempts:
        state = "used_up"
    else:
        return {"state": "pending", "send": True, "round": rnd, "used": len(starts), "row": last}
    again = (state == "held" and retry_ambiguous) or (state == "used_up" and retry_failed)
    return {"state": state, "send": again, "round": rnd + 1 if again else rnd, "used": 0, "row": last}


def _states(ledger: list[dict], ids, max_attempts: int, retry_failed: bool = False, retry_ambiguous: bool = False) -> dict[str, dict]:
    by_line: dict[str, list[dict]] = {}
    for r in ledger:
        by_line.setdefault(r.get("custom_id"), []).append(r)
    return {c: line_state(by_line.get(c, []), max_attempts, retry_failed, retry_ambiguous) for c in ids}


def metric_row(custom_id: str, state: dict) -> dict:
    """A line's metrics.jsonl row: the Flex response's metrics, or an error row like the Batch path's."""
    row = state.get("row") or {}
    if state["state"] == "completed":
        m = dict(row.get("metrics") or {})
        m["custom_id"] = custom_id
        return m
    if state["state"] == "held":
        why = (f"its outcome and charge are unknown ({row.get('error')})" if row.get("result") == OUTCOME_UNKNOWN
               else "a request was in flight when a run stopped, so its outcome and charge are unknown")
        error = f"{why}; re-run with --retry-ambiguous to send it again"
    elif state["state"] == "used_up":
        error = f"used up its attempts, the last with {row.get('error')}; re-run with --retry-failed to send it again"
    elif state["state"] == "failed":
        error = str(row.get("error"))
    elif row:
        error = f"not finished ({row.get('note') or row.get('error')}); re-run the same command to continue"
    else:
        error = "not sent; re-run the same command to send it"
    return {"custom_id": custom_id, "status": "error", "error": error[:500]}


def summarize(ledger: list[dict], states: dict[str, dict], *, sent: int, concurrency: int, max_attempts: int) -> dict:
    """Lines and attempts for run.json, over the whole ledger (every run of the job)."""
    starts = [r for r in ledger if r.get("event") == "start"]
    outcomes = [r for r in ledger if r.get("event") == "outcome"]
    answered = {_key(r) for r in outcomes}
    no_outcome = sum(1 for r in starts if _key(r) not in answered)
    by_status = Counter(str(r["http_status"]) if r.get("http_status") is not None else str(r.get("error_kind")) for r in outcomes)
    by_billing = Counter(str(r.get("billing")) for r in outcomes)
    if no_outcome:
        by_status["no_outcome"] += no_outcome
        by_billing[UNKNOWN] += no_outcome
    lines = Counter(s["state"] for s in states.values())
    seconds = [r.get("seconds") or 0 for r in outcomes if r.get("result") == COMPLETED]
    return {
        "concurrency": concurrency,
        "max_attempts": max_attempts,
        "client_timeout_seconds": float(_F["client_timeout_seconds"]),
        "lines": {"total": len(states), "completed": lines["completed"], "failed": lines["failed"], "used_up": lines["used_up"],
                  "held_in_flight": lines["held"], "not_finished": lines["pending"]},
        "attempts": {
            "requests": len(starts),
            "requests_this_run": sent,
            "by_http_status": dict(sorted(by_status.items())),
            "by_billing": dict(sorted(by_billing.items())),
            "held_in_flight": lines["held"],
            "not_billed_429": by_billing[NOT_BILLED],
            "unknown_charges": by_billing[UNKNOWN],
            "other_tier": by_billing[OTHER_TIER],
            "mean_seconds_per_completed_request": round(sum(seconds) / len(seconds), 1) if seconds else None,
        },
        "billing_note": "measured attempts are priced (actual); not_billed (HTTP 429) and rejected_no_usage (other 4xx) "
                        "returned no usage; unknown (HTTP 408 or 5xx, client timeouts, dropped connections, no outcome) and "
                        "other_tier are counted, not priced",
    }


class _Ledger:
    """The append-only attempt ledger: every row is flushed to disk before write() returns."""

    def __init__(self, path: pathlib.Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        torn = False
        if path.exists() and path.stat().st_size:
            with open(path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                torn = f.read(1) != b"\n"
        self.f = open(path, "a", encoding="utf-8", newline="\n")
        if torn:
            self.f.write("\n")  # a row cut short when a run was killed: the next row starts on a line of its own
        self.lock = threading.Lock()

    def write(self, row: dict) -> None:
        with self.lock:
            self.f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.f.flush()
            os.fsync(self.f.fileno())

    def close(self) -> None:
        self.f.close()


class _Sender:
    """Sends lines and records every attempt; shared by the worker threads of one run."""

    def __init__(self, client, bodies: dict, hashes: dict, ledger: _Ledger, deployment: str, max_attempts: int):
        self.client, self.bodies, self.hashes, self.ledger = client, bodies, hashes, ledger
        self.deployment, self.max_attempts = deployment, max_attempts
        self.max_server_wait = float(_F["max_server_wait_seconds"])
        self.stop = threading.Event()  # set by Ctrl+C or a job-stopping error: no attempt starts after it
        self.stopped: str | None = None
        self.lock = threading.Lock()
        self.sent = self.in_flight = self.completed = self.throttled = 0

    def say(self, text: str) -> None:
        with self.lock:
            print(text, flush=True)

    def progress(self, total: int) -> str:
        return (f"  {now()} Flex: {self.completed} of {total} lines completed this run, {self.in_flight} requests in flight, "
                f"{self.sent} sent, {self.throttled} HTTP 429s")

    def _row(self, event: str, cid: str, rnd: int, attempt: int, **fields) -> dict:
        return {"event": event, "custom_id": cid, "round": rnd, "attempt": attempt, "request_sha256": self.hashes[cid], "at": now(), **fields}

    def _outcome(self, cid: str, rnd: int, attempt: int, *, http_status: int | None, billing: str, result: str, seconds: float,
                 error: str | None = None, service_tier: str | None = None, usage: dict | None = None, **extra) -> None:
        self.ledger.write(self._row("outcome", cid, rnd, attempt, http_status=http_status, error=error, service_tier=service_tier,
                                    usage=usage, billing=billing, result=result, seconds=seconds, **extra))

    def send_line(self, cid: str, rnd: int, used: int) -> None:
        """Attempts for one line, up to max_attempts in its round (`used` were made by earlier runs)."""
        attempt = used
        while attempt < self.max_attempts and not self.stop.is_set():
            attempt += 1
            # Durable before the request leaves: if the process dies while it's in flight, the ledger still shows it.
            self.ledger.write(self._row("start", cid, rnd, attempt))
            with self.lock:
                self.sent += 1
                self.in_flight += 1
            t0 = time.monotonic()
            resp = err = None
            try:
                resp = self.client.responses.create(**self.bodies[cid])
            except Exception as ex:  # recorded below; _failed decides what happens next
                err = ex
            seconds = round(time.monotonic() - t0, 1)
            with self.lock:
                self.in_flight -= 1
            if err is None:
                self._answered(cid, rnd, attempt, resp, seconds)
                return
            wait = self._failed(cid, rnd, attempt, err, seconds)
            if wait is None:
                return
            _sleep(wait, self.stop)

    def _answered(self, cid: str, rnd: int, attempt: int, resp, seconds: float) -> None:
        """Record a response. It was processed, and may be billed, so it's recorded even if it can't be read."""
        try:
            m = batchlib.response_metrics(resp if isinstance(resp, dict) else resp.model_dump())
        except Exception as ex:
            error = f"the response couldn't be read: {type(ex).__name__}: {ex}"[:500]
            self._outcome(cid, rnd, attempt, http_status=200, billing=UNKNOWN, result=OUTCOME_UNKNOWN, seconds=seconds, error=error)
            self.say(f"  {cid}: {error} (held; --retry-ambiguous sends it again)")
            return
        usage = {k: m.get(k) or 0 for k in USAGE}
        tier = m.get("service_tier")
        if tier == "flex":
            self._outcome(cid, rnd, attempt, http_status=200, billing=MEASURED, result=COMPLETED, seconds=seconds,
                          service_tier=tier, usage=usage, metrics=m)
            with self.lock:
                self.completed += 1
            return
        error = f"processed on tier {tier}, not flex"
        self._outcome(cid, rnd, attempt, http_status=200, billing=OTHER_TIER, result=FAILED, seconds=seconds, error=error,
                      service_tier=tier, usage=usage)
        self.say(f"  {cid}: {error}; not priced at Flex rates and not sent again")

    def _failed(self, cid: str, rnd: int, attempt: int, err: Exception, seconds: float) -> float | None:
        """Record a failed attempt. Returns the wait before the next attempt, or None when this run sends no more for the line."""
        status = _http_status(err)
        kind = None if status is not None else _kind(err)
        text = f"{type(err).__name__}: {err}"[:500]
        server_wait = retry_after(err)
        extra: dict = {"error_kind": kind} if kind else {}
        if server_wait is not None:
            extra["retry_after_s"] = round(server_wait, 3)
        wait = None
        if kind == "auth" or status in STOP_STATUS or _flex_unsupported(status, text, self.deployment):
            billing, result = REJECTED, RETRYING  # nothing more is sent; a re-run continues this round
            self._stop_job(self._stop_message(status, kind, text))
            note = "stopping the job"
        elif kind in ("timeout", "connection"):
            # No answer, but the request may have reached the service, and a Flex request can queue: the service may
            # still process and bill it. Resending could pay twice for one line, so it's held; the other lines carry on.
            billing, result = UNKNOWN, OUTCOME_UNKNOWN
            note = "no answer, but it may have been processed and billed: held, not resent (--retry-ambiguous sends it again)"
        elif status in RETRY_STATUS:
            billing = NOT_BILLED if status == 429 else UNKNOWN
            if attempt >= self.max_attempts:
                result, note = USED_UP, f"used up its {self.max_attempts} attempts; --retry-failed sends it again"
            elif server_wait is not None and server_wait > self.max_server_wait:
                result = RETRYING
                until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=min(server_wait, 1e7))
                note = extra["note"] = (f"the service asked to wait {server_wait:.0f} s, longer than max_server_wait_seconds "
                                        f"({self.max_server_wait:.0f}); left for a re-run after {until.isoformat(timespec='seconds')}")
            else:
                wait = max(backoff(attempt), server_wait or 0.0)
                result, extra["wait_s"] = RETRYING, round(wait, 1)
                note = f"attempt {attempt} of {self.max_attempts}; next in {wait:.0f} s"
        elif status is None:
            # Not an HTTP error: whether the request reached the service is unknown, so the line is held.
            billing, result, extra["error_kind"] = UNKNOWN, OUTCOME_UNKNOWN, "exception"
            self._stop_job(f"Stopped: an unexpected error while sending {cid} ({text[:300]}). Whether that request reached the "
                           "service is unknown, so its line is held (--retry-ambiguous sends it again); nothing more was sent.")
            note = "outcome unknown; stopping the job"
        else:
            billing, result = (REJECTED if 400 <= status < 500 else UNKNOWN), FAILED
            note = "not retried"
        self._outcome(cid, rnd, attempt, http_status=status, billing=billing, result=result, seconds=seconds, error=text, **extra)
        if status == 429:
            with self.lock:
                self.throttled += 1
        self.say(f"  {cid}: {text[:200]} ({note})")
        return wait

    def _stop_job(self, message: str) -> None:
        with self.lock:
            self.stopped = self.stopped or message
        self.stop.set()

    def _stop_message(self, status: int | None, kind: str | None, text: str) -> str:
        if status == 400:
            return (f"Stopped: deployment '{self.deployment}' doesn't support Flex processing ({text[:300]}). Flex needs a Global "
                    "Standard deployment of a Flex-capable model, for example gpt-6-sol; every other request would fail the "
                    "same way, so nothing more was sent.")
        if status == 404:
            return (f"Stopped: HTTP 404 for deployment '{self.deployment}' ({text[:300]}). Check --deployment and --base-url; "
                    "nothing more was sent.")
        what = "couldn't get a Microsoft Entra ID token" if kind == "auth" else f"HTTP {status}"
        return (f"Stopped: {what} for deployment '{self.deployment}' ({text[:300]}). Sign in again (az login), or check "
                "AZURE_OPENAI_API_KEY or your access to the Azure OpenAI resource; nothing more was sent.")


def _wait_all(futures: list, sender: _Sender | None = None, total: int = 0) -> None:
    """Wait for every future in short steps, so Ctrl+C is noticed (a wait without a timeout can't be interrupted on
    Windows); with a sender, print progress now and then."""
    pending, shown = set(futures), time.monotonic()
    while pending:
        done, pending = cf.wait(pending, timeout=0.5, return_when=cf.FIRST_COMPLETED)
        for f in done:
            if not f.cancelled():
                f.result()  # a worker's unexpected error (the ledger can't be written, say) stops the run
        if sender and time.monotonic() - shown >= PROGRESS_SECONDS:
            shown = time.monotonic()
            sender.say(sender.progress(total))


def _send(sender: _Sender, todo: list[tuple[str, int, int]], concurrency: int) -> str | None:
    """Send the lines, `concurrency` at a time. Returns why the run stopped early, or None.

    Ctrl+C stops new requests and retries, and waits for the requests in flight so their outcomes are recorded. A second
    Ctrl+C leaves at once: those requests keep a start row with no outcome, so a re-run holds their lines.
    """
    pool = cf.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="flex")
    futures: list = []
    try:
        for item in todo:
            futures.append(pool.submit(sender.send_line, *item))
        _wait_all(futures, sender, len(todo))
    except KeyboardInterrupt:
        sender.stop.set()
        for f in futures:
            f.cancel()
        print(f"\nStopping: no new requests are sent. Waiting for {sender.in_flight} request(s) in flight so their outcomes are "
              "recorded; press Ctrl+C again to leave now (their lines are then held).", file=sys.stderr, flush=True)
        try:
            _wait_all(futures)
        except KeyboardInterrupt:
            print("Left without waiting: the requests in flight have no outcome in the ledger, so a re-run holds their lines "
                  "(--retry-ambiguous sends them again).", file=sys.stderr, flush=True)
            os._exit(130)  # the worker threads are blocked in their requests and can't be joined
        pool.shutdown(wait=True)
        return "Interrupted (Ctrl+C); the requests in flight finished and were recorded."
    except BaseException:
        sender.stop.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return sender.stopped


def run_lines(lines: list[dict], workdir: str | os.PathLike, *, deployment: str, base_url: str | None = None,
              subscription: str | None = None, client=None, concurrency: int | None = None, retry_failed: bool = False,
              retry_ambiguous: bool = False) -> dict:
    """Send every line that still needs sending with Flex processing, then read back the state of every line.

    Returns {"rows": one metrics row per line, sorted by custom_id, in the shape the Batch path writes to metrics.jsonl
    (an error row for a line without a Flex response); "summary": lines and attempts, for run.json; "stopped": why the
    run stopped early (Ctrl+C, or an error every request would get), or None}. `client` replaces the OpenAI client.
    """
    problems = check_lines(lines)
    if problems:
        raise SystemExit("Refusing to send: " + "; ".join(problems))
    max_attempts = int(_F["max_attempts"])
    concurrency = max(1, int(concurrency or _F["concurrency"]))
    bodies = {line["custom_id"]: flex_body(line, deployment) for line in lines}
    hashes = {cid: request_sha256(body) for cid, body in bodies.items()}
    states = _states(read_ledger(workdir), bodies, max_attempts, retry_failed, retry_ambiguous)
    changed = sorted(c for c, s in states.items() if s["state"] == "completed" and s["row"].get("request_sha256") != hashes[c])
    if changed:
        print(f"WARNING: {len(changed)} completed line(s), for example {changed[:3]}, were sent with a different request "
              "(the deployment, settings or input changed since); their results are kept. Use a new --workdir to start over.")
    todo = [(c, s["round"], s["used"]) for c, s in states.items() if s["send"]]
    skip = Counter(s["state"] for s in states.values() if not s["send"])
    notes = [
        f"{skip['held']} held because a request's outcome is unknown, for example it timed out or was in flight when a run "
        "stopped (--retry-ambiguous sends them again)" if skip["held"] else "",
        f"{skip['used_up']} held after using up their attempts (--retry-failed sends them again)" if skip["used_up"] else "",
        f"{skip['failed']} failed with errors retrying can't fix" if skip["failed"] else "",
    ]
    print(f"Flex: {len(lines)} lines: {skip['completed']} completed earlier, {len(todo)} to send to '{deployment}' "
          f"({concurrency} at a time, at most {max_attempts} attempts each)" + "".join(f"; {n}" for n in notes if n)
          + f". Ledger: {LEDGER}")
    sent, stopped = 0, None
    if todo:
        client = client or make_client(base_url, subscription)
        ledger = _Ledger(pathlib.Path(workdir) / LEDGER)
        try:
            sender = _Sender(client, bodies, hashes, ledger, deployment, max_attempts)
            stopped = _send(sender, todo, concurrency)
            sent = sender.sent
        finally:
            ledger.close()
    rows = read_ledger(workdir)
    states = _states(rows, bodies, max_attempts)
    summary = summarize(rows, states, sent=sent, concurrency=concurrency, max_attempts=max_attempts)
    summary["stopped"] = stopped
    return {"rows": [metric_row(c, states[c]) for c in sorted(bodies)], "summary": summary, "stopped": stopped}
