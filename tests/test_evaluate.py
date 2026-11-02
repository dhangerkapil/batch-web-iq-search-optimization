import json
import pathlib

from evaluate import score_run, to_markdown
from workloads import get_workload


def write(p: pathlib.Path, rows):
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def fact(status):
    return {"status": status, "finding": "", "suggested_value": None, "citations": []}


def test_score_run_counts_right_wrong_invalid_and_missing(tmp_path):
    labels = {
        "A": {"id": "A", "brand_owner": "verified", "owner_hq": "verified"},
        "B": {"id": "B", "brand_owner": "contradicted", "owner_hq": "verified"},
        "C": {"id": "C", "brand_owner": "verified", "owner_hq": "verified"},
    }
    write(
        tmp_path / "results.jsonl",
        [
            {"id": "A", "result": {"brand_owner": fact("Verified"), "owner_hq": fact("verified")}},
            {"id": "B", "result": [{"brand_owner": fact("partial_match"), "owner_hq": fact("verified")}]},
        ],
    )
    (tmp_path / "run.json").write_text(json.dumps({"label": "t", "design": "two-stage", "actual": {"usd_per_1000_records": 9.5}}), encoding="utf-8")
    s = score_run(tmp_path, labels, get_workload("brand-owner"))
    assert s["facts_right"] == 3  # A: both (case-insensitive); B: owner_hq only
    assert s["records_right"] == 1
    assert s["invalid_status_values"] == 1  # "partial_match"
    assert s["missing_or_unparseable"] == 1  # C
    assert "B brand_owner: partial_match (expected contradicted)" in to_markdown([s])
