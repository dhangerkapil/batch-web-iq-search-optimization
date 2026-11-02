import argparse

import pytest

from analyze import analyze_input, analyze_output
from batch import check_lines, response_metrics
from cost import actual, forecast
from prices import line_cost, load_prices, model_rates
from report import check_text
from webiq import _rpc_messages, normalize_results, search_args

PRICES = load_prices()

BODY = {
    "status": "completed",
    "usage": {
        "input_tokens": 10_000,
        "input_tokens_details": {"cached_tokens": 4_000},
        "output_tokens": 1_000,
        "output_tokens_details": {"reasoning_tokens": 600},
    },
    "tool_usage": {"web_search": {"num_requests": 3}},
    "output": [
        {"type": "web_search_call", "action": {"type": "search", "queries": ["a", "b"]}},
        {"type": "web_search_call", "action": {"type": "open_page"}},
        {"type": "message", "content": [{"type": "output_text", "text": '{"ok": true}'}]},
    ],
}


def test_response_metrics_uses_num_requests_for_billing():
    m = response_metrics(BODY)
    assert m["web_search_requests"] == 3
    assert m["web_search_calls"] == 2
    assert m["web_search_actions"] == {"search": 1, "open_page": 1}
    assert m["cached_tokens"] == 4_000 and m["json_valid"] and m["output"] == {"ok": True}
    assert m["cache_write_tokens"] == 0 and m["service_tier"] is None  # not reported by this response


def test_line_cost_prices_cached_tokens_and_bing_requests():
    rates = model_rates(PRICES, "gpt-5.4-mini")
    c = line_cost(response_metrics(BODY), rates, PRICES)
    assert c["input"] == pytest.approx(6_000 * 0.375 / 1e6 + 4_000 * 0.0375 / 1e6)
    assert c["output"] == pytest.approx(1_000 * 2.25 / 1e6)
    assert c["web_search"] == pytest.approx(3 * 14 / 1000)


def test_actual_adds_webiq_calls_and_normalizes_per_1000_records():
    rows = [dict(response_metrics(BODY), custom_id="1"), {"custom_id": "2", "status": "error", "error": "x"}]
    a = actual(rows, records=2, prices=PRICES, price_model="gpt-5.4-mini", webiq_calls=4)
    assert a["lines_with_usage"] == 1 and a["lines_failed"] == 1
    assert a["usd"]["webiq"] == pytest.approx(4 * 7 / 1000)
    assert a["usd_per_1000_records"] == pytest.approx(a["usd"]["total"] / 2 * 1000, abs=0.01)


def test_forecast_math():
    f = forecast(prices=PRICES, price_model="gpt-5.4-mini", lines=1000, input_tokens=3_000_000, exact=True, webiq_calls=1_200, output_per_line=500)
    assert f["usd"]["webiq"] == pytest.approx(8.4)
    assert f["usd"]["input"] == pytest.approx(1.125)
    assert f["usd"]["output"] == pytest.approx(1.125)
    assert f["usd_per_1000_records"] == pytest.approx(10.65)


def test_unknown_model_price_fails_clearly():
    with pytest.raises(SystemExit):
        model_rates(PRICES, "no-such-model")


def test_check_lines_catches_mixed_deployments_and_duplicate_ids():
    a = {"custom_id": "1", "url": "/v1/responses", "body": {"model": "a"}}
    b = {"custom_id": "1", "url": "/v1/responses", "body": {"model": "b"}}
    problems = check_lines([a, b])
    assert any("one deployment" in p for p in problems) and any("not unique" in p for p in problems)


def test_search_args_omit_region_when_none():
    opts = argparse.Namespace(max_results=5, language="en", max_chars=1200)
    args = search_args({"query": "q", "region": None}, opts)
    assert "region" not in args and args["contentFormat"] == "passage" and args["maxLength"] == 1200
    assert search_args({"query": "q", "region": "GB"}, opts)["region"] == "GB"


def test_missing_web_results_means_no_results():
    assert normalize_results({}) == []
    assert normalize_results({"webResults": [{"title": "t", "url": "u", "content": "c", "isAdult": False}]}) == [{"title": "t", "url": "u", "content": "c"}]


class FakeResponse:
    def __init__(self, ctype, text):
        self.headers = {"content-type": ctype}
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


def test_mcp_responses_parse_as_json_or_server_sent_events():
    sse = 'event: message\ndata: {"jsonrpc":"2.0","id":5,"result":{"ok":1}}\n\n'
    assert _rpc_messages(FakeResponse("text/event-stream", sse)) == [{"jsonrpc": "2.0", "id": 5, "result": {"ok": 1}}]
    assert _rpc_messages(FakeResponse("application/json", '{"id": 1}')) == [{"id": 1}]


def test_report_check_finds_endpoints_and_paths():
    assert check_text('{"x": "https://myres.openai.azure.com/openai/v1/"}')
    assert check_text('{"src": "C:\\\\Users\\\\me\\\\file"}')
    assert check_text('{"batch_id": "batch_123"}')
    assert not check_text('{"design": "two-stage", "usd_per_1000_records": 8.1}')


def test_analyze_input_and_output():
    line = {"custom_id": "1", "url": "/v1/responses", "body": {"model": "m", "tools": [{"type": "web_search"}], "input": [{"role": "user", "content": "hello"}]}}
    rep = analyze_input([line, dict(line, custom_id="2")])
    assert rep["tools"] == {"web_search": 2} and rep["max_tool_calls"] == {"(unset)": 2} and rep["duplicate_custom_ids"] == 0
    out = analyze_output([{"custom_id": "1", "response": {"status_code": 200, "body": BODY}}], "gpt-5.4-mini", PRICES)
    assert out["lines_with_usage"] == 1 and 0 < out["search_share_of_cost"] < 1
