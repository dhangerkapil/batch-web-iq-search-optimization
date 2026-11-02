"""Load settings.toml, the one file that holds every tunable value the code uses.

Set the environment variable SEARCH_THEN_BATCH_SETTINGS to the path of another copy to use it instead.
"""
from __future__ import annotations

import os
import pathlib
import sys

if sys.version_info >= (3, 11):
    import tomllib
else:  # Python 3.10
    import tomli as tomllib

ENV = "SEARCH_THEN_BATCH_SETTINGS"
PATH = pathlib.Path(os.environ.get(ENV) or pathlib.Path(__file__).with_name("settings.toml"))


def load(path: str | os.PathLike | None = None) -> dict:
    p = pathlib.Path(path) if path else PATH
    try:
        with open(p, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        raise SystemExit(f"Settings file not found: {p}") from None
    except tomllib.TOMLDecodeError as ex:
        raise SystemExit(f"{p} isn't valid TOML: {ex}") from None


SETTINGS = load()
