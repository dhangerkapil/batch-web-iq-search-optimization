import json

from batch_lines import line_text
from two_stage_lines import EVIDENCE_CLOSE, EVIDENCE_OPEN, format_evidence, two_stage_line
from web_search_lines import web_search_line
from workloads import get_workload

REC = {"id": "R01", "brand": "Gatorade", "market": "US", "claimed_owner": "PepsiCo, Inc.", "claimed_owner_hq_country": "US"}


def entry(purpose, query, region, results):
    return {"query_id": query, "query": query, "region": region, "purpose": purpose, "used_by": ["R01"], "results": results}


BRAND = entry(
    "brand_owner",
    "Gatorade brand owner",
    "US",
    [{"title": "Gatorade", "url": "https://example.com/a", "content": "brand page"}, {"title": "Again", "url": "https://example.com/b", "content": "dup"}],
)
OWNER = entry("owner_hq", "PepsiCo, Inc. headquarters", None, [{"title": "HQ", "url": "https://example.com/b", "content": "y" * 2000}])


def test_two_stage_line_has_no_tools_and_a_strict_schema():
    w = get_workload("brand-owner")
    line = two_stage_line(w, REC, [BRAND, OWNER], deployment="dep")
    body = line["body"]
    assert line["custom_id"] == "R01" and line["url"] == "/v1/responses"
    assert "tools" not in body
    assert body["store"] is False
    assert body["text"]["format"]["strict"] is True
    assert body["reasoning"] == {"effort": "low"}
    user = body["input"][1]["content"]
    assert user.startswith(w.rules())  # identical static prefix in every line
    assert EVIDENCE_OPEN in user and EVIDENCE_CLOSE in user
    assert user.rstrip().endswith(json.dumps(REC, ensure_ascii=False, separators=(",", ":")))


def test_shared_evidence_comes_first_and_repeated_urls_are_shown_once():
    w = get_workload("brand-owner")
    text = format_evidence([BRAND, OWNER], w.evidence_order, 1200)
    assert text.index("PepsiCo, Inc. headquarters") < text.index("Gatorade brand owner")
    assert "same page as above: https://example.com/b" in text
    assert "y" * 1200 in text and "y" * 1201 not in text


def test_unbounded_web_search_line_has_no_caps_or_schema():
    w = get_workload("brand-owner")
    body = web_search_line(w, REC, deployment="dep", bounded=False)["body"]
    assert body["tools"] == [{"type": "web_search"}]
    assert "max_tool_calls" not in body and "text" not in body and "reasoning" not in body


def test_bounded_web_search_line_caps_calls_and_sets_location():
    w = get_workload("brand-owner")
    body = web_search_line(w, REC, deployment="dep", bounded=True, max_tool_calls=2)["body"]
    assert body["tools"] == [{"type": "web_search", "user_location": {"type": "approximate", "country": "US"}}]
    assert body["max_tool_calls"] == 2
    assert body["text"]["format"]["strict"] is True


def test_line_text_joins_system_and_user():
    w = get_workload("brand-owner")
    line = two_stage_line(w, REC, [OWNER], deployment="dep")
    assert line_text(line).startswith(w.system_prompt())
