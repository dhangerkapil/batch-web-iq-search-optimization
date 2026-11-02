"""One exit-code contract for every option's `run` (shared/exits.py), so a scheduler knows what to do next."""
import argparse
import pathlib
import subprocess
import sys

import pytest

import cli
import exits

REPO = pathlib.Path(__file__).resolve().parents[1]
RUNS = (("option-1-bounded-web-search", "bounded_web_search.py", True), ("option-2-two-stage-web-iq", "two_stage.py", True),
        ("flex-web-search-and-web-iq", "flex_job.py", False))


def command(fn):
    ap = argparse.ArgumentParser()
    ap.set_defaults(fn=fn)
    return ap


@pytest.mark.parametrize("outcome, code", [
    (exits.HELD, 3),
    (exits.Exit(exits.RERUN, "2 batch jobs still running"), 75),
    (exits.Exit(exits.NEEDS_PERSON, "sign in again"), 1),
    (KeyboardInterrupt(), 130),
], ids=["held", "rerun", "needs-a-person", "ctrl-c"])
def test_run_exits_with_the_code_of_the_contract(monkeypatch, capsys, outcome, code):
    def fn(a):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(sys, "argv", ["run"])
    with pytest.raises(SystemExit) as ended:
        cli.run(command(fn))
    assert ended.value.code == code
    if isinstance(outcome, exits.Exit):
        assert outcome.message in capsys.readouterr().err  # the message is printed, as SystemExit("message") would


def test_a_finished_run_exits_0(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run"])
    cli.run(command(lambda a: exits.DONE))
    cli.run(command(lambda a: None))  # plan, search and build return nothing


@pytest.mark.parametrize("folder, script, batch", RUNS, ids=[r[1] for r in RUNS])
def test_every_run_can_turn_off_the_automatic_rounds(folder, script, batch):
    r = subprocess.run([sys.executable, script, "run", "--help"], cwd=REPO / folder, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "--no-auto-retry" in r.stdout and ("--max-wait-minutes" in r.stdout) == batch


def test_the_exit_codes_are_documented():
    for doc in ("docs/07-deploy-and-run.md", *(f"{folder}/README.md" for folder, _, _ in RUNS)):
        text = (REPO / doc).read_text(encoding="utf-8")
        assert all(f"| `{code}` |" in text for code in (0, 1, 3, 75, 130)), doc
