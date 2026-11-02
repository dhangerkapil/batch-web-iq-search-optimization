import json
import pathlib

import pytest

from search_plan import build_plan, evidence_by_record, query_id, summarize_plan
from util import read_jsonl
from workloads import Search, Workload, get_workload, name_contains, name_tokens

SAMPLE = pathlib.Path(__file__).resolve().parents[1] / "samples" / "brand-owner"


@pytest.fixture(scope="module")
def records():
    return read_jsonl(SAMPLE / "records.jsonl")


@pytest.fixture(scope="module")
def workload():
    return get_workload("brand-owner")


def test_name_tokens_drop_accents_apostrophes_and_legal_words():
    assert name_tokens("Nestlé S.A.") == {"nestle"}
    assert name_tokens("The Coca-Cola Company") == {"coca", "cola"}
    assert name_tokens("Lay's") == {"lays"}
    assert name_tokens("PepsiCo, Inc.") == {"pepsico"}


def test_name_contains():
    assert name_contains("Coca-Cola Zero Sugar", "The Coca-Cola Company")
    assert name_contains("Nestlé Pure Life", "Nestlé S.A.")
    assert not name_contains("Lay's", "PepsiCo, Inc.")
    assert not name_contains("Anything", "Inc.")  # an owner made only of legal words never matches


def test_lean_and_full_plans_on_the_sample(records, workload):
    lean = summarize_plan(build_plan(records, workload, "lean"), len(records))
    full = summarize_plan(build_plan(records, workload, "full"), len(records))
    assert (lean["searches"], full["searches"]) == (26, 29)
    assert lean["by_purpose"] == {"brand_owner": 20, "owner_hq": 6}
    assert full["searches_without_sharing"] == 46
    assert full["saved_by_sharing"] == 17


def test_owner_search_is_shared_and_has_no_region(records, workload):
    plan = build_plan(records, workload, "full")
    owner = [e for e in plan.values() if e["query"] == "PepsiCo, Inc. headquarters"]
    assert len(owner) == 1
    assert owner[0]["region"] is None
    assert owner[0]["used_by"] == ["R01", "R02", "R03", "R04", "R05"]


def test_lean_skips_brand_search_only_when_name_contains_owner(records, workload):
    plan = build_plan(records, workload, "lean")
    skipped = {r["id"] for r in records} - {rid for e in plan.values() if e["purpose"] == "brand_owner" for rid in e["used_by"]}
    assert skipped == {"R10", "R20", "R21"}


def test_query_id_ignores_case_and_spacing_but_not_region():
    assert query_id("Tide  brand owner", "US") == query_id("tide brand owner", "US")
    assert query_id("Tide brand owner", "US") != query_id("Tide brand owner", "GB")


def test_duplicate_ids_are_rejected(workload):
    rec = {"id": "X", "brand": "Tide", "market": "US", "claimed_owner": "P&G", "claimed_owner_hq_country": "US"}
    with pytest.raises(SystemExit):
        build_plan([rec, dict(rec)], workload, "full")


def test_overlong_query_is_rejected():
    class Long(Workload):
        def searches(self, rec, plan):
            return [Search("x" * 1001, "p")]

    with pytest.raises(SystemExit):
        build_plan([{"id": "1"}], Long(), "full")


def test_problems_flag_bad_country_codes(workload):
    rec = {"id": "X", "brand": "Tide", "market": "USA", "claimed_owner": "P&G", "claimed_owner_hq_country": ""}
    assert workload.problems(rec) == ["missing claimed_owner_hq_country", "market 'USA' is not an ISO 3166-1 alpha-2 code"]


def test_evidence_by_record(records, workload):
    plan = build_plan(records, workload, "lean")
    by = evidence_by_record(list(plan.values()))
    assert [e["purpose"] for e in by["R20"]] == ["owner_hq"]
    assert sorted(e["purpose"] for e in by["R01"]) == ["brand_owner", "owner_hq"]


def test_labels_cover_every_record(records):
    labels = read_jsonl(SAMPLE / "labels.jsonl")
    assert [r["id"] for r in records] == [label["id"] for label in labels]
    for label in labels:
        assert label["all_verified"] == (label["brand_owner"] == label["owner_hq"] == "verified"), json.dumps(label)
