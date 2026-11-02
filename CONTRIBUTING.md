# Contributing

Contributions and suggestions are welcome. For a larger change, open an issue to discuss it before you start.

## Before you open a pull request

- Run the offline tests: `python -m pytest -q`. They call no Azure or Web IQ service.
- Don't commit keys, tokens, endpoints, subscription IDs, evidence files (`evidence.jsonl`), batch input files or
  `state.json`. Use `python evaluation/report.py` to copy shareable run files; it refuses files that contain
  endpoints or local paths, and removes citation URLs that point to pages about individuals. Read results before you
  publish them.
- Prices aren't in the code: they're read live from the Azure Retail Prices API. If you add or change a meter in
  `shared/settings.toml`, check that it resolves with `python shared/prices.py`. Keep the tests on
  the saved table in `tests/fixtures/`, and put new tunable values in settings.toml rather than in code.
- Keep each option's code in its own folder, and code that more than one option uses in `shared/`. Module names must
  be unique across folders (`tests/test_layout.py` checks), because each script puts its own folder and `shared/` on
  Python's path.
- If you add a claim about a service to the docs, link the Microsoft Learn page (or other public source) beside it.
