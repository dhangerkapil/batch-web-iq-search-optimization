# Results

Measured runs, copied with `python evaluation/report.py <work folder> --out results/<date> --redact-records R22,R23`. `report` refuses files that contain endpoints, local paths or job IDs, and removes citation URLs that point to pages about individuals.

## 2026-10-31

Six Global Batch jobs on the 23 records in [samples/brand-owner](../samples/brand-owner/), East US 2. Summary and analysis: [docs/09-results-on-the-sample.md](../docs/09-results-on-the-sample.md). Accuracy table: [2026-10-31/evaluation.md](2026-10-31/evaluation.md).

| Folder | Design | Plan | Model |
| --- | --- | --- | --- |
| [web-search-gpt54mini](2026-10-31/web-search-gpt54mini/) | Built-in `web_search` tool in each line, no caps: the starting point ([option 1](../option-1-bounded-web-search/README.md)'s script with `--design web-search`) | - | gpt-5.4-mini |
| [bounded-web-search-gpt54mini](2026-10-31/bounded-web-search-gpt54mini/) | [Option 1](../option-1-bounded-web-search/README.md): built-in `web_search`, `max_tool_calls: 2`, low effort, strict schema, search budget | - | gpt-5.4-mini |
| [two-stage-full-gpt54mini](2026-10-31/two-stage-full-gpt54mini/) | [Option 2](../option-2-two-stage-web-iq/README.md): search-then-batch | full | gpt-5.4-mini |
| [two-stage-lean-gpt54mini](2026-10-31/two-stage-lean-gpt54mini/) | [Option 2](../option-2-two-stage-web-iq/README.md): search-then-batch | lean | gpt-5.4-mini |
| [web-search-gpt54](2026-10-31/web-search-gpt54/) | Built-in `web_search` tool in each line, no caps: the starting point | - | gpt-5.4 |
| [two-stage-lean-gpt54](2026-10-31/two-stage-lean-gpt54/) | [Option 2](../option-2-two-stage-web-iq/README.md): search-then-batch | lean | gpt-5.4 |

These runs were made with the code before it was arranged in option folders (`tool_version` 0.1.0 in `run.json`). The prompts haven't changed since: today's code counts the same prompt tokens for the same records (9,527 for option 1's lines; 97,979 estimated for option 2's). The forecasts have: they now assume 350 output tokens a line on gpt-5.4-mini, not 650.

Each folder has:

| File | Contents |
| --- | --- |
| `run.json` | Settings, search plan, forecasts, stage-1 statistics, batch timing, actual usage and cost, and which working files were deleted |
| `results.jsonl` | One line per record: the model's decision (statuses, findings, suggested values, citation URLs), or the error |
| `metrics.jsonl` | One line per record: tokens, cached tokens, reasoning tokens, billed web search requests, search calls, and the parsed answer |

Not published, by design: evidence files and batch input files (they hold Web IQ content, which the terms don't allow us to redistribute), and `state.json` (endpoints and job IDs).

**Removed before publishing.** R22 and R23 are fictional records. Nothing real matches them, so their searches returned pages about unrelated real businesses and people, including a personal profile page, company-registry officer pages and local news. Their findings and summaries are replaced with `[removed before publishing]` and their citation lists are emptied. Their statuses and suggested values are kept, so the accuracy table still reproduces. No personal-page URLs remained in the other records.

The decisions are model output and can be wrong; [labels.jsonl](../samples/brand-owner/labels.jsonl) has the expected answers and sources.
