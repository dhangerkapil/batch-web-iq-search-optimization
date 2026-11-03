# Option 1: bounded web search (built-in web search, capped)

Keep the built-in `web_search` tool in each batch line, but cap it and tell the model when to stop. The pipeline doesn't change and there's no new service: only the lines do. This folder holds the code that writes those lines and the command that runs them.

| On the 23-record sample, gpt-5.4-mini Global Batch | Model-driven web search | Bounded web search |
| --- | --- | --- |
| Billed Bing requests per record | 2.87 | 2.52 |
| Cost per 1,000 records | $43.12 | $40.33 (-6%) |
| Facts right (of 46) | 42 | 42 |

Measured on 31 October 2026 at list prices ([09](../docs/09-results-on-the-sample.md), [raw results](../results/2026-10-31/bounded-web-search-gpt54mini/)). The saving depends on how much your model searches by default: a cap only removes searches the model was making, and gpt-5.4-mini searched little on this sample. A model that searches more leaves more to cut, so measure your own job first with `analyze` (below).

## What changes in each line

[web_search_lines.py](web_search_lines.py) (`web_search_line` with `bounded=True`) sets:

- the current `web_search` tool, not `web_search_preview`, which is "supported but not recommended" ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)), with `user_location` set to the record's market;
- `max_tool_calls: 2`: the API ignores further tool calls in the response;
- a search budget written into the prompt: how much to search and when to stop;
- `reasoning.effort: low`, set in every line: without it, gpt-5.4 and gpt-5.4-mini run at their default, `none`;
- a strict JSON schema ([Structured outputs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs));
- `max_output_tokens` as a guardrail: you pay for the tokens generated, not the cap.

[WALKTHROUGH.md](WALKTHROUGH.md) shows a real line, field by field.

## Benefits and drawbacks

Benefits:
- No new service, contract or stage: a change to the lines only, and Global Batch accepted every setting.
- The cap stops the long search chains a model can run when nothing tells it to stop.

Drawbacks:
- **Still model-driven.** The cap limits tool calls, not the Bing requests inside them: on the sample, 1.48 call items per record still produced 2.52 billed requests.
- **No forecast before running.** `run` doesn't accept `--max-cost`, because the model decides how many searches to run.
- **A tight budget can end in `insufficient_evidence`.** Two `owner_hq` facts did on the sample: within two searches, the model didn't find a page that stated the headquarters.

## What you need

- A Global Batch deployment on an Azure OpenAI (Microsoft Foundry) resource, and a role that can upload files and create batch jobs on it ([07 - Deploy and run](../docs/07-deploy-and-run.md#3-create-the-azure-resources)).
- Python 3.10 or later and the packages in [requirements.txt](../requirements.txt), and the Azure CLI for `az login`.

## Run it

Once, from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
az login
cd option-1-bounded-web-search
```

Then, from this folder:

```bash
# What drives the cost of a batch job you already ran; your files stay local
python ../shared/analyze.py my-batch-output.jsonl --price-model gpt-5.4-mini

# Write the bounded lines without submitting, to read them or submit them your own way
python bounded_web_search.py build ../samples/brand-owner/records.jsonl --deployment <Global Batch deployment> --out bounded.jsonl

# Or run a whole job: build, submit, wait, collect, price and clean up; run the same command again to resume
python bounded_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/bounded \
    --base-url https://<resource>.openai.azure.com/openai/v1/ \
    --deployment <Global Batch deployment> --price-model gpt-5.4-mini

# Check the answers against the sample's labels
python ../evaluation/evaluate.py runs/bounded --labels ../samples/brand-owner/labels.jsonl
```

`--design web-search` builds or runs the same lines without the caps, to measure a job as it runs today.

`run` is built to run unattended. It splits a job above the batch file limits into several batch jobs, waits while the deployment's enqueued-token quota is full (whether the region refuses the create at once or the job fails after validating), waits until every job ends (or for `--max-wait-minutes`), and sends lines that failed with HTTP 408, 429 or 5xx, or that an expired job never processed, again in up to three rounds. Lines it still can't answer are held and listed in `held-records.jsonl`, in the input format, to run again as a new job. `--no-auto-retry` turns the rounds off. [Run it unattended](../docs/07-deploy-and-run.md#run-it-unattended) has the details, and every `run` ends with one of these exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Done: every line was answered, or failed for good with an error retrying can't fix (listed in `results.jsonl`) |
| `1` | Needs a person: bad input, sign-in or permissions, or a batch job that failed validation. Fix it and run the same command again |
| `3` | Finished, with records held after the automatic rounds: run `held-records.jsonl` as a new job later |
| `75` | Still running, waiting for quota, or a create whose answer was lost couldn't be checked yet: run the same command again later |
| `130` | Interrupted with Ctrl+C: run the same command again to resume |

## Settings that matter

All in [shared/settings.toml](../shared/settings.toml). A command-line option overrides a value for one run.

| Setting | Line | Default |
| --- | --- | --- |
| `[defaults] max_tool_calls` (`--max-tool-calls`) | [90](../shared/settings.toml#L90) | `2` |
| `[defaults] reasoning_effort` (`--reasoning-effort`) | [88](../shared/settings.toml#L88) | `"low"`. Keep it set: gpt-5.4 and gpt-5.4-mini default to `none` ([03](../docs/03-options-compared.md#option-1---bound-the-built-in-web-search)) |
| `[defaults] max_output_tokens` (`--max-output-tokens`) | [89](../shared/settings.toml#L89) | `8000` |
| `[batch] max_rounds`: rounds in all for lines that failed with HTTP 408, 429 or 5xx, or weren't processed | [133](../shared/settings.toml#L133) | `3` |
| `[batch] part_bytes`: where a job above the file limits is split | [128](../shared/settings.toml#L128) | 180 MB (and 100,000 lines) |
| `[batch] quota_wait_minutes`: how long to wait while the enqueued-token quota is full | [134](../shared/settings.toml#L134) | `1440` |
| `[batch] max_wait_minutes` (`--max-wait-minutes`): when to stop waiting, with exit code 75 | [132](../shared/settings.toml#L132) | `0`: until every job ends |
| The Bing meter that prices the searches | [70](../shared/settings.toml#L70) | Grounding with Bing, Search Transactions, read live from the Azure Retail Prices API |

## Files

| File | What it does |
| --- | --- |
| [WALKTHROUGH.md](WALKTHROUGH.md) | Every step, with the code behind it and a real line |
| [bounded_web_search.py](bounded_web_search.py) | The command: `plan`, `build` and `run` |
| [web_search_lines.py](web_search_lines.py) | Writes one line in which the model searches, capped or not |
| [../shared/](../shared/) | The code every option uses: the job runner ([pipeline.py](../shared/pipeline.py)), the Batch API ([batch.py](../shared/batch.py)), costs and live prices ([cost.py](../shared/cost.py), [prices.py](../shared/prices.py)), the sample workload's prompt and schema ([workloads/brand_owner.py](../shared/workloads/brand_owner.py)) and [settings.toml](../shared/settings.toml) |

## Run it on Flex instead of Batch

[flex-web-search-and-web-iq/](../flex-web-search-and-web-iq/README.md) sends these same lines with Flex processing: `python flex_job.py run ... --design bounded-web-search`, from that folder.

## More

- [03 - Options compared](../docs/03-options-compared.md#option-1---bound-the-built-in-web-search): why it's attractive, what we measured, when to choose it.
- [01 - Why web search in batch costs more](../docs/01-why-web-search-in-batch-costs-more.md): how search is billed, and how to measure your own job.
- [Option 2, two-stage with Web IQ](../option-2-two-stage-web-iq/README.md): the lower-cost design, when the searches can be built from each record's fields.
