"""Live pricing against a fake Azure Retail Prices API, and the settings file."""
import json
import os
import pathlib
import re
import subprocess
import sys

import httpx
import pytest

import prices as P
import settings as S
from settings import SETTINGS

REPO = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = json.loads((pathlib.Path(__file__).with_name("fixtures") / "prices-2026-10-31.json").read_text(encoding="utf-8"))


def catalog(table=FIXTURE):
    """Retail Prices API items carrying the prices in `table`, under the meter names in settings.toml (every tier,
    including optional meters such as cache_write)."""
    cfg, items = SETTINGS["pricing"], []
    for model, tiers in cfg["models"].items():
        for tier, spec in tiers.items():
            for field in P.TOKEN_FIELDS:
                if field not in spec:
                    continue
                items.append({"serviceName": spec["service"], "armRegionName": spec["region"], "productName": "Azure OpenAI",
                              "meterName": spec[field], "unitOfMeasure": "1M", "type": "Consumption", "isPrimaryMeterRegion": True,
                              "effectiveStartDate": "2026-10-01T00:00:00Z", "retailPrice": table["models"][model][tier][f"{field}_per_1m"]})
    for key, spec in cfg["tools"].items():
        items.append({"serviceName": spec["service"], "armRegionName": spec["region"], "productName": spec.get("product", ""),
                      "meterName": spec["meter"], "unitOfMeasure": "1K", "type": "Consumption", "isPrimaryMeterRegion": True,
                      "effectiveStartDate": "2026-10-01T00:00:00Z", "retailPrice": table["tools"][key]["usd"]})
    return items


def fake_api(items, page_size=4, fail_first=0):
    """A Retail Prices API stand-in that understands the filters fetch_prices sends, pages its answers and can
    rate-limit the first requests."""
    calls = {"n": 0, "failed": 0, "filters": []}

    def handler(request):
        calls["n"] += 1
        if calls["failed"] < fail_first:
            calls["failed"] += 1
            return httpx.Response(429, headers={"retry-after": "0"})
        flt = request.url.params["$filter"]
        calls["filters"].append(flt)

        def values(name):
            return [v.replace("''", "'") for v in re.findall(name + r" eq '((?:[^']|'')*)'", flt)]

        service, region, meters = values("serviceName")[0], values("armRegionName")[0], set(values("meterName"))
        hits = [i for i in items if i["serviceName"] == service and i["armRegionName"] == region and i["meterName"] in meters]
        skip = int(request.url.params.get("$skip", 0))
        more = skip + page_size < len(hits)
        nxt = str(request.url.copy_merge_params({"$skip": str(skip + page_size)})) if more else None
        return httpx.Response(200, json={"Items": hits[skip:skip + page_size], "NextPageLink": nxt})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(P.time, "sleep", lambda s: None)


def test_live_prices_read_every_configured_meter_across_pages():
    client, calls = fake_api(catalog())
    table = P.fetch_prices(SETTINGS, client=client)
    for model, tiers in SETTINGS["pricing"]["models"].items():
        for tier in tiers:
            assert table["models"][model][tier] == pytest.approx(FIXTURE["models"][model][tier])
        assert "retrieved" in table["models"][model]["source"]
    for key in SETTINGS["pricing"]["tools"]:
        assert table["tools"][key]["usd"] == pytest.approx(FIXTURE["tools"][key]["usd"])
    assert table["source"] == "Azure Retail Prices API" and table["retrieved_utc"]
    assert calls["n"] > 2  # filtered queries per service and region, each over several pages


def test_each_filter_stays_within_the_api_condition_limit():
    """The Retail Prices API rejected a $filter with more than 20 conditions (HTTP 400, 1 Nov 2026), so a service and
    region with many meters, such as Foundry Models with every model and tier, is read in several requests."""
    client, calls = fake_api(catalog(), page_size=100)
    table = P.fetch_prices(SETTINGS, client=client)
    limit = int(SETTINGS["pricing"]["max_meters_per_filter"])
    assert calls["filters"] and all(f.count(" eq ") <= 20 for f in calls["filters"])
    assert all(f.count("meterName eq ") <= limit for f in calls["filters"])
    foundry = {spec[field] for tiers in SETTINGS["pricing"]["models"].values() for spec in tiers.values()
               for field in P.TOKEN_FIELDS if field in spec}
    assert len(foundry) > limit  # so this test really exercises the split
    assert table["models"]["gpt-6-sol"]["flex"]["cache_write_per_1m"] == 1.25


def test_the_cache_write_meter_is_read_when_configured_and_shown():
    client, _ = fake_api(catalog())
    table = P.fetch_prices(SETTINGS, client=client)
    flex = table["models"]["gpt-6-sol"]["flex"]
    assert flex == {"input_per_1m": 1.0, "cached_input_per_1m": 0.1, "cache_write_per_1m": 1.25, "output_per_1m": 5.0}
    assert SETTINGS["pricing"]["models"]["gpt-6-sol"]["flex"]["cache_write"] in table["models"]["gpt-6-sol"]["source"]
    assert "cache_write_per_1m" not in table["models"]["gpt-5.4-mini"]["global_batch"]  # optional: only where configured
    text = P.show_table(table)
    assert "cache write    1.25" in text and text.count("cache write") == 1


def test_prices_per_1k_tokens_are_converted_to_per_1m():
    items = catalog()
    meter = SETTINGS["pricing"]["models"]["gpt-5.4-mini"]["global_batch"]["input"]
    for i in items:
        if i["meterName"] == meter:
            i.update(unitOfMeasure="1K", retailPrice=i["retailPrice"] / 1000)
    client, _ = fake_api(items)
    rates = P.fetch_prices(SETTINGS, client=client)["models"]["gpt-5.4-mini"]["global_batch"]
    assert rates["input_per_1m"] == pytest.approx(FIXTURE["models"]["gpt-5.4-mini"]["global_batch"]["input_per_1m"])


def test_a_missing_meter_stops_with_its_name():
    meter = SETTINGS["pricing"]["models"]["gpt-5.4"]["global_batch"]["output"]
    client, _ = fake_api([i for i in catalog() if i["meterName"] != meter])
    with pytest.raises(P.PricingError, match=re.escape(meter)):
        P.fetch_prices(SETTINGS, client=client)


def test_a_meter_with_two_prices_must_name_its_product():
    items = catalog()
    web = SETTINGS["pricing"]["tools"]["webiq_web_per_1k"]
    items.append(dict(next(i for i in items if i["meterName"] == web["meter"]), productName="Another product", retailPrice=99.0))
    client, _ = fake_api(items)
    assert P.fetch_prices(SETTINGS, client=client)["tools"]["webiq_web_per_1k"]["usd"] == FIXTURE["tools"]["webiq_web_per_1k"]["usd"]
    out = SETTINGS["pricing"]["models"]["gpt-5.4"]["global_batch"]["output"]
    items.append(dict(next(i for i in items if i["meterName"] == out), retailPrice=99.0))
    client, _ = fake_api(items)
    with pytest.raises(P.PricingError, match="different prices"):
        P.fetch_prices(SETTINGS, client=client)


def test_rate_limits_are_retried_then_reported():
    client, calls = fake_api(catalog(), fail_first=2)
    assert P.fetch_prices(SETTINGS, client=client)["tools"] and calls["failed"] == 2
    client, _ = fake_api(catalog(), fail_first=99)
    with pytest.raises(P.PricingError, match="429"):
        P.fetch_prices(SETTINGS, client=client)


def test_a_price_file_wins_and_only_its_name_is_recorded(tmp_path, monkeypatch):
    f = tmp_path / "negotiated.json"
    f.write_text(json.dumps({k: v for k, v in FIXTURE.items() if k != "source"}), encoding="utf-8")
    assert P.load_prices(f)["source"] == "file negotiated.json"
    monkeypatch.setenv(P.PRICES_FILE_ENV, str(f))
    assert P.load_prices()["source"] == "file negotiated.json"


def test_live_prices_are_read_once_and_a_failure_says_what_to_do(monkeypatch, capsys):
    monkeypatch.delenv(P.PRICES_FILE_ENV)
    monkeypatch.setattr(P, "_LIVE", None)
    calls = []
    monkeypatch.setattr(P, "fetch_prices", lambda: calls.append(1) or FIXTURE)
    assert P.load_prices() is P.load_prices() and len(calls) == 1
    monkeypatch.setattr(P, "_LIVE", None)

    def unavailable():
        raise P.PricingError("couldn't reach the API")

    monkeypatch.setattr(P, "fetch_prices", unavailable)
    with pytest.raises(SystemExit):
        P.load_prices()
    assert "--prices" in capsys.readouterr().err


def test_prices_command_shows_and_saves_the_table(tmp_path, monkeypatch, capsys):
    out = tmp_path / "saved.json"
    monkeypatch.setattr(sys, "argv", ["prices.py", "--save", str(out)])
    P.main()
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["models"]["gpt-5.4"]["global_batch"] == FIXTURE["models"]["gpt-5.4"]["global_batch"]
    text = capsys.readouterr().out
    assert "gpt-5.4-mini" in text and "webiq_web_per_1k" in text and "Saved to" in text


def test_settings_file_can_be_swapped(tmp_path):
    alt = tmp_path / "settings.toml"
    alt.write_text(S.PATH.read_text(encoding="utf-8").replace("max_results = 5\n", "max_results = 3\n"), encoding="utf-8")
    assert S.load(alt)["webiq"]["max_results"] == 3
    with pytest.raises(SystemExit, match="not found"):
        S.load(tmp_path / "missing.toml")
    env = {**os.environ, S.ENV: str(alt)}
    code = "import sys; sys.path.insert(0, 'shared'); from settings import SETTINGS; print(SETTINGS['webiq']['max_results'])"
    run = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "3"


def test_settings_cover_every_model_the_forecast_knows():
    assert set(SETTINGS["forecast"]["output_tokens_per_line"]) == set(SETTINGS["pricing"]["models"])
