"""The job runner every option uses: the `plan`, `search`, `build` and `run` commands, for any design, on Global Batch or Flex.

Each design's own code is loaded from its folder when a command needs it: option-1-bounded-web-search (lines in which
the model searches), option-2-two-stage-web-iq (the search plan, Web IQ and the no-tools lines) and
flex-web-search-and-web-iq (Flex processing). Each folder's entry script fixes the design or the executor.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import pathlib
import sys
import time
from collections import Counter
from typing import Iterable

import batch as batchlib
import exits
import folders
from batch_lines import DESIGNS, line_text
from cost import actual, estimate_before_search, exact_before_submit, print_forecast
from exits import HELD_RECORDS, HELD_STATUS, Exit
from prices import load_prices, model_rates, source_line, tier_for
from settings import SETTINGS
from util import count_tokens, now, read_json, read_jsonl, script, write_json, write_jsonl
from workloads import get_workload

TOOL_VERSION = "0.3.0"  # recorded in run.json
FLEX_LEDGER = "flex-attempts.jsonl"  # flex.py's LEDGER: a work folder that has one holds a Flex job
BATCH_PART = "batch-input-part.jsonl"  # one batch job's lines while they're uploaded; deleted once they are
_B = SETTINGS["batch"]


def option_1(module: str):
    """A module of option 1, bounded web search (option-1-bounded-web-search/)."""
    return folders.load(folders.OPTION_1, module)


def option_2(module: str):
    """A module of option 2, two-stage with Web IQ (option-2-two-stage-web-iq/)."""
    return folders.load(folders.OPTION_2, module)


def flex_executor():
    """The Flex executor (flex-web-search-and-web-iq/flex.py)."""
    return folders.load(folders.FLEX, "flex")


PRICES_USED = "prices-used.json"  # the price table a job started with; a resumed job is priced with it again


def run_prices(a, wd: pathlib.Path) -> tuple[dict, dict]:
    """The job's prices: read (live, or from --prices) when it starts and saved in the work folder, so every part of a
    resumed job is priced the same way even if list prices change in between."""
    p = wd / PRICES_USED
    if p.exists():
        if getattr(a, "prices", None):
            print(f"--prices applies to new jobs only; this job keeps the prices it started with ({p.name}).")
        table = read_json(p)
    else:
        table = load_prices(a.prices)
        write_json(p, table)
    info = {"file": PRICES_USED, "source": table.get("source"), "retrieved_utc": table.get("retrieved_utc"),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    return table, info


def _merge_counts(counts: Iterable[dict]) -> dict:
    out: dict[str, int] = {}
    for c in counts:
        for k, v in c.items():
            out[k] = out.get(k, 0) + v
    return out


def read_kept_lines(path: pathlib.Path) -> list[dict]:
    """Settled lines (per-line metrics) kept from earlier batch jobs of this job: answered, failed for good, or held."""
    return read_jsonl(path) if path.exists() else []


def keep_lines(path: pathlib.Path, rows: list[dict]) -> None:
    """Add settled lines to kept-lines.jsonl, once each, replacing the file in one step: a run that stops after this
    and collects the same jobs again adds nothing twice."""
    kept = read_kept_lines(path)
    have = {r["custom_id"] for r in kept}
    tmp = path.with_name(path.name + ".tmp")
    write_jsonl(tmp, kept + [r for r in rows if r["custom_id"] not in have])
    os.replace(tmp, path)


def remaining_lines(lines: list[dict], done_ids: set[str]) -> list[dict]:
    """Batch lines whose custom_id hasn't been settled in an earlier batch job of the same job."""
    return [line for line in lines if line["custom_id"] not in done_ids]


def is_flex(a) -> bool:
    return getattr(a, "executor", None) == "flex"


def forecast_title(text: str, a) -> str:
    """Flex forecasts say so; Global Batch forecasts keep their plain titles."""
    return f"{text} at Flex rates ({a.price_model})" if is_flex(a) else text


def in_order(rows: list[dict], order: list[str]) -> list[dict]:
    """Rows in the order of `order` (record ids, as in the records file), so results follow the input."""
    rank = {rid: i for i, rid in enumerate(order)}
    return sorted(rows, key=lambda r: (rank.get(r.get("custom_id"), len(rank)), str(r.get("custom_id"))))


def results_rows(rows: list[dict], order: list[str] | None = None) -> list[dict]:
    """results.jsonl: one row per line, in the records' input order when `order` is given, else by custom_id."""
    rows = in_order(rows, order) if order is not None else sorted(rows, key=lambda r: str(r.get("custom_id")))
    return [
        {"id": r["custom_id"], "status": r.get("status"), "result": r.get("output"), "error": r.get("error") or (None if r.get("json_valid") else "output is not valid JSON")}
        for r in rows
    ]


def held_row(row: dict, why: str) -> dict:
    """A line that isn't answered and isn't sent again by this job: status "held", with why and its last error."""
    last = f": {row['error']}" if row.get("error") else ""
    return {"custom_id": row["custom_id"], "status": HELD_STATUS, "error": f"{why}{last}"[:500]}


def held_by_search(workload, records: list[dict], evidence: list[dict]) -> dict[str, dict]:
    """Records that use a search that still failed: {record id: a held row naming the search}. They aren't sent to the
    model, because a record without its evidence gets a confident wrong answer, not an error."""
    failed: dict[str, list[dict]] = {}
    for e in evidence:
        if e.get("error"):
            for rid in e.get("used_by") or []:
                failed.setdefault(rid, []).append(e)
    out = {}
    for r in records:
        es = failed.get(workload.record_id(r))
        if es:
            more = f" (and {len(es) - 1} more searches)" if len(es) > 1 else ""
            out[workload.record_id(r)] = held_row({"custom_id": workload.record_id(r), "error": es[0]["error"]},
                                                  f"not sent to the model: search {es[0]['query']!r} failed{more}")
    return out


def write_outputs(wd: pathlib.Path, workload, records: list[dict], rows: list[dict], run: dict, held_by_searches: int) -> list[dict]:
    """metrics.jsonl and results.jsonl, one row per record in input order; held-records.jsonl, the held records in the
    input records format to run again as a new job (removed when none is held); and the held count in run.json."""
    order = [workload.record_id(r) for r in records]
    rows = in_order(rows, order)
    write_jsonl(wd / "metrics.jsonl", rows)
    results = results_rows(rows, order)
    write_jsonl(wd / "results.jsonl", results)
    held = {r["custom_id"] for r in rows if r.get("status") == HELD_STATUS}
    if held:
        write_jsonl(wd / HELD_RECORDS, [r for r in records if workload.record_id(r) in held])
    elif (wd / HELD_RECORDS).exists():
        (wd / HELD_RECORDS).unlink()
    run["held"] = {"records": len(held), "search_failed": held_by_searches, "not_answered": len(held) - held_by_searches,
                   "file": HELD_RECORDS if held else None}
    return results


def remove_working_files(a, paths: list[pathlib.Path]) -> list[str]:
    removed = []
    if not a.keep_working_files:
        for p in paths:
            if p.exists():
                p.unlink()
                removed.append(p.name)
    return removed


def held_note(run: dict) -> str:
    n = (run.get("held") or {}).get("records") or 0
    return f"; {n} records held, listed in {HELD_RECORDS} to run again as a new job" if n else ""


def load_records(a):
    workload = get_workload(a.workload)
    records = read_jsonl(a.records)
    problems = {workload.record_id(r) if r.get("id") is not None else f"line {i}": p for i, r in enumerate(records, 1) if (p := workload.problems(r))}
    if problems:
        print(f"{len(problems)} record(s) with data problems:")
        for rid, p in list(problems.items())[:10]:
            print(f"  {rid}: {'; '.join(p)}")
        if not getattr(a, "allow_problems", False):
            raise SystemExit("Fix the records or pass --allow-problems.")
    return workload, records


def order_for_caching(workload, records: list[dict], plan: dict[str, dict]) -> list[dict]:
    """Put records that share their first evidence block next to each other (same long prompt prefix)."""
    if not workload.evidence_order:
        return records
    first = workload.evidence_order[0]
    key: dict[str, str] = {}
    for e in plan.values():
        if e["purpose"] == first:
            for rid in e["used_by"]:
                key.setdefault(rid, e["query"].lower())
    return sorted(records, key=lambda r: key.get(workload.record_id(r), ""))


def build_lines(workload, records, design: str, a, evidence: list[dict] | None = None, plan: dict | None = None) -> list[dict]:
    if design == "two-stage":
        by_rec = option_2("search_plan").evidence_by_record(evidence or [])
        missing = [workload.record_id(r) for r in records if not by_rec.get(workload.record_id(r))]
        if missing:
            raise SystemExit(f"No evidence for {len(missing)} record(s), for example {missing[:5]}; refusing to build the batch.")
        ordered = order_for_caching(workload, records, plan or {})
        return [
            option_2("two_stage_lines").two_stage_line(
                workload,
                r,
                by_rec[workload.record_id(r)],
                deployment=a.deployment,
                reasoning_effort=a.reasoning_effort,
                max_output_tokens=a.max_output_tokens,
                evidence_chars=a.max_chars,
            )
            for r in ordered
        ]
    bounded = design == "bounded-web-search"
    return [
        option_1("web_search_lines").web_search_line(
            workload,
            r,
            deployment=a.deployment,
            bounded=bounded,
            max_tool_calls=a.max_tool_calls,
            reasoning_effort=a.reasoning_effort,
            max_output_tokens=a.max_output_tokens,
        )
        for r in records
    ]


def cmd_plan(a) -> None:
    workload, records = load_records(a)
    if a.design != "two-stage":
        tokens = sum(count_tokens(line_text(line))[0] for line in build_lines(workload, records, a.design, a))
        print(
            f"{a.design}: {len(records)} records, {tokens:,} prompt tokens before any search. The model decides how many "
            "searches to run, so search cost and search-added input tokens can't be forecast; measure them with "
            f"{script('shared', 'analyze.py')} on a finished job's output."
        )
        return
    plan = option_2("search_plan").build_plan(records, workload, a.plan)
    s = option_2("search_plan").summarize_plan(plan, len(records))
    print(
        f"Plan {a.plan}: {s['searches']} unique searches for {s['records']} records = {s['searches_per_record']:.2f} per record "
        f"{s['by_purpose']}; sharing saves {s['saved_by_sharing']} of {s['searches_without_sharing']}"
    )
    if a.show_searches:
        for e in plan.values():
            print(f"  [{e['purpose']}] region={e['region'] or '-'}  {e['query']}  (used by {len(e['used_by'])})")
    prices = load_prices(a.prices)
    print_forecast(forecast_title("Forecast before searching", a), estimate_before_search(workload, records, plan, a, prices))
    print(f"Prices: {source_line(prices)}")


def cmd_search(a) -> None:
    workload, records = load_records(a)
    plan = option_2("search_plan").build_plan(records, workload, a.plan)
    entries, stats = option_2("webiq").run_searches(plan, a.out, a)
    errors = [e for e in entries if e.get("error")]
    print(f"Wrote {len(entries)} searches to {a.out}; results per search: {[len(e.get('results') or []) for e in entries]}; errors: {len(errors)}")
    print(f"This session: {stats}")
    for e in errors:
        print(f"  ERROR {e['query']}: {e['error']}")


def cmd_build(a) -> None:
    workload, records = load_records(a)
    evidence = read_jsonl(a.evidence) if a.evidence else None
    plan = option_2("search_plan").build_plan(records, workload, a.plan) if a.design == "two-stage" else None
    if a.design == "two-stage" and evidence is None:
        raise SystemExit("--evidence is required for --design two-stage (run `two_stage.py search` first).")
    failed = [e for e in evidence or [] if e.get("error")]
    if failed:
        raise SystemExit(f"{len(failed)} searches in {a.evidence} failed; re-run `two_stage.py search` to retry them before building.")
    lines = build_lines(workload, records, a.design, a, evidence, plan)
    n = write_jsonl(a.out, lines)
    print(f"Wrote {n} {a.design} lines to {a.out}")


def cmd_run(a) -> int:
    """A whole job: plan, search, build, forecast, send, wait, collect, price and clean up; re-run the same command to
    resume. Returns the exit code of a run that ended (exits.DONE, or exits.HELD when records are held); raises
    exits.Exit, or SystemExit with a message (exit code 1), for the others (exits.py)."""
    if a.design not in DESIGNS:
        raise SystemExit(f"Unknown design {a.design}")
    if a.design != "two-stage" and a.max_cost is not None:
        raise SystemExit("--max-cost can't be enforced when the model decides how many searches to run. Remove it to run this design.")
    flex = is_flex(a)
    wd = pathlib.Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    run_path, state_path = wd / "run.json", wd / "state.json"
    evidence_path, batch_path = wd / "evidence.jsonl", wd / "batch-input.jsonl"
    kept_path = wd / "kept-lines.jsonl"  # settled lines of earlier batch jobs of this job
    run = read_json(run_path) if run_path.exists() else {}
    if run.get("status") == "done":
        per = (run.get("actual") or {}).get("usd_per_1000_records")
        print(f"{wd} is already done: " + (f"${per} per 1,000 records" if per is not None else "nothing was sent to the model")
              + held_note(run) + f". Results: {wd / 'results.jsonl'}")
        return exits.HELD if (run.get("held") or {}).get("records") else exits.DONE
    if flex and (state_path.exists() or kept_path.exists()):
        raise SystemExit(f"{wd} holds a Batch job (state.json or kept-lines.jsonl): finish it with the option's own script, which runs on Global Batch, or use a new --workdir.")
    if not flex and (wd / FLEX_LEDGER).exists():
        raise SystemExit(f"{wd} holds a Flex job ({FLEX_LEDGER}): continue it with {script('flex-web-search-and-web-iq', 'flex_job.py')}, or use a new --workdir.")
    workload, records = load_records(a)
    prices, price_info = run_prices(a, wd)
    print(f"Prices: {source_line(prices)}")
    if flex:
        model_rates(prices, a.price_model, "flex")  # stop before any search or request if the model has no Flex prices
    run.update(
        tool_version=TOOL_VERSION,
        label=a.label or wd.name,
        design=a.design,
        workload=workload.name,
        records_file=os.path.basename(a.records),
        records=len(records),
        deployment=a.deployment,
        price_model=a.price_model,
        prices=price_info,
        settings={
            "reasoning_effort": a.reasoning_effort if a.design != "web-search" else "(model default)",
            "max_output_tokens": a.max_output_tokens,
            "max_tool_calls": a.max_tool_calls if a.design == "bounded-web-search" else None,
            "auto_retry": not getattr(a, "no_auto_retry", False),
        },
    )
    if flex:
        run.update(executor="flex", tier="flex")
    run.setdefault("started_at", now())
    webiq_calls, held, lines = 0, {}, None

    if a.design == "two-stage":
        plan = option_2("search_plan").build_plan(records, workload, a.plan)
        webiq_calls = len(plan)
        run.update(
            plan=a.plan,
            plan_summary=option_2("search_plan").summarize_plan(plan, len(records)),
            search_settings={"via": a.via, "max_results": a.max_results, "max_chars": a.max_chars, "language": a.language, "qps": a.qps},
        )

    if not state_path.exists():
        if a.design == "two-stage":
            pre = estimate_before_search(workload, records, plan, a, prices)
            run["forecast_before_search"] = pre
            print_forecast(forecast_title("Forecast before searching", a), pre)
            if a.max_cost is not None and pre["usd"]["total"] > a.max_cost:
                write_json(run_path, run)
                raise SystemExit(f"Forecast ${pre['usd']['total']:.2f} is above --max-cost {a.max_cost}. Nothing was called.")
            evidence, session = option_2("webiq").run_searches(plan, evidence_path, a)
            errors = [e for e in evidence if e.get("error")]
            run.setdefault("stage1_sessions", []).append(session)
            run["stage1"] = {
                "searches": len(evidence),
                "errors": len(errors),
                "sessions": len(run["stage1_sessions"]),
                "rounds": session.get("rounds"),
                "attempts": sum(s["attempts"] for s in run["stage1_sessions"]),
                "failed_attempts_by_status": _merge_counts(s["failed_attempts_by_status"] for s in run["stage1_sessions"]),
                "seconds": round(sum(s["seconds"] for s in run["stage1_sessions"]), 1),
                "mean_latency_s": round(sum(e.get("latency_s", 0) for e in evidence) / max(1, len(evidence)), 2),
                "results_per_search_mean": round(sum(len(e.get("results") or []) for e in evidence) / max(1, len(evidence)), 2),
                "searches_with_no_results": sum(1 for e in evidence if not e.get("error") and not e.get("results")),
                "premium_results": sum(1 for e in evidence for r in e.get("results") or [] if r.get("contentTier") == "premium"),
            }
            write_json(run_path, run)
            held = held_by_search(workload, records, evidence)
            webiq_calls = len(evidence) - len(errors)  # a failed search returned nothing, so it isn't priced
            if errors:
                for e in errors[:10]:
                    print(f"  ERROR {e['query']}: {e['error']}")
                # Another 4xx (a bad query, sign-in or permissions) fails the same way however often it's retried.
                stuck = sorted({s for e in errors if isinstance(s := e.get("http_status"), int) and 400 <= s < 500 and s not in (408, 429)})
                if getattr(a, "no_auto_retry", False):
                    raise Exit(exits.NEEDS_PERSON if stuck else exits.RERUN,
                               f"{len(errors)} searches failed; nothing was submitted (--no-auto-retry). Re-run the same command to retry only those.")
                print(f"{len(errors)} searches still failed after the automatic rounds; the {len(held)} records that use them are "
                      f"held: they aren't sent to the model, and {HELD_RECORDS} lists them at the end.")
                if len(held) == len(records):
                    return all_held(a, run, run_path, wd, workload, records, held, prices, webiq_calls, stuck, [evidence_path])
            lines = build_lines(workload, [r for r in records if workload.record_id(r) not in held], a.design, a, evidence, plan)
            write_jsonl(batch_path, lines)
            exact = exact_before_submit(lines, webiq_calls, a, prices)
            run["forecast_before_submit"] = exact
            print_forecast(forecast_title("Forecast before submitting", a), exact)
            write_json(run_path, run)
            if a.max_cost is not None and exact["usd"]["total"] > a.max_cost:
                raise SystemExit(f"Forecast ${exact['usd']['total']:.2f} is above --max-cost {a.max_cost}. Searches ran; "
                                 + ("no Flex request was sent." if flex else "the batch was not submitted."))
        else:
            lines = build_lines(workload, records, a.design, a)
            write_jsonl(batch_path, lines)
            run["prompt_tokens_before_search"] = sum(count_tokens(line_text(line))[0] for line in lines)
            print(f"{a.design}: {len(lines)} lines; the model decides how many searches to run, so cost is known only afterwards.")
        if flex:
            return run_flex(a, run, run_path, wd, workload, records, lines, held, prices, webiq_calls, [evidence_path, batch_path])
    elif a.design == "two-stage" and evidence_path.exists():
        # A submitted job, resumed: its searches ran when it started.
        evidence = read_jsonl(evidence_path)
        held = held_by_search(workload, records, evidence)
        webiq_calls = sum(1 for e in evidence if not e.get("error"))
    return run_batch(a, run, run_path, wd, workload, records, lines, held, prices, webiq_calls)


def all_held(a, run: dict, run_path: pathlib.Path, wd: pathlib.Path, workload, records: list[dict], held: dict, prices: dict,
             webiq_calls: int, stuck: list[int], working_files: list[pathlib.Path]) -> int:
    """Every record uses a search that still failed, so nothing is sent to the model. Exit code 1 if a failure needs a
    person (another 4xx: a bad query, sign-in or permissions), else 3."""
    rows = list(held.values())
    write_outputs(wd, workload, records, rows, run, len(rows))
    run["actual"] = actual(rows, records=len(records), prices=prices, price_model=a.price_model, webiq_calls=webiq_calls,
                           tier=tier_for(getattr(a, "executor", None)))
    if stuck:
        run["status"] = "searches failed"
        write_json(run_path, run)
        raise Exit(exits.NEEDS_PERSON, f"Every record is held: searches failed with HTTP {', '.join(map(str, stuck))}, which "
                   "retrying can't fix (a bad query, sign-in or permissions). Nothing was sent to the model. Fix the cause and "
                   "re-run the same command: only the failed searches run again.")
    removed = remove_working_files(a, working_files)
    run.update(status="done", finished_at=now(), deleted={"service_files": 0, "local_files": removed})
    write_json(run_path, run)
    print(f"Every record is held: their searches still failed after {run['stage1']['rounds']} rounds, so nothing was sent to "
          f"the model. {wd / HELD_RECORDS} lists them: run them as a new job later.")
    return exits.HELD


def max_wait_deadline(a) -> float | None:
    """When this run stops waiting (--max-wait-minutes, else [batch] max_wait_minutes); None waits until every job ends."""
    minutes = getattr(a, "max_wait_minutes", None)
    minutes = float(_B["max_wait_minutes"] if minutes is None else minutes)
    return time.time() + minutes * 60 if minutes > 0 else None


def round_jobs(lines: list[dict]) -> list[dict]:
    """A round's batch jobs: the lines in one file, or split into parts when they exceed the file limits."""
    return [{"part": i, "custom_ids": [line["custom_id"] for line in part]} for i, part in enumerate(batchlib.split_parts(lines), 1)]


def batch_summary(batches: list[dict]) -> dict:
    """run.json "batch": the job's timing, or, over several jobs (parts and rounds), from the first created to the last ended."""
    if len(batches) == 1:
        return {k: v for k, v in batches[0].items() if k not in ("round", "part", "lines")}
    starts = [b["created_at"] for b in batches if b.get("created_at")]
    ends = [b["completed_at"] for b in batches if b.get("completed_at")]
    first, last = (min(starts) if starts else None), (max(ends) if ends else None)
    return {
        "jobs": len(batches),
        "rounds": max(b.get("round") or 1 for b in batches),
        "statuses": dict(Counter(b.get("status") for b in batches)),
        "created_at": first,
        "completed_at": last,
        "minutes_end_to_end": round((dt.datetime.fromisoformat(last) - dt.datetime.fromisoformat(first)).total_seconds() / 60, 1)
        if first and last else None,
        "request_counts": {k: sum((b.get("request_counts") or {}).get(k) or 0 for b in batches) for k in ("total", "completed", "failed")},
    }


def create_job(client, job: dict, state: dict, state_path: pathlib.Path, endpoint: str, budget) -> object:
    """Create the batch job for an uploaded part, once only.

    A create can go through without its answer reaching this run: the process was stopped while it was in flight, or
    the call timed out, lost its connection or failed with HTTP 5xx. Two jobs from one file would bill twice, so:
      - every attempt is recorded in state.json as "sent" before it's sent, and is sent with the SDK's retries off;
      - after the first attempt, the resource's batch jobs are listed before each new one, and a job made from the
        same file is waited for instead of creating another; a new create is sent only when the list shows none;
      - if the jobs can't be listed, the attempt is marked "uncertain" and nothing is deleted: CreateUncertain is raised
        for an error that may pass (no answer, HTTP 408, 409, 429 or 5xx), ListRefused for a sign-in or access problem
        or any other error; the next run lists them before anything else;
      - once an attempt may have gone through ("maybe_created"), the upload and the record of the attempts are kept,
        whatever happens next.
    A create refused for now (a full quota, HTTP 429) or one whose outcome is unknown is tried again after the
    budget's pause; QuotaWaitOver is raised when the budget runs out. Any other error is raised."""
    if job.get("create") in ("sent", "uncertain"):
        job["maybe_created"] = True  # an earlier run stopped without learning what its attempt did
    while True:
        if job.get("attempts"):
            try:
                b = batchlib.find_job(client, job["input_file_id"], skip=job.get("ended_jobs", []))
            except Exception as ex:  # no answer yet, not "no job"
                job["create"] = "uncertain"
                write_json(state_path, state)
                why = f"{type(ex).__name__}: {ex}"
                if batchlib.passing(ex):
                    raise batchlib.CreateUncertain(f"the batch jobs couldn't be listed to check ({why})") from None
                raise batchlib.ListRefused(f"listing the batch jobs to check was refused ({why})") from None
            if b is not None:
                job.pop("create", None)
                print(f"  Found batch {b.id}, created from this file by an earlier attempt: waiting for it instead of creating another")
                return b
        job.update(attempts=int(job.get("attempts") or 0) + 1, create="sent", create_sent_at=now())
        write_json(state_path, state)
        try:
            b = batchlib.create(client, job["input_file_id"], endpoint)
        except Exception as ex:
            if batchlib.refused_for_now(ex):
                job["create"] = "refused"  # this attempt created nothing
                why = "the service refused to create the job for now (a full enqueued-token quota or HTTP 429; nothing was created)"
            elif batchlib.ambiguous(ex):
                job["maybe_created"] = True
                why = f"creating the job got no answer ({type(ex).__name__}), so the jobs are listed before trying again"
            else:
                job["create"] = "refused"
                write_json(state_path, state)
                raise
            write_json(state_path, state)
            budget.wait(why)
            continue
        job.pop("create", None)
        return b


def submit_round(client, run: dict, run_path: pathlib.Path, wd: pathlib.Path, state: dict, state_path: pathlib.Path, line_map,
                 budget) -> None:
    """Upload and create each job of the round that isn't created yet, recording every step in state.json.

    A full enqueued-token quota is waited out with the run's budget (batch.QuotaBudget). If the budget runs out, the
    run ends with exit code 75 and the uploaded file stays recorded in state.json, so a re-run creates the job from it
    without uploading again."""
    jobs = state["jobs"]
    for job in jobs:
        if job.get("batch_id"):
            continue
        part = [line_map()[c] for c in job["custom_ids"]]
        label = f"{run['label']} round {state['round']}" + (f" part {job['part']} of {len(jobs)}" if len(jobs) > 1 else "")
        if not job.get("input_file_id"):
            path = wd / BATCH_PART
            write_jsonl(path, part)
            try:
                job["input_file_id"] = batchlib.upload(client, path, part, label)
            finally:
                path.unlink(missing_ok=True)
            write_json(state_path, state)
        try:
            b = create_job(client, job, state, state_path, part[0]["url"], budget)
        except batchlib.QuotaWaitOver as ex:
            run["status"] = "waiting for quota"
            write_json(run_path, run)
            if job.get("maybe_created"):
                raise Exit(exits.RERUN, f"Batch {label}: {ex}. An earlier attempt got no answer, so the job may or may not "
                           "exist. The uploaded file is kept, and running the same command again looks for the job before "
                           "creating it.") from None
            raise Exit(exits.RERUN, f"Batch {label}: {ex}. Its job wasn't created (nothing was billed), and the uploaded file is "
                       "kept: re-run the same command to create the job from it.") from None
        except batchlib.CreateUncertain as ex:
            run["status"] = "creating a batch job: uncertain"
            write_json(run_path, run)
            raise Exit(exits.RERUN, f"Batch {label}: a create may have gone through, but {ex}. Nothing was deleted: running the "
                       "same command again lists the batch jobs first, and waits for the job if it exists.") from None
        except batchlib.ListRefused as ex:
            run["status"] = "creating a batch job: needs sign-in or access"
            write_json(run_path, run)
            raise Exit(exits.NEEDS_PERSON, f"Batch {label}: a create may have gone through, but {ex}. Nothing was deleted. Fix the "
                       "sign-in (az login), your access to the Azure OpenAI resource or the endpoint, then run the same command "
                       "again: it lists the batch jobs first, and waits for the job if it exists.") from None
        except KeyboardInterrupt:
            raise  # the upload and the create attempt are recorded in state.json; a re-run looks for the job first
        except BaseException:
            if job.get("maybe_created"):
                # An earlier attempt may have created the job: keep the upload and the record of the attempts, so the
                # next run looks for that job instead of uploading again and creating a second one.
                print(f"Batch {label}: creating the job failed. An earlier attempt may have created it, so the uploaded "
                      "file is kept, and running the same command again looks for that job first.", file=sys.stderr)
                raise
            # The service refused the only attempt, so no job uses the file: don't leave its prompts (and Web IQ content)
            # in the file store.
            batchlib.delete_files(client, [job.pop("input_file_id")])
            for key in ("attempts", "create", "create_sent_at"):
                job.pop(key, None)
            write_json(state_path, state)
            raise
        job.update(batch_id=b.id, submitted_at=batchlib.ts(int(time.time())))
        write_json(state_path, state)
        print(f"Submitted batch {b.id} ({label}): {len(part)} lines, deployment {part[0]['body']['model']}, status {b.status}")


def wait_round(client, a, run: dict, run_path: pathlib.Path, state: dict, state_path: pathlib.Path, deadline: float | None) -> dict:
    """Wait until every job of the round has ended; {batch id: batch}. A job this run cancels is recorded in state.json,
    so its unanswered lines are sent again; --max-wait-minutes ends the run with exit code 75, the jobs still running."""
    jobs = state["jobs"]

    def cancelled_by_run(batch_id: str) -> None:
        next(j for j in jobs if j["batch_id"] == batch_id)["cancelled_by_run"] = True
        write_json(state_path, state)

    try:
        return batchlib.wait_all(client, [j["batch_id"] for j in jobs], a.poll_seconds, a.stall_minutes, a.cancel_on_stall,
                                 deadline, cancelled_by_run)
    except batchlib.WaitOver as ex:
        run["status"] = "batch running"
        write_json(run_path, run)
        raise Exit(exits.RERUN, f"Stopped waiting (--max-wait-minutes): {ex}. They keep running: re-run the same command to "
                   "wait for them and collect the results.") from None


def run_batch(a, run: dict, run_path: pathlib.Path, wd: pathlib.Path, workload, records: list[dict], lines: list[dict] | None,
              held: dict, prices: dict, webiq_calls: int) -> int:
    """Send the lines as Global Batch jobs and wait, in rounds, until every line is settled, then write metrics.jsonl,
    results.jsonl, held-records.jsonl and run.json, and clean up.

    A round's lines go in one batch job, or in several when they exceed the file limits (100,000 lines or 200 MB),
    waited on together. When every job of the round has ended, its lines are sorted (batch.sort_lines): answered lines
    and lines that failed for good are settled; lines that may pass are sent again in a new round, up to max_rounds in
    all for this run's lines (with --no-auto-retry, they wait for a re-run); lines of a job someone cancelled outside
    this run are held. A job that failed because the enqueued-token quota was full isn't a round: it's created again
    from the same file once the quota has had time to free up. state.json holds the round in progress, and
    kept-lines.jsonl every settled line, so a re-run of the same command resumes, and never sends a settled line again.
    """
    state_path, kept_path, batch_path = wd / "state.json", wd / "kept-lines.jsonl", wd / "batch-input.jsonl"
    auto, max_rounds = not getattr(a, "no_auto_retry", False), int(_B["max_rounds"])
    deadline = max_wait_deadline(a)
    budget = batchlib.QuotaBudget(deadline)
    state = read_json(state_path) if state_path.exists() else None
    if state and "jobs" not in state:  # a job submitted by an earlier version of this code: one job, every line not kept
        state = {"base_url": state["base_url"], "subscription": state.get("subscription"), "round": 1, "first_round": 1,
                 "jobs": [{"part": 1, "custom_ids": None, **{k: state.get(k) for k in ("input_file_id", "batch_id", "submitted_at")}}]}
    by_id: dict[str, dict] = {}

    def line_map() -> dict[str, dict]:
        if not by_id:
            by_id.update((line["custom_id"], line) for line in (lines if lines is not None else read_jsonl(batch_path)))
        return by_id

    if state is None:
        settled = {r["custom_id"] for r in read_kept_lines(kept_path)}
        to_send = remaining_lines(lines, settled)
        if settled:
            print(f"{len(settled)} lines were settled in earlier batch jobs of this job (answered, failed for good or held) and "
                  f"are kept; sending the other {len(to_send)}.")
        if to_send:
            # The automatic rounds count from here: lines sent again after a person's fix get every round again.
            run["batch_rounds"] = int(run.get("batch_rounds") or 0) + 1
            state = {"base_url": a.base_url, "subscription": a.subscription, "round": run["batch_rounds"],
                     "first_round": run["batch_rounds"], "jobs": round_jobs(to_send)}
            write_json(state_path, state)
    client, deleted, blocked, waiting = None, 0, [], []
    while state:
        client = client or batchlib.make_client(state["base_url"], state.get("subscription"))
        if state.get("delete"):  # the files of the round before, if a run stopped before deleting them
            deleted += len(batchlib.delete_files(client, state.pop("delete")))
            write_json(state_path, state)
        for job in state["jobs"]:
            if job.get("custom_ids") is None:
                kept_ids = {r["custom_id"] for r in read_kept_lines(kept_path)}
                job["custom_ids"] = [c for c in line_map() if c not in kept_ids]
        rnd, jobs = state["round"], state["jobs"]
        rounds_sent = rnd - int(state.get("first_round") or rnd) + 1  # rounds this run's lines have been sent in, this one too
        if len(jobs) > 1:
            print(f"Batch round {rnd}: {sum(len(j['custom_ids']) for j in jobs)} lines in {len(jobs)} batch jobs (a file holds at "
                  f"most {batchlib.MAX_LINES:,} lines and {batchlib.PART_BYTES:,} bytes here)")
        submit_round(client, run, run_path, wd, state, state_path, line_map, budget)
        run["status"] = "batch running"
        write_json(run_path, run)
        ended = wait_round(client, a, run, run_path, state, state_path, deadline)
        refused = [job for job in jobs if batchlib.failed_for_quota(ended[job["batch_id"]])]
        if refused:
            # Outside the regions that refuse the create at once, a job created while the enqueued-token quota is full
            # validates, then fails with token_limit_exceeded. That's a wait too, not a round: the job is created again
            # from the same file, after the budget's pause.
            for job in refused:
                b = ended[job["batch_id"]]
                run.setdefault("batches", []).append({"round": rnd, "part": job["part"], "lines": len(job["custom_ids"]), **batchlib.batch_timing(b)})
                batchlib.delete_files(client, [b.output_file_id, b.error_file_id])
                job.setdefault("ended_jobs", []).append(b.id)
                for key in ("batch_id", "submitted_at", "create", "create_sent_at"):
                    job.pop(key, None)
            write_json(state_path, state)
            write_json(run_path, run)
            try:
                budget.wait(f"{len(refused)} batch job(s) failed because the enqueued-token quota was full (token_limit_exceeded)")
            except batchlib.QuotaWaitOver as ex:
                run["status"] = "waiting for quota"
                write_json(run_path, run)
                raise Exit(exits.RERUN, f"Batch round {rnd}: {ex}. The uploaded files are kept: re-run the same command to "
                           "create the jobs again.") from None
            continue
        out: dict[str, list[dict]] = {"settled": [], "retry": [], "held": [], "blocked": []}
        files: list[str | None] = []
        for job in jobs:
            b = ended[job["batch_id"]]
            for k, v in batchlib.sort_lines(job["custom_ids"], batchlib.collect(client, b), b, bool(job.get("cancelled_by_run"))).items():
                out[k] += v
            run.setdefault("batches", []).append({"round": rnd, "part": job["part"], "lines": len(job["custom_ids"]), **batchlib.batch_timing(b)})
            files += [job.get("input_file_id"), b.output_file_id, b.error_file_id]
        cancelled, retry, waiting, nxt = out["held"], out["retry"], [], None
        blocked += out["blocked"]  # not sent again in this run: a person fixes the input, then re-runs the same command
        if not auto:  # with --no-auto-retry a person decides: the re-run sends them again, whoever cancelled the job
            retry, cancelled = retry + cancelled, []
        settled = out["settled"] + [held_row(r, "the batch job was cancelled outside this run, so this line isn't sent again "
                                            "automatically") for r in cancelled]
        if retry and auto and rounds_sent < max_rounds:
            nxt = round_jobs([line_map()[r["custom_id"]] for r in retry])
        elif retry and auto:
            settled += [held_row(r, f"still not answered after {rounds_sent} rounds") for r in retry]
        else:
            waiting = retry
        print(f"Batch round {rnd} ended ({', '.join(f'{n} {s}' for s, n in Counter(e.status for e in ended.values()).items())}): "
              f"{len(out['settled'])} lines answered or failed for good"
              + (f", {len(retry)} to send again" + (f" in round {rnd + 1}" if nxt else "") if retry else "")
              + (f", {len(cancelled)} held (cancelled outside this run)" if cancelled else "")
              + (f", {len(out['blocked'])} in a job that failed validation" if out["blocked"] else ""))
        # Settled lines are saved before any file is deleted: answered lines are billed, and a re-run must never lose them.
        keep_lines(kept_path, settled)
        if nxt:
            run["batch_rounds"] = rnd + 1
            state = {"base_url": state["base_url"], "subscription": state.get("subscription"), "round": rnd + 1,
                     "first_round": int(state.get("first_round") or rnd), "jobs": nxt, "delete": [f for f in files if f]}
            write_json(state_path, state)
            write_json(run_path, run)
        else:
            state = None
            state_path.unlink()
            deleted += len(batchlib.delete_files(client, files))
    two_stage = " without searching again" if a.design == "two-stage" else ""
    if blocked:
        run["status"] = "batch failed"
        write_json(run_path, run)
        raise Exit(exits.NEEDS_PERSON, f"A batch job failed validation, so {len(blocked)} lines weren't processed ({blocked[0]['error'][:300]}). "
                   f"Fix the cause and re-run the same command: the lines already settled are kept, and only the others are sent "
                   f"again{two_stage}.")
    if waiting:
        run["status"] = "batch round ended"
        write_json(run_path, run)
        statuses = ", ".join(sorted({b["status"] for b in run["batches"] if b.get("round") == rnd}))
        raise Exit(exits.RERUN, f"The batch round ended ('{statuses}') with {len(waiting)} lines that failed with HTTP 408, 429 or 5xx "
                   f"or weren't processed; with --no-auto-retry they wait for you. Re-run the same command to send only those "
                   f"again{two_stage}; the other lines are kept in {wd}.")
    rows = read_kept_lines(kept_path) + list(held.values())
    results = write_outputs(wd, workload, records, rows, run, len(held))
    if run.get("batches"):
        run["batch"] = batch_summary(run["batches"])
    run["actual"] = actual(rows, records=len(records), prices=prices, price_model=a.price_model, webiq_calls=webiq_calls)
    # state.json holds the endpoint and job IDs; run.json (kept) has everything else about the job.
    removed = remove_working_files(a, [wd / "evidence.jsonl", batch_path, kept_path, state_path, wd / BATCH_PART])
    run.update(status="done", finished_at=now(), deleted={"service_files": deleted, "local_files": removed})
    write_json(run_path, run)
    f = run.get("forecast_before_submit", {}).get("usd_per_1000_records")
    per = run["actual"]["usd_per_1000_records"]
    jobs_done = len(run.get("batches") or [])
    print(
        f"Done: {len(results)} results in {wd / 'results.jsonl'}; {jobs_done} batch job(s) in {run.get('batch_rounds') or 0} round(s)"
        + (f"; actual ${per} per 1,000 records" if per is not None else "")
        + (f" (forecast ${f})" if f is not None else "")
        + held_note(run)
        + f". Deleted {deleted} service files and {removed or 'no'} local working files."
    )
    return exits.HELD if run["held"]["records"] else exits.DONE


def run_flex(a, run: dict, run_path: pathlib.Path, wd: pathlib.Path, workload, records: list[dict], lines: list[dict], held: dict,
             prices: dict, webiq_calls: int, working_files: list[pathlib.Path]) -> int:
    """Send the lines with Flex processing (flex.py), then write metrics.jsonl, results.jsonl, held-records.jsonl and
    run.json as the batch path does, priced at the model's flex rates.

    The attempt ledger (flex-attempts.jsonl) is the job's state: a re-run of the same command resumes from it. The job
    is done when no line is left to send: each has a Flex response, or an error that retrying can't fix. Lines held
    after the automatic rounds end the run with exit code 3, and the working files are kept, so --retry-failed or
    --retry-ambiguous can send them again; lines still waiting end it with exit code 75. The results so far are written.
    """
    run["status"] = "flex in progress"
    write_json(run_path, run)
    started, t0 = now(), time.time()
    out = flex_executor().run_lines(lines, wd, deployment=a.deployment, base_url=a.base_url, subscription=a.subscription,
                                    concurrency=getattr(a, "flex_concurrency", None), retry_failed=getattr(a, "retry_failed", False),
                                    retry_ambiguous=getattr(a, "retry_ambiguous", False),
                                    auto_rounds=1 if getattr(a, "no_auto_retry", False) else None)
    s, rows = out["summary"], out["rows"] + list(held.values())
    run.setdefault("flex_sessions", []).append({"started_at": started, "seconds": round(time.time() - t0, 1),
                                                "requests": s["attempts"]["requests_this_run"]})
    run["flex"] = s
    results = write_outputs(wd, workload, records, rows, run, len(held))
    run["actual"] = actual(rows, records=len(records), prices=prices, price_model=a.price_model, webiq_calls=webiq_calls, tier="flex")
    n = s["lines"]
    so_far = f"The results so far are in {wd / 'results.jsonl'}"
    if out["stopped"]:
        run["status"] = "flex stopped"
        write_json(run_path, run)
        raise Exit(exits.INTERRUPTED if out.get("interrupted") else exits.NEEDS_PERSON,
                   f"{out['stopped']} Every request sent so far is recorded in {wd / FLEX_LEDGER}; re-run the same command to resume "
                   "(completed lines aren't sent again).")
    if n["not_finished"]:
        run["status"] = "flex incomplete"
        write_json(run_path, run)
        raise Exit(exits.RERUN, f"Flex job not finished: {n['completed']} of {n['total']} lines completed; {n['not_finished']} weren't "
                   "finished, for example because the service asked to wait longer than the automatic rounds allow. Re-run the "
                   f"same command later to continue. {so_far}; the working files are kept for the re-run.")
    if n["used_up"] or n["held_in_flight"]:
        run["status"] = "flex incomplete"
        write_json(run_path, run)
        left = [
            f"{n['used_up']} used up their attempts in every round (--retry-failed sends them again)" if n["used_up"] else "",
            f"{n['held_in_flight']} had a request whose outcome is unknown, for example it timed out (--retry-ambiguous sends "
            "them again; they may have been billed)" if n["held_in_flight"] else "",
        ]
        print(f"Flex job finished with held lines: {n['completed']} of {n['total']} lines completed; "
              + "; ".join(x for x in left if x) + held_note(run)
              + f". {so_far}; the working files are kept for --retry-failed or --retry-ambiguous.")
        return exits.HELD
    removed = remove_working_files(a, working_files)
    run.update(status="done", finished_at=now(), deleted={"service_files": 0, "local_files": removed})
    write_json(run_path, run)
    f = run.get("forecast_before_submit", {}).get("usd_per_1000_records")
    failed = f"; {n['failed']} lines failed with errors retrying can't fix (see results.jsonl)" if n["failed"] else ""
    per = run["actual"]["usd_per_1000_records"]
    print(
        f"Done: {len(results)} results in {wd / 'results.jsonl'}; Flex: {s['attempts']['requests']} requests, "
        f"{s['attempts']['not_billed_429']} HTTP 429s{failed}; actual ${per} per 1,000 records"
        + (f" (forecast ${f})" if f is not None else "")
        + held_note(run)
        + f". Deleted {removed or 'no'} local working files; kept the attempt ledger {FLEX_LEDGER}."
    )
    return exits.HELD if run["held"]["records"] else exits.DONE
