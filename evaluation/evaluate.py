"""Compare run results with labeled answers: per-fact accuracy, record accuracy, invalid values and cost.

Example, from this folder:
  python evaluate.py ../option-2-two-stage-web-iq/runs/lean --labels ../samples/brand-owner/labels.jsonl
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

from settings import SETTINGS
from util import read_json, read_jsonl
from workloads import STATUSES, get_workload


def score_run(run_dir: pathlib.Path, labels: dict[str, dict], workload) -> dict:
    run = read_json(run_dir / "run.json") if (run_dir / "run.json").exists() else {}
    results = {r["id"]: r for r in read_jsonl(run_dir / "results.jsonl")}
    facts_total = facts_right = records_right = invalid = missing = 0
    wrong: dict[str, list[str]] = {}
    for rid, label in labels.items():
        r = results.get(rid)
        got = workload.fact_statuses(r.get("result")) if r and r.get("result") is not None else {f: None for f in workload.facts}
        if not r or r.get("result") is None:
            missing += 1
        record_ok = True
        for f in workload.facts:
            facts_total += 1
            if got[f] is not None and got[f] not in STATUSES:
                invalid += 1
            if got[f] == label[f]:
                facts_right += 1
            else:
                record_ok = False
                wrong.setdefault(rid, []).append(f"{f}: {got[f]} (expected {label[f]})")
        records_right += record_ok
    actual = run.get("actual") or {}
    return {
        "run": run.get("label") or run_dir.name,
        "design": run.get("design"),
        "plan": run.get("plan"),
        "model": run.get("price_model"),
        "records": len(labels),
        "facts_right": facts_right,
        "facts_total": facts_total,
        "records_right": records_right,
        "invalid_status_values": invalid,
        "missing_or_unparseable": missing,
        "usd_per_1000_records": actual.get("usd_per_1000_records"),
        "search_share_of_cost": actual.get("search_share_of_cost"),
        "per_record_mean": actual.get("per_record_mean"),
        "batch_minutes": (run.get("batch") or {}).get("minutes_end_to_end"),
        "forecast_usd_per_1000": (run.get("forecast_before_submit") or {}).get("usd_per_1000_records"),
        "wrong": wrong,
    }


def to_markdown(scores: list[dict]) -> str:
    out = [
        "| Run | Design | Plan | Model | $ per 1,000 records | Search share of cost | Web search requests / record | Web IQ calls / record | Input tokens / record (cached) | Output tokens / record | Facts right | Records right | Invalid status values | Missing or unparseable |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for s in scores:
        m = s.get("per_record_mean") or {}
        share = s.get("search_share_of_cost")
        out.append(
            f"| {s['run']} | {s['design']} | {s['plan'] or '-'} | {s['model']} | {s['usd_per_1000_records']} | "
            f"{'-' if share is None else f'{share:.0%}'} | {m.get('web_search_requests', '-')} | {m.get('webiq_calls', '-')} | "
            f"{m.get('input_tokens', '-')} ({m.get('cached_tokens', '-')}) | {m.get('output_tokens', '-')} | "
            f"{s['facts_right']}/{s['facts_total']} | {s['records_right']}/{s['records']} | {s['invalid_status_values']} | {s['missing_or_unparseable']} |"
        )
    out.append("")
    out.append("Wrong answers by run:")
    out.append("")
    for s in scores:
        if not s["wrong"]:
            out.append(f"- **{s['run']}**: none")
            continue
        detail = "; ".join(f"{rid} {', '.join(v)}" for rid, v in sorted(s["wrong"].items()))
        out.append(f"- **{s['run']}**: {detail}")
    return "\n".join(out) + "\n"


def cmd_evaluate(a) -> None:
    workload = get_workload(a.workload)
    labels = {r["id"]: r for r in read_jsonl(a.labels)}
    scores = [score_run(pathlib.Path(d), labels, workload) for d in a.runs]
    md = to_markdown(scores)
    print(md)
    if a.out:
        pathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        pathlib.Path(a.out).write_text(md, encoding="utf-8")
        print(f"Wrote {a.out}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python evaluate.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="run folders (each with run.json and results.jsonl)")
    ap.add_argument("--labels", required=True, help="labeled answers, JSONL (samples/brand-owner/labels.jsonl for the sample)")
    ap.add_argument("--workload", default=SETTINGS["defaults"]["workload"])
    ap.add_argument("--out", help="also write the table to this Markdown file")
    cmd_evaluate(ap.parse_args())


if __name__ == "__main__":
    main()
