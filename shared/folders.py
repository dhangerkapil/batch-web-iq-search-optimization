"""Where each option's code lives, so the shared job runner can load a design's code from its own folder."""
from __future__ import annotations

import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OPTION_1 = ROOT / "option-1-bounded-web-search"  # bounded web search: web_search_lines.py
OPTION_2 = ROOT / "option-2-two-stage-web-iq"  # two-stage with Web IQ: search_plan.py, webiq.py, two_stage_lines.py
FLEX = ROOT / "flex-web-search-and-web-iq"  # Flex processing: flex.py


def load(folder: pathlib.Path, module: str):
    """Import `module` from an option folder. Stops with a clear message if the folder or the file is missing."""
    if not (folder / f"{module}.py").is_file():
        raise SystemExit(f"{module}.py isn't in {folder}; this command needs that folder of the repository.")
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    return importlib.import_module(module)
