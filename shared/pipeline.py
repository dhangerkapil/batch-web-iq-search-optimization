"""The job runner every option uses: the `plan`, `search`, `build` and `run` commands, for any design, on Global Batch or Flex.

Each design's own code is loaded from its folder when a command needs it: option-1-bounded-web-search (lines in which
the model searches), option-2-two-stage-web-iq (the search plan, Web IQ and the no-tools lines) and
flex-web-search-and-web-iq (Flex processing). Each folder's entry script fixes the design or the executor.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import time
from typing import Iterable

import batch as batchlib
import folders
from batch_lines import DESIGNS, line_text
from cost import actual, estimate_before_search, exact_before_submit, print_forecast
from prices import load_prices, model_rates, source_line
from util import count_tokens, now, read_json, read_jsonl, script, write_json, write_jsonl
from workloads import get_workload

TOOL_VERSION = "0.2.0"  # recorded in run.json
FLEX_LEDGER = "flex-attempts.jsonl"  # flex.py's LEDGER: a work folder that has one holds a Flex job


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
    """Completed lines (per-line metrics) kept from earlier batches of this job that ended before completing."""
    return read_jsonl(path) if path.exists() else []


def remaining_lines(lines: list[dict], done_ids: set[str]) -> list[dict]:
    """Batch lines whose custom_id hasn't completed in an earlier batch of the same job."""
    return [line for line in lines if line["custom_id"] not in done_ids]


def is_flex(a) -> bool:
    return getattr(a, "executor", None) == "flex"


def forecast_title(text: str, a) -> str:
    """Flex forecasts say so; Global Batch forecasts keep their plain titles."""
    return f"{text} at Flex rates ({a.price_model})" if is_flex(a) else text


def results_rows(rows: list[dict]) -> list[dict]:
    """results.jsonl: one row per line, by custom_id."""
    return [
        {"id": r["custom_id"], "status": r.get("status"), "result": r.get("output"), "error": r.get("error") or (None if r.get("json_valid") else "output is not valid JSON")}
        for r in sorted(rows, key=lambda r: str(r.get("custom_id")))
    ]


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


def cmd_run(a) -> None:
    if a.design not in DESIGNS:
        raise SystemExit(f"Unknown design {a.design}")
    if a.design != "two-stage" and a.max_cost is not None:
        raise SystemExit("--max-cost can't be enforced when the model decides how many searches to run. Remove it to run this design.")
    flex = is_flex(a)
    wd = pathlib.Path(a.workdir)
    wd.mkdir(parents=True, exist_ok=True)
    run_path, state_path = wd / "run.json", wd / "state.json"
    evidence_path, batch_path = wd / "evidence.jsonl", wd / "batch-input.jsonl"
    kept_path = wd / "kept-lines.jsonl"  # completed lines of earlier batches of this job that didn't finish
    run = read_json(run_path) if run_path.exists() else {}
    if run.get("status") == "done":
        print(f"{wd} is already done: ${run['actual']['usd_per_1000_records']} per 1,000 records. Results: {wd / 'results.jsonl'}")
        return
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
        },
    )
    if flex:
        run.update(executor="flex", tier="flex")
    run.setdefault("started_at", now())
    client = None
    webiq_calls = 0

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
                "attempts": sum(s["attempts"] for s in run["stage1_sessions"]),
                "failed_attempts_by_status": _merge_counts(s["failed_attempts_by_status"] for s in run["stage1_sessions"]),
                "seconds": round(sum(s["seconds"] for s in run["stage1_sessions"]), 1),
                "mean_latency_s": round(sum(e.get("latency_s", 0) for e in evidence) / max(1, len(evidence)), 2),
                "results_per_search_mean": round(sum(len(e.get("results") or []) for e in evidence) / max(1, len(evidence)), 2),
                "searches_with_no_results": sum(1 for e in evidence if not e.get("error") and not e.get("results")),
                "premium_results": sum(1 for e in evidence for r in e.get("results") or [] if r.get("contentTier") == "premium"),
            }
            write_json(run_path, run)
            if errors:
                for e in errors[:10]:
                    print(f"  ERROR {e['query']}: {e['error']}")
                raise SystemExit(f"{len(errors)} searches failed; nothing was submitted. Re-run the same command to retry only those.")
            lines = build_lines(workload, records, a.design, a, evidence, plan)
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
            run_flex(a, run, run_path, wd, lines, len(records), prices, webiq_calls, [evidence_path, batch_path])
            return
        done_ids = {r["custom_id"] for r in read_kept_lines(kept_path)}
        to_submit = remaining_lines(lines, done_ids)
        if done_ids:
            print(f"{len(done_ids)} lines completed in an earlier batch of this job and are kept; submitting the other {len(to_submit)}.")
            write_jsonl(batch_path, to_submit)
        if to_submit:
            client = batchlib.make_client(a.base_url, a.subscription)
            ids = batchlib.submit(client, batch_path, to_submit, run["label"])
            write_json(state_path, {"base_url": a.base_url, "subscription": a.subscription, **ids})
        write_json(run_path, run)

    kept = read_kept_lines(kept_path)
    deleted: list[str] = []
    batch_status = "completed (in earlier batches)"
    if state_path.exists():
        state = read_json(state_path)
        client = client or batchlib.make_client(state["base_url"], state.get("subscription"))
        b = batchlib.wait(client, state["batch_id"], a.poll_seconds, a.stall_minutes, a.cancel_on_stall)
        rows = batchlib.collect(client, b, wd / "metrics.jsonl")
        deleted = batchlib.delete_files(client, [state.get("input_file_id"), b.output_file_id, b.error_file_id])
        if b.status != "completed":
            # Completed lines of a failed, expired or cancelled job are billed: keep them, and keep the evidence,
            # so a re-run resubmits only the other lines and doesn't search again.
            new = [r for r in rows if r.get("status") not in (None, "error")]
            write_jsonl(kept_path, kept + new)
            run.setdefault("incomplete_batches", []).append({"batch": batchlib.batch_timing(b), "lines_kept": len(new)})
            run["status"] = f"batch {b.status}"
            state_path.unlink()
            write_json(run_path, run)
            raise SystemExit(
                f"The batch ended '{b.status}'. Kept {len(new)} completed lines ({len(kept) + len(new)} so far); the evidence and batch input "
                f"are kept in {wd}. Re-run the same command to resubmit only the remaining lines, without searching again."
            )
        run["batch"] = batchlib.batch_timing(b)
        batch_status = b.status
        rows = kept + rows
    else:
        rows = kept
    write_jsonl(wd / "metrics.jsonl", rows)
    results = results_rows(rows)
    write_jsonl(wd / "results.jsonl", results)
    run["actual"] = actual(rows, records=len(records), prices=prices, price_model=a.price_model, webiq_calls=webiq_calls)
    removed = []
    if not a.keep_working_files:
        # state.json holds the endpoint and job IDs; run.json (kept) has everything else about the job.
        for p in (evidence_path, batch_path, kept_path, state_path):
            if p.exists():
                p.unlink()
                removed.append(p.name)
    run.update(status="done", finished_at=now(), deleted={"service_files": len(deleted), "local_files": removed})
    write_json(run_path, run)
    f = run.get("forecast_before_submit", {}).get("usd_per_1000_records")
    print(
        f"Done: {len(results)} results in {wd / 'results.jsonl'}; batch {batch_status}; actual ${run['actual']['usd_per_1000_records']} per 1,000 records"
        + (f" (forecast ${f})" if f is not None else "")
        + f". Deleted {len(deleted)} service files and {removed or 'no'} local working files."
    )


def run_flex(a, run: dict, run_path: pathlib.Path, wd: pathlib.Path, lines: list[dict], records: int, prices: dict,
             webiq_calls: int, working_files: list[pathlib.Path]) -> None:
    """Send the lines with Flex processing (flex.py), then write metrics.jsonl, results.jsonl and run.json as the batch
    path does, priced at the model's flex rates.

    The attempt ledger (flex-attempts.jsonl) is the job's state: a re-run of the same command resumes from it. The job
    is done when no line is left to send: each has a Flex response, or an error that retrying can't fix. Until then the
    working files are kept, and the results so far are written.
    """
    run["status"] = "flex in progress"
    write_json(run_path, run)
    started, t0 = now(), time.time()
    out = flex_executor().run_lines(lines, wd, deployment=a.deployment, base_url=a.base_url, subscription=a.subscription,
                            concurrency=getattr(a, "flex_concurrency", None), retry_failed=getattr(a, "retry_failed", False),
                            retry_ambiguous=getattr(a, "retry_ambiguous", False))
    s, rows = out["summary"], out["rows"]
    run.setdefault("flex_sessions", []).append({"started_at": started, "seconds": round(time.time() - t0, 1),
                                                "requests": s["attempts"]["requests_this_run"]})
    run["flex"] = s
    write_jsonl(wd / "metrics.jsonl", rows)
    results = results_rows(rows)
    write_jsonl(wd / "results.jsonl", results)
    run["actual"] = actual(rows, records=records, prices=prices, price_model=a.price_model, webiq_calls=webiq_calls, tier="flex")
    n = s["lines"]
    if out["stopped"]:
        run["status"] = "flex stopped"
        write_json(run_path, run)
        raise SystemExit(f"{out['stopped']} Every request sent so far is recorded in {wd / FLEX_LEDGER}; re-run the same "
                         "command to resume (completed lines aren't sent again).")
    if n["used_up"] or n["held_in_flight"] or n["not_finished"]:
        run["status"] = "flex incomplete"
        write_json(run_path, run)
        left = [
            f"{n['used_up']} used up their attempts (add --retry-failed to send them again)" if n["used_up"] else "",
            f"{n['held_in_flight']} had a request whose outcome is unknown, for example in flight when a run stopped (add "
            "--retry-ambiguous to send them again; they may have been billed)" if n["held_in_flight"] else "",
            f"{n['not_finished']} weren't finished, for example because the service asked to wait longer than "
            "max_server_wait_seconds (re-run the same command later)" if n["not_finished"] else "",
        ]
        raise SystemExit(f"Flex job not finished: {n['completed']} of {n['total']} lines completed; "
                         + "; ".join(x for x in left if x)
                         + f". The results so far are in {wd / 'results.jsonl'}; the working files are kept for the re-run.")
    removed = []
    if not a.keep_working_files:
        for p in working_files:
            if p.exists():
                p.unlink()
                removed.append(p.name)
    run.update(status="done", finished_at=now(), deleted={"service_files": 0, "local_files": removed})
    write_json(run_path, run)
    f = run.get("forecast_before_submit", {}).get("usd_per_1000_records")
    failed = f"; {n['failed']} lines failed with errors retrying can't fix (see results.jsonl)" if n["failed"] else ""
    print(
        f"Done: {len(results)} results in {wd / 'results.jsonl'}; Flex: {s['attempts']['requests']} requests, "
        f"{s['attempts']['not_billed_429']} HTTP 429s{failed}; actual ${run['actual']['usd_per_1000_records']} per 1,000 records"
        + (f" (forecast ${f})" if f is not None else "")
        + f". Deleted {removed or 'no'} local working files; kept the attempt ledger {FLEX_LEDGER}."
    )
