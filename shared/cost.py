"""Cost forecasts before spending, and actual cost from per-line usage (Global Batch or Flex processing)."""
from __future__ import annotations

import json

from batch_lines import line_text
from prices import line_cost, model_rates, source_line, tier_for, tool_rate
from settings import SETTINGS
from util import count_tokens
from workloads import Workload

_F = SETTINGS["forecast"]
# Output tokens per record assumed in forecasts ([forecast.output_tokens_per_line] in settings.toml: measured on the
# sample and rounded up, so forecasts err high). Measure your own workload and pass --output-tokens-per-line.
DEFAULT_OUTPUT_TOKENS = dict(_F["output_tokens_per_line"])
DEFAULT_OUTPUT_TOKENS_OTHER = int(_F["default_output_tokens_per_line"])
# Message framing added by the service on top of the text (measured: a few tokens per message).
FRAMING_TOKENS = int(_F["framing_tokens"])


def format_tokens(text_format: dict | None) -> int:
    """Tokens the JSON schema adds to every request (approximation: the schema text itself)."""
    return count_tokens(json.dumps(text_format, separators=(",", ":")))[0] if text_format else 0


def forecast(*, prices: dict, price_model: str, lines: int, input_tokens: float, exact: bool, webiq_calls: int, output_per_line: int,
             tier: str = "global_batch") -> dict:
    """Forecast at the rates of one price tier: global_batch (Global Batch) or flex (Flex processing, flex_job.py)."""
    rates = model_rates(prices, price_model, tier)
    # Models that bill prompt-cache writes charge more for a written token than for input. Pricing every input token at
    # the higher rate keeps the forecast erring high however much of the prompt is written to the cache.
    in_rate = max(rates["input_per_1m"], rates.get("cache_write_per_1m", 0.0))
    webiq = webiq_calls * tool_rate(prices, "webiq_web_per_1k")
    tok_in = input_tokens * in_rate / 1e6
    tok_out = lines * output_per_line * rates["output_per_1m"] / 1e6
    total = webiq + tok_in + tok_out
    return {
        "lines": lines,
        "webiq_calls": webiq_calls,
        "input_tokens": round(input_tokens),
        "input_tokens_exact": exact,
        "output_tokens_per_line_assumed": output_per_line,
        "usd": {"webiq": round(webiq, 4), "input": round(tok_in, 4), "output": round(tok_out, 4), "total": round(total, 4)},
        "usd_per_1000_records": round(total / max(1, lines) * 1000, 2),
        "note": f"Prices: {source_line(prices)}; {price_model} {tier} rates. Cached-input discounts are not assumed"
                + (" and input is priced at the cache-write rate" if in_rate > rates["input_per_1m"] else "") + ", so forecasts err high.",
    }


def estimate_before_search(workload: Workload, records: list[dict], plan: dict[str, dict], opts, prices: dict) -> dict:
    """Before any search: exact prompt tokens plus an upper-bound estimate of the evidence each record will carry."""
    uses: dict[str, int] = {}
    for e in plan.values():
        for rid in e["used_by"]:
            uses[rid] = uses.get(rid, 0) + 1
    static = count_tokens(workload.system_prompt() + "\n" + workload.rules())[0] + format_tokens(workload.output_format()) + 2 * FRAMING_TOKENS
    # Upper bound per search: every result at full length, plus its title/URL line (~160 characters).
    per_search = int(opts.max_results * (opts.max_chars + 160) / 3.6)
    total = 0
    for rec in records:
        total += static + count_tokens(workload.record_text(rec))[0] + uses.get(workload.record_id(rec), 0) * per_search
    return forecast(
        prices=prices,
        price_model=opts.price_model,
        lines=len(records),
        input_tokens=total,
        exact=False,
        webiq_calls=len(plan),
        output_per_line=opts.output_tokens_per_line or DEFAULT_OUTPUT_TOKENS.get(opts.price_model, DEFAULT_OUTPUT_TOKENS_OTHER),
        tier=tier_for(getattr(opts, "executor", None)),
    )


def exact_before_submit(lines: list[dict], webiq_calls: int, opts, prices: dict) -> dict:
    """After the searches: input tokens counted from the finished batch lines (no tools, so nothing is added later)."""
    total, exact = 0, True
    for line in lines:
        n, ok = count_tokens(line_text(line))
        exact = exact and ok
        total += n + format_tokens((line["body"].get("text") or {}).get("format")) + 2 * FRAMING_TOKENS
    return forecast(
        prices=prices,
        price_model=opts.price_model,
        lines=len(lines),
        input_tokens=total,
        exact=exact,
        webiq_calls=webiq_calls,
        output_per_line=opts.output_tokens_per_line or DEFAULT_OUTPUT_TOKENS.get(opts.price_model, DEFAULT_OUTPUT_TOKENS_OTHER),
        tier=tier_for(getattr(opts, "executor", None)),
    )


def print_forecast(title: str, f: dict) -> None:
    u = f["usd"]
    print(
        f"{title}: {f['lines']:,} records, {f['webiq_calls']:,} Web IQ calls, {f['input_tokens']:,} input tokens "
        f"({'exact' if f['input_tokens_exact'] else 'estimate'}), {f['output_tokens_per_line_assumed']} output tokens/record assumed -> "
        f"${u['total']:,.4f} (Web IQ ${u['webiq']:,.4f} + input ${u['input']:,.4f} + output ${u['output']:,.4f}) "
        f"= ${f['usd_per_1000_records']:,.2f} per 1,000 records"
    )


def actual(rows: list[dict], *, records: int, prices: dict, price_model: str, webiq_calls: int, tier: str = "global_batch") -> dict:
    """Actual cost from per-line usage, plus the Web IQ calls made before the job.

    tier is the price tier the lines ran on: global_batch (the Batch API) or flex (Flex processing). Web IQ is counted
    as one call per unique search that returned results; failed attempts are listed in run.json (stage1) because the
    Web IQ pricing page doesn't say whether they're billed. `rows` include lines kept from earlier batches of the same
    job that didn't complete, since those lines were billed too. Where the rates include a cache-write rate, written
    prompt-cache tokens are priced at it instead of the input rate (an assumption, see prices.line_cost);
    usd.total_if_cache_writes_billed_on_top shows the total if they turn out to be billed on top of input.
    """
    rates = model_rates(prices, price_model, tier)
    billed = [r for r in rows if r.get("status") not in (None, "error")]
    sums = {k: sum((r.get(k) or 0) for r in billed) for k in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "web_search_requests", "web_search_calls")}
    cost = {"input": 0.0, "output": 0.0, "web_search": 0.0}
    for r in billed:
        c = line_cost(r, rates, prices)
        for k in cost:
            cost[k] += c[k]
    webiq = webiq_calls * tool_rate(prices, "webiq_web_per_1k")
    total = cost["input"] + cost["output"] + cost["web_search"] + webiq
    n = max(1, len(billed))
    usd = {
        "tokens_input": round(cost["input"], 6),
        "tokens_output": round(cost["output"], 6),
        "web_search": round(cost["web_search"], 6),
        "webiq": round(webiq, 6),
        "total": round(total, 6),
    }
    if "cache_write_per_1m" in rates:
        usd["total_if_cache_writes_billed_on_top"] = round(total + sums["cache_write_tokens"] * rates["input_per_1m"] / 1e6, 6)
    out = {
        "records": records,
        "lines_with_usage": len(billed),
        "lines_failed": sum(1 for r in rows if r.get("status") == "error") + max(0, records - len(rows)),
        "json_valid": sum(1 for r in billed if r.get("json_valid")),
        "per_record_mean": {
            "input_tokens": round(sums["input_tokens"] / n, 1),
            "cached_tokens": round(sums["cached_tokens"] / n, 1),
            "cache_write_tokens": round(sums["cache_write_tokens"] / n, 1),
            "output_tokens": round(sums["output_tokens"] / n, 1),
            "reasoning_tokens": round(sums["reasoning_tokens"] / n, 1),
            "web_search_requests": round(sums["web_search_requests"] / n, 2),
            "web_search_calls": round(sums["web_search_calls"] / n, 2),
            "webiq_calls": round(webiq_calls / max(1, records), 3),
        },
        "totals": sums | {"webiq_calls": webiq_calls},
        "usd": usd,
        "usd_per_1000_records": round(total / max(1, records) * 1000, 2),
        "search_share_of_cost": round((cost["web_search"] + webiq) / total, 3) if total else None,
        "prices": {"model": price_model, "tier": tier, "source": prices["models"][price_model].get("source"),
               "retrieved_utc": prices.get("retrieved_utc"), "rates": {k: v for k, v in rates.items()},
               "tools": {k: v.get("usd") for k, v in (prices.get("tools") or {}).items()}},
    }
    if "cache_write_per_1m" in rates:
        out["prices"]["cache_writes"] = ("written prompt-cache tokens are priced at the cache-write rate instead of the input "
                                         "rate; whether they are billed on top of input is an open question")
    return out
