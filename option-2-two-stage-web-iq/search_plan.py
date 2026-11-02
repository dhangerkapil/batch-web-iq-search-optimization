"""Search plan: the unique web searches a job needs, built from record fields and de-duplicated across the job."""
from __future__ import annotations

import hashlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "shared"))  # the code every option shares

from settings import SETTINGS
from workloads import Workload

MAX_QUERY_CHARS = int(SETTINGS["webiq"]["max_query_chars"])  # Web IQ rejects longer queries


def query_id(query: str, region: str | None) -> str:
    q = " ".join(query.split()).lower()
    return hashlib.sha1(f"{q}|{region or ''}".encode()).hexdigest()[:16]


def build_plan(records: list[dict], workload: Workload, plan: str) -> dict[str, dict]:
    """Return {query_id: {query_id, query, region, purpose, used_by: [record ids]}}, in first-seen order."""
    out: dict[str, dict] = {}
    seen_ids: set[str] = set()
    for rec in records:
        rid = workload.record_id(rec)
        if rid in seen_ids:
            raise SystemExit(f"Duplicate record id '{rid}': ids become batch custom_id values and must be unique.")
        seen_ids.add(rid)
        for s in workload.searches(rec, plan):
            q = " ".join(s.query.split())
            if not q or len(q) > MAX_QUERY_CHARS:
                raise SystemExit(f"{rid}: query must be 1-{MAX_QUERY_CHARS} characters: {q[:80]!r}")
            qid = query_id(q, s.region)
            e = out.setdefault(qid, {"query_id": qid, "query": q, "region": s.region, "purpose": s.purpose, "used_by": []})
            if rid not in e["used_by"]:
                e["used_by"].append(rid)
    return out


def summarize_plan(plan: dict[str, dict], records: int) -> dict:
    by_purpose: dict[str, int] = {}
    for e in plan.values():
        by_purpose[e["purpose"]] = by_purpose.get(e["purpose"], 0) + 1
    uses = sum(len(e["used_by"]) for e in plan.values())
    return {
        "records": records,
        "searches": len(plan),
        "searches_per_record": round(len(plan) / max(1, records), 3),
        "by_purpose": by_purpose,
        "searches_without_sharing": uses,
        "saved_by_sharing": uses - len(plan),
        "shared_searches": sum(1 for e in plan.values() if len(e["used_by"]) > 1),
    }


def evidence_by_record(entries: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in entries:
        for rid in e.get("used_by") or []:
            out.setdefault(rid, []).append(e)
    return out
