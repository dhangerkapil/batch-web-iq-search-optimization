"""Small shared helpers: JSONL files, timestamps and token counting."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import pathlib
from typing import Iterable

_ENCODING = None


def read_jsonl(path: str | os.PathLike) -> list[dict]:
    """Read a JSONL file. A UTF-8 byte-order mark is tolerated on read; files written here never have one."""
    if not pathlib.Path(path).is_file():
        raise SystemExit(f"File not found: {path}")
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"{path}: line {n} is not valid JSON: {e}")
    return rows


def write_jsonl(path: str | os.PathLike, rows: Iterable[dict]) -> int:
    """Write UTF-8 JSONL without a byte-order mark and with \\n line endings, as the Batch API requires."""
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
            n += 1
    return n


def write_json(path: str | os.PathLike, obj: dict) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json(path: str | os.PathLike) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


REPO = pathlib.Path(__file__).resolve().parent.parent


def script(*parts: str) -> str:
    """A file in this repository as a path from the current folder, so a command printed in a message runs as shown
    from the repository root or from an option folder."""
    path = REPO.joinpath(*parts)
    try:
        return os.path.relpath(path)
    except ValueError:  # on Windows, the current folder is on another drive
        return str(path)


def count_tokens(text: str) -> tuple[int, bool]:
    """Return (tokens, exact). Exact with tiktoken's o200k_base encoding; otherwise about 3.6 characters per token.

    tiktoken downloads its encoding file on first use. Without internet access the count falls back to the estimate.
    """
    global _ENCODING
    if _ENCODING is None:
        try:
            import tiktoken  # type: ignore

            _ENCODING = tiktoken.get_encoding("o200k_base")
        except Exception:
            _ENCODING = False
    if _ENCODING:
        return len(_ENCODING.encode(text)), True
    return math.ceil(len(text) / 3.6), False
