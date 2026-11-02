"""List prices, read live from the public Azure Retail Prices API, and the cost of one response.

No price is written in the code. settings.toml names the meters to read ([pricing.models] and [pricing.tools]), and
fetch_prices() turns them into the price table every command uses:

  {"currency": "USD", "source": "Azure Retail Prices API", "retrieved_utc": "...",
   "models": {"gpt-5.4-mini": {"global_batch": {"input_per_1m": 0.375, ...}, "source": "..."},
              "gpt-6-sol": {"flex": {"input_per_1m": 1.0, ..., "cache_write_per_1m": 1.25, ...}, "source": "..."}},
   "tools": {"webiq_web_per_1k": {"usd": 7.0, "source": "..."}, ...}}

A model's rates are kept per tier: global_batch for Global Batch jobs, flex for Flex processing (flex_job.py).

The API needs no sign-in and returns list prices
(https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices). For negotiated rates, or
to work without network access, save the table (`python prices.py --save prices.json`), edit it, and pass
--prices prices.json to any command (or set SEARCH_THEN_BATCH_PRICES).

Examples, from this folder:
  python prices.py                                  the table the code uses now
  python prices.py --contains "5.4 mini Batch"      any meter whose name contains the text
  python prices.py --save my-prices.json            save it, to price offline or with negotiated rates
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time

from settings import SETTINGS
from util import read_json

PRICES_FILE_ENV = "SEARCH_THEN_BATCH_PRICES"
# Meters per model and tier. cached_input (prompt-cache reads) and cache_write (prompt-cache writes, billed by GPT-5.6
# and later families) are optional.
TOKEN_FIELDS = ("input", "cached_input", "cache_write", "output")
TIERS = {"batch": "global_batch", "flex": "flex"}  # the price tier of each executor
_LIVE: dict | None = None


class PricingError(Exception):
    """The Retail Prices API couldn't be read, or a configured meter wasn't found in it."""


def _units(unit_of_measure: str) -> float:
    """How many units one price covers: '1M' -> 1e6, '1K' -> 1e3, '10K' -> 1e4, '1' -> 1."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KM]?)\s*", unit_of_measure or "")
    if not m:
        raise PricingError(f"unexpected unitOfMeasure {unit_of_measure!r}")
    return float(m.group(1)) * {"": 1.0, "K": 1e3, "M": 1e6}[m.group(2)]


def _quoted(value: str) -> str:
    return value.replace("'", "''")


def _get(client, url: str, params: dict | None, cfg: dict) -> dict:
    attempts = int(cfg.get("max_attempts", 3))
    for attempt in range(1, attempts + 1):
        try:
            r = client.get(url, params=params)
        except Exception as ex:  # network errors are retried like a 5xx
            if attempt == attempts:
                raise PricingError(f"couldn't reach {url}: {ex}") from ex
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts:
            wait = r.headers.get("retry-after")
            time.sleep(min(60.0, float(wait)) if wait and wait.replace(".", "", 1).isdigit() else 2 ** attempt)
            continue
        raise PricingError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
    raise PricingError(f"no answer from {url}")


def _query(client, cfg: dict, flt: str) -> list[dict]:
    """Every item that matches an OData filter, following NextPageLink."""
    url: str | None = cfg["api_url"]
    params: dict | None = {"$filter": flt, "currencyCode": cfg.get("currency_code", "USD")}
    items: list[dict] = []
    while url:
        body = _get(client, url, params, cfg)
        items += body.get("Items") or []
        url, params = body.get("NextPageLink"), None
    return items


def _select(items: list[dict], meter: str, product: str | None) -> dict:
    """The one current consumption price of a meter. Several items with the same price are fine; different prices for
    one meter mean the configuration has to name the product."""
    found = [i for i in items if i.get("meterName") == meter and i.get("type", "Consumption") == "Consumption"
             and (product is None or i.get("productName") == product)]
    if not found:
        raise PricingError(f"meter '{meter}'" + (f" of '{product}'" if product else "") +
                           " isn't in the Retail Prices API response; check its name in settings.toml")
    found.sort(key=lambda i: (bool(i.get("isPrimaryMeterRegion", True)), i.get("effectiveStartDate") or ""), reverse=True)
    prices = {(i.get("retailPrice"), i.get("unitOfMeasure")) for i in found if i.get("isPrimaryMeterRegion", True)} or \
        {(i.get("retailPrice"), i.get("unitOfMeasure")) for i in found}
    if len(prices) > 1:
        raise PricingError(f"meter '{meter}' has {len(prices)} different prices ({sorted(prices)}); add product = ... "
                           "in settings.toml to pick one")
    return found[0]


def fetch_prices(settings: dict | None = None, client=None) -> dict:
    """Read every meter named in settings ([pricing]) from the Retail Prices API: filtered queries per service and
    region, each naming at most max_meters_per_filter meters."""
    cfg = (settings or SETTINGS)["pricing"]
    per_filter = max(1, int(cfg["max_meters_per_filter"]))
    wanted = []  # (kind, key, tier, field, service, region, product, meter)
    for model, tiers in cfg.get("models", {}).items():
        for tier, spec in tiers.items():
            for field in TOKEN_FIELDS:
                if field in spec:
                    wanted.append(("model", model, tier, field, spec["service"], spec["region"], spec.get("product"), spec[field]))
    for key, spec in cfg.get("tools", {}).items():
        wanted.append(("tool", key, None, None, spec["service"], spec["region"], spec.get("product"), spec["meter"]))
    groups: dict[tuple[str, str], set[str]] = {}
    for w in wanted:
        groups.setdefault((w[4], w[5]), set()).add(w[7])
    own = client is None
    if own:
        import httpx

        client = httpx.Client(timeout=float(cfg.get("timeout_seconds", 60)))
    try:
        items = {}
        for (service, region), meters in groups.items():
            ordered, found = sorted(meters), []
            # The API rejects a filter with more than 20 conditions, so a long meter list is read in groups.
            for start in range(0, len(ordered), per_filter):
                names = " or ".join(f"meterName eq '{_quoted(m)}'" for m in ordered[start:start + per_filter])
                flt = f"serviceName eq '{_quoted(service)}' and armRegionName eq '{_quoted(region)}' and ({names})"
                found += _query(client, cfg, flt)
            items[(service, region)] = found
    finally:
        if own:
            client.close()
    retrieved = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    table: dict = {"currency": cfg.get("currency_code", "USD"), "source": "Azure Retail Prices API", "api_url": cfg["api_url"],
                   "retrieved_utc": retrieved, "models": {}, "tools": {}}
    used: dict[tuple[str, str], list[dict]] = {}
    for kind, key, tier, field, service, region, product, meter in wanted:
        item = _select(items[(service, region)], meter, product)
        units = _units(item.get("unitOfMeasure"))
        if kind == "model":
            table["models"].setdefault(key, {}).setdefault(tier, {})[f"{field}_per_1m"] = round(item["retailPrice"] * 1e6 / units, 10)
            used.setdefault((key, tier), []).append(item)
        else:
            table["tools"][key] = {"usd": round(item["retailPrice"] * 1e3 / units, 10),
                                   "source": f"Azure Retail Prices API: {item.get('productName')}, '{meter}', effective "
                                             f"{(item.get('effectiveStartDate') or '')[:10]}, retrieved {retrieved}"}
    tiers: dict[str, list[str]] = {}
    for (model, tier), its in used.items():
        tiers.setdefault(model, []).append(
            f"{its[0].get('armRegionName')}, {tier} meters " + ", ".join(f"'{i.get('meterName')}'" for i in its)
            + f", effective {max((i.get('effectiveStartDate') or '')[:10] for i in its)}")
    for model, parts in tiers.items():
        table["models"][model]["source"] = f"Azure Retail Prices API, {'; '.join(parts)}, retrieved {retrieved}"
    return table


def load_prices(path: str | os.PathLike | None = None) -> dict:
    """The price table: from path (or the SEARCH_THEN_BATCH_PRICES environment variable) when given, otherwise live
    from the Retail Prices API, read once per process."""
    p = path or os.environ.get(PRICES_FILE_ENV)
    if p:
        table = read_json(pathlib.Path(p))
        table.setdefault("source", f"file {pathlib.Path(p).name}")  # the name only: run records shouldn't hold local paths
        return table
    global _LIVE
    if _LIVE is None:
        try:
            _LIVE = fetch_prices()
        except PricingError as ex:
            print(f"Couldn't read list prices from the Azure Retail Prices API: {ex}. To price without it, pass --prices "
                  f"<file> (saved with `python shared/prices.py --save <file>`, or your negotiated rates).",
                  file=sys.stderr, flush=True)
            raise SystemExit(1) from None
    return _LIVE


def tier_for(executor: str | None) -> str:
    """The price tier of an executor: global_batch for batch (the default), flex for flex."""
    return TIERS[executor or "batch"]


def model_rates(prices: dict, model: str, tier: str = "global_batch") -> dict:
    m = prices["models"].get(model)
    if not m or tier not in m:
        known = ", ".join(sorted(k for k, v in prices["models"].items() if isinstance(v, dict) and tier in v)) or "none"
        hint = " Flex needs a Flex-capable model, for example gpt-6-sol." if tier == "flex" else ""
        raise SystemExit(f"No {tier} price for '{model}' (models with {tier} prices: {known}). Add its meters under "
                         f"[pricing.models.\"{model}\".{tier}] in settings.toml.{hint}")
    return m[tier]


def tool_rate(prices: dict, key: str) -> float:
    """USD per single request or call."""
    return prices["tools"][key]["usd"] / 1000


def line_cost(metrics: dict, rates: dict, prices: dict) -> dict:
    """USD for one completed line (a batch line or a Flex request), from its usage and tool counts.

    cached_tokens (prompt-cache reads) and cache_write_tokens (prompt-cache writes, reported by GPT-5.6 and later
    families) are both parts of input_tokens. When the rates include cache_write_per_1m, written tokens are priced at
    that rate instead of the input rate. That is an assumption: whether cache writes are billed on top of input isn't
    documented, and if they are, the input cost here is too low by cache_write_tokens at the input rate. Without a
    cache-write rate, written tokens stay at the input rate. "cache_write" is the part of "input" spent on writes.
    """
    cached = metrics.get("cached_tokens", 0) or 0
    written = (metrics.get("cache_write_tokens", 0) or 0) if "cache_write_per_1m" in rates else 0
    uncached = max(0, (metrics.get("input_tokens", 0) or 0) - cached - written)
    tokens_in = uncached * rates["input_per_1m"] / 1e6 + cached * rates.get("cached_input_per_1m", rates["input_per_1m"]) / 1e6
    cache_write = written * rates["cache_write_per_1m"] / 1e6 if written else 0.0
    tokens_in += cache_write
    tokens_out = (metrics.get("output_tokens", 0) or 0) * rates["output_per_1m"] / 1e6
    web_search = (metrics.get("web_search_requests", 0) or 0) * tool_rate(prices, "web_search_per_1k_requests")
    return {"input": tokens_in, "cache_write": cache_write, "output": tokens_out, "web_search": web_search,
            "total": tokens_in + tokens_out + web_search}


def source_line(table: dict) -> str:
    """Where a price table came from and when, for forecasts and run records."""
    return f"{table.get('source')}, retrieved {table.get('retrieved_utc') or table.get('retrieved_note') or 'unknown'}"


def show_table(table: dict) -> str:
    out = [f"{source_line(table)} ({table.get('currency', 'USD')})"]
    for model, tiers in table.get("models", {}).items():
        for tier, r in tiers.items():
            if isinstance(r, dict):
                write = f"cache write {r['cache_write_per_1m']:>7g}  " if "cache_write_per_1m" in r else ""
                out.append(f"  {model:<14} {tier:<13} input {r['input_per_1m']:>7g}  cached input {r.get('cached_input_per_1m', r['input_per_1m']):>7g}  "
                           f"{write}output {r['output_per_1m']:>7g}  per 1M tokens")
    for key, t in table.get("tools", {}).items():
        out.append(f"  {key:<28} {t['usd']:>7g} per 1,000")
    return "\n".join(out)


def save_prices(out: str | os.PathLike, path: str | os.PathLike | None = None) -> dict:
    """Save the price table (live unless path is given), to price offline or with edited (negotiated) rates later."""
    table = load_prices(path)
    pathlib.Path(out).write_text(json.dumps(table, indent=2), encoding="utf-8")
    return table


def lookup(contains: str, region: str | None = None, service: str | None = None, timeout: float | None = None) -> list[dict]:
    """Query the public Azure Retail Prices API (no sign-in) for consumption meters whose name contains `contains`."""
    import httpx

    cfg = SETTINGS["pricing"]
    flt = f"armRegionName eq '{_quoted(region or cfg['lookup_region'])}' and contains(meterName, '{_quoted(contains)}')"
    if service:
        flt += f" and serviceName eq '{_quoted(service)}'"
    with httpx.Client(timeout=timeout or float(cfg.get("timeout_seconds", 60))) as client:
        items = _query(client, cfg, flt)
    return sorted((i for i in items if i.get("type") == "Consumption"), key=lambda i: (i.get("serviceName", ""), i.get("meterName", "")))


def main() -> None:
    ap = argparse.ArgumentParser(prog="python prices.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--save", metavar="FILE", help="save the price table to FILE; pass --prices FILE later to price offline or with edited (negotiated) rates")
    g.add_argument("--contains", help="look up any meter whose name contains this text, for example '5.4 mini Batch'")
    ap.add_argument("--prices", help="show (or save) this price table instead of live list prices")
    ap.add_argument("--region", default=SETTINGS["pricing"]["lookup_region"], help="with --contains")
    ap.add_argument("--service", help="with --contains, for example 'Foundry Models'")
    a = ap.parse_args()
    if a.contains:
        for i in lookup(a.contains, a.region, a.service):
            print(f"{i['serviceName']:<16} {i['meterName']:<40} {i['retailPrice']:>10} per {i['unitOfMeasure']:<4} from {i['effectiveStartDate'][:10]}")
        return
    print(show_table(save_prices(a.save, a.prices) if a.save else load_prices(a.prices)))
    if a.save:
        print(f"Saved to {a.save}. Price with it by passing --prices {a.save}.")


if __name__ == "__main__":
    main()
