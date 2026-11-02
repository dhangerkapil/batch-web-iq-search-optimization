"""Copy a finished run's shareable files into a results folder.

Refuses files that hold anything environment-specific (endpoints, local paths, job IDs). Removes citation URLs that
point to pages about individuals (profile, social media and company-officer pages), and can remove the free text
and citations of chosen records altogether, for example records about entities that don't exist, whose evidence is
about unrelated real businesses and people. Statuses and suggested values are kept, so evaluate.py still works.

Example, from this folder:
  python report.py ../option-2-two-stage-web-iq/runs/lean --out ../results/my-run --redact-records R22,R23
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re

SHAREABLE = ("run.json", "results.jsonl", "metrics.jsonl", "prices-used.json")
NEVER_COPY = ("state.json", "evidence.jsonl", "batch-input.jsonl", "batch-input-part.jsonl", "kept-lines.jsonl", "flex-attempts.jsonl")
# Endpoints, local paths and resource IDs that shouldn't leave your environment. Path patterns can't match inside URLs.
PATTERNS = [
    r"https://[a-z0-9-]+\.openai\.azure\.com",
    r"https://[a-z0-9-]+\.services\.ai\.azure\.com",
    r"https://[a-z0-9-]+\.cognitiveservices\.azure\.com",
    r"/subscriptions/[0-9a-f-]{36}",
    r"(?<![\w./-])[A-Za-z]:\\\\",
    r"(?<![\w./-])/(home|Users)/[A-Za-z]",
    r"\"(batch_id|input_file_id|output_file_id|error_file_id)\"",
]
PERSONAL_URL = re.compile(
    r"linkedin\.com/in/|/officers?\b|/people/|facebook\.com/|instagram\.com/|tiktok\.com/@|(?://|\.)(x|twitter)\.com/", re.I
)
TEXT_KEYS = ("finding", "summary", "issues_summary", "problem")
REMOVED = "[removed before publishing]"


def check_text(text: str) -> list[str]:
    return [p for p in PATTERNS if re.search(p, text)]


def _redact(obj, remove_text: bool, counts: dict):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k.endswith("citations") and isinstance(v, list):
                if remove_text:
                    counts["citations_removed"] += len(v)
                    out[k] = []
                    continue
                kept = [u for u in v if not (isinstance(u, str) and PERSONAL_URL.search(u))]
                counts["personal_urls_removed"] += len(v) - len(kept)
                out[k] = kept
            elif remove_text and k in TEXT_KEYS and isinstance(v, str):
                out[k] = REMOVED
            else:
                out[k] = _redact(v, remove_text, counts)
        return out
    if isinstance(obj, list):
        return [_redact(x, remove_text, counts) for x in obj]
    return obj


def redact_line(row: dict, redact_ids: set[str], counts: dict) -> dict:
    rid = str(row.get("id") or row.get("custom_id"))
    remove_text = rid in redact_ids
    out = _redact(row, remove_text, counts)
    if remove_text:
        counts["records_redacted"].add(rid)
        if out.get("output_text"):
            out["output_text"] = REMOVED
    return out


def cmd_report(a) -> None:
    out = pathlib.Path(a.out)
    redact_ids = {x.strip() for x in (a.redact_records or "").split(",") if x.strip()}
    for d in map(pathlib.Path, a.runs):
        problems = []
        for name in SHAREABLE:
            src = d / name
            if src.exists():
                hits = check_text(src.read_text(encoding="utf-8"))
                if hits:
                    problems.append(f"{src}: matches {hits}")
        if problems:
            raise SystemExit("Not copying; remove environment-specific values first:\n  " + "\n  ".join(problems))
        target = out / d.name
        target.mkdir(parents=True, exist_ok=True)
        counts = {"personal_urls_removed": 0, "citations_removed": 0, "records_redacted": set()}
        copied = []
        for name in SHAREABLE:
            src = d / name
            if not src.exists():
                continue
            if name.endswith(".jsonl"):
                rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
                text = "".join(json.dumps(redact_line(r, redact_ids, counts), ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
                (target / name).write_text(text, encoding="utf-8", newline="\n")
            else:
                (target / name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            copied.append(name)
        print(
            f"{d} -> {target}: {', '.join(copied)}; removed {counts['personal_urls_removed']} personal-page URLs; "
            f"removed the text and {counts['citations_removed']} citations of records {sorted(counts['records_redacted']) or 'none'} "
            f"(never copied: {', '.join(NEVER_COPY)})"
        )


def main() -> None:
    ap = argparse.ArgumentParser(prog="python report.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+", help="run folders (each with run.json and results.jsonl)")
    ap.add_argument("--out", required=True, help="results folder; each run is copied into a subfolder of the same name")
    ap.add_argument("--redact-records", help="comma-separated record ids whose findings and citations are removed (statuses are kept)")
    cmd_report(ap.parse_args())


if __name__ == "__main__":
    main()
