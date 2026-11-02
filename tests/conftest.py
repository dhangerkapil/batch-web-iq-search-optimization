"""Offline test setup: put each folder's code on the path, price with the table saved on 31 Oct 2026, and never read
live prices."""
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
FOLDERS = ("shared", "option-1-bounded-web-search", "option-2-two-stage-web-iq", "flex-web-search-and-web-iq", "evaluation")
for folder in FOLDERS:
    sys.path.insert(0, str(REPO / folder))

os.environ["SEARCH_THEN_BATCH_PRICES"] = str(pathlib.Path(__file__).with_name("fixtures") / "prices-2026-10-31.json")


@pytest.fixture(autouse=True)
def no_live_prices(monkeypatch):
    import prices

    real = prices.fetch_prices

    def offline_only(settings=None, client=None):
        if client is None:
            raise AssertionError("a test tried to read live prices; pass a fake client")
        return real(settings, client)

    monkeypatch.setattr(prices, "fetch_prices", offline_only)
