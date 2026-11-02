"""The folder layout: one folder per option with its own code and guides, and shared/ for the code every option uses."""
import ast
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
OPTIONS = ("option-1-bounded-web-search", "option-2-two-stage-web-iq", "flex-web-search-and-web-iq")
FOLDERS = ("shared", *OPTIONS, "evaluation")
ENTRY_SCRIPTS = {
    "option-1-bounded-web-search": ("bounded_web_search.py",),
    "option-2-two-stage-web-iq": ("two_stage.py", "toolbox.py"),
    "flex-web-search-and-web-iq": ("flex_job.py",),
    "shared": ("analyze.py", "prices.py"),
    "evaluation": ("evaluate.py", "report.py"),
}


def test_each_option_folder_has_its_guides_and_its_code():
    for folder in OPTIONS:
        names = {p.name for p in (REPO / folder).iterdir()}
        assert {"README.md", "WALKTHROUGH.md"} <= names, folder
        assert any(n.endswith(".py") for n in names), folder


def test_module_names_are_unique_across_folders():
    seen: dict[str, str] = {}
    for folder in FOLDERS:
        for p in (REPO / folder).glob("*.py"):
            assert p.stem not in seen, f"{p.name} is in both {seen.get(p.stem)} and {folder}"
            seen[p.stem] = folder


@pytest.mark.parametrize("folder,script", [(f, s) for f, scripts in ENTRY_SCRIPTS.items() for s in scripts])
def test_every_entry_script_runs_from_its_folder(folder, script):
    r = subprocess.run([sys.executable, script, "--help"], cwd=REPO / folder, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "usage: python " + script in r.stdout


def test_an_entry_script_also_runs_from_the_repository_root():
    r = subprocess.run([sys.executable, "option-2-two-stage-web-iq/two_stage.py", "plan", "--help"], cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_shared_code_imports_no_option_module_when_it_loads():
    option_modules = {p.stem for f in (*OPTIONS, "evaluation") for p in (REPO / f).glob("*.py")}
    for p in (REPO / "shared").rglob("*.py"):
        for node in ast.parse(p.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = {(node.module or "").split(".")[0]}
            else:
                continue
            assert not names & option_modules, f"{p.name} imports {names & option_modules} when it loads"


def test_the_runner_loads_each_option_only_when_a_command_needs_it():
    code = ("import sys; sys.path.insert(0, 'shared'); import pipeline; "
            "print(sorted(m for m in ('flex', 'webiq', 'search_plan', 'two_stage_lines', 'web_search_lines') if m in sys.modules))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "[]"


def test_the_flex_ledger_has_one_name():
    import flex
    import pipeline

    assert pipeline.FLEX_LEDGER == flex.LEDGER


def test_a_message_names_a_script_as_it_runs_from_the_current_folder(monkeypatch):
    import os

    from util import script

    monkeypatch.chdir(REPO)
    assert script("shared", "analyze.py") == os.path.join("shared", "analyze.py")
    monkeypatch.chdir(REPO / "option-1-bounded-web-search")
    assert script("shared", "analyze.py") == os.path.join("..", "shared", "analyze.py")


def test_plan_tells_you_where_analyze_is_from_the_repository_root():
    r = subprocess.run([sys.executable, "option-1-bounded-web-search/bounded_web_search.py", "plan", "samples/brand-owner/records.jsonl"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert f"measure them with {pathlib.Path('shared', 'analyze.py')} on" in r.stdout
