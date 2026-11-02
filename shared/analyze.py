"""Look at an existing batch file before changing anything: what drives its cost?

  Input file (lines with "body"):  tools per line, caps, reasoning settings, output format, prompt tokens,
                                   and the shared prefix that prompt caching could reuse.
  Output file (lines with "response"): billed web search requests, search calls, tokens and cost per line,
                                   and the share of cost that is search.
Runs locally and sends none of your data. Pricing an output file reads public list prices (or --prices <file>).

Example, from this folder:
  python analyze.py my-batch-output.jsonl --price-model gpt-5.4-mini
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics

from batch import response_metrics
from batch_lines import line_text
from prices import line_cost, load_prices, model_rates, source_line
from settings import SETTINGS
from util import count_tokens, read_jsonl

CACHE_MIN_TOKENS = int(SETTINGS["caching"]["min_prefix_tokens"])


def _stats(values: list[float]) -> str:
    if not values:
        return "-"
    return f"min {min(values):,.0f} / mean {statistics.mean(values):,.1f} / max {max(values):,.0f}"


def analyze_input(rows: list[dict]) -> dict:
    tools, caps, efforts, formats, max_calls = (collections.Counter() for _ in range(5))
    texts, tokens = [], []
    for r in rows:
        b = r.get("body") or {}
        for t in b.get("tools") or [{"type": "(none)"}]:
            tools[t.get("type")] += 1
        caps[str(b.get("max_output_tokens"))] += 1
        efforts[(b.get("reasoning") or {}).get("effort", "(model default)")] += 1
        formats[((b.get("text") or {}).get("format") or {}).get("type", "(free text)")] += 1
        max_calls[str(b.get("max_tool_calls", "(unset)"))] += 1
        t = line_text(r)
        texts.append(t)
        tokens.append(count_tokens(t)[0])
    prefix_tokens = count_tokens(os.path.commonprefix(texts))[0] if texts else 0
    ids = [r.get("custom_id") for r in rows]
    return {
        "lines": len(rows),
        "duplicate_custom_ids": len(ids) - len(set(ids)),
        "deployments": dict(collections.Counter((r.get("body") or {}).get("model") for r in rows)),
        "tools": dict(tools),
        "max_tool_calls": dict(max_calls),
        "reasoning_effort": dict(efforts),
        "output_format": dict(formats),
        "max_output_tokens": dict(caps),
        "prompt_tokens": _stats(tokens),
        "prompt_tokens_total": sum(tokens),
        "shared_prefix_tokens": prefix_tokens,
        "prompt_caching_possible": prefix_tokens >= CACHE_MIN_TOKENS,
    }


def analyze_output(rows: list[dict], price_model: str | None, prices: dict) -> dict:
    metrics = []
    for o in rows:
        body = ((o.get("response") or {}).get("body")) or {}
        if (o.get("response") or {}).get("status_code") == 200 and body:
            metrics.append(response_metrics(body))
    out = {
        "lines": len(rows),
        "lines_with_usage": len(metrics),
        "web_search_requests_per_line": _stats([m["web_search_requests"] for m in metrics]),
        "web_search_calls_per_line": _stats([m["web_search_calls"] for m in metrics]),
        "input_tokens_per_line": _stats([m["input_tokens"] for m in metrics]),
        "cached_tokens_per_line": _stats([m["cached_tokens"] for m in metrics]),
        "output_tokens_per_line": _stats([m["output_tokens"] for m in metrics]),
        "reasoning_tokens_per_line": _stats([m["reasoning_tokens"] for m in metrics]),
        "json_valid": sum(1 for m in metrics if m["json_valid"]),
    }
    if price_model and metrics:
        rates = model_rates(prices, price_model, "global_batch")
        costs = [line_cost(m, rates, prices) for m in metrics]
        total = sum(c["total"] for c in costs)
        search = sum(c["web_search"] for c in costs)
        out.update(
            usd_per_1000_lines=round(total / len(costs) * 1000, 2),
            usd_search_per_1000_lines=round(search / len(costs) * 1000, 2),
            search_share_of_cost=round(search / total, 3) if total else None,
            priced_as=f"{price_model} global_batch ({source_line(prices)})",
        )
    return out


def cmd_analyze(a) -> None:
    rows = read_jsonl(a.file)
    if rows and "response" in rows[0]:
        report = analyze_output(rows, a.price_model, load_prices(a.prices) if a.price_model else {})
    else:
        report = analyze_input(rows)
    print(json.dumps(report, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python analyze.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="a batch input file (lines with body) or output file (lines with response), JSONL")
    ap.add_argument("--price-model", help="output files: price the lines with this model's batch rates")
    ap.add_argument("--prices", help="price table to use instead of live list prices")
    cmd_analyze(ap.parse_args())


if __name__ == "__main__":
    main()
