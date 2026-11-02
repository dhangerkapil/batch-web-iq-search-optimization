# Flex processing with web search or with Web IQ

Run either design on [Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing) instead of Global Batch: [flex_job.py](flex_job.py) sends each line as its own synchronous Responses API request with `service_tier: "flex"`, to a Global Standard deployment of a Flex-capable model. [flex.py](flex.py), in this folder, does the sending.

| Variant | Command, from this folder | What changes |
| --- | --- | --- |
| **Flex with web search** | `python flex_job.py run ... --design bounded-web-search` | [Option 1](../option-1-bounded-web-search/README.md)'s lines, sent one by one |
| **Flex with Web IQ** | `python flex_job.py run ... --design two-stage` | Stage 1 is unchanged: [option 2](../option-2-two-stage-web-iq/README.md)'s Web IQ searches. Only stage 2 changes: its no-tools lines, sent one by one |

**Not measured on the 23-record sample.** Live checks ran both variants on the first 5 sample records, on gpt-6-sol in East US 2: twice on 1 November 2026, before and after this repository's folders were reorganized, and once on 2 November with the one-command code, from a fresh clone. Every line completed with no HTTP 429. All 10 facts matched the labels in every run but one: on 2 November, Flex with web search answered R02's owner `insufficient_evidence` within its two-search budget.
- Flex with web search cost $36.68, $36.41, then $36.63 per 1,000 records. On 2 November each request took a median of 6.3 seconds.
- Flex with Web IQ cost $13.83, $13.85, then $13.87 per 1,000 records, against forecasts of $16.64, $16.65 and $16.65. On 2 November each request took a median of 4.1 seconds, after 6 searches in 8.8 seconds ([09](../docs/09-results-on-the-sample.md#speed)).

Five records check that the code works end to end; they can't rank the variants or compare them with Batch.

## What Flex is

From [Use Flex processing (preview)](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing):

- **Preview**, with "no latency SLA or service SLA".
- **A per-request setting** on the same Global Standard deployment that serves Standard requests, at a "50% discount compared with Standard token rates", on dedicated Flex meters.
- **Synchronous**: "Flex processing isn't the same as the Batch API". A request can get HTTP 429 when Flex capacity isn't available, and there's no automatic fallback to Standard.
- **The deployment's quota** is shared: "Flex and Standard requests use the quota assigned to the Global Standard deployment".
- **Only some models**: the gpt-6 and gpt-5.6 families on Global Standard when we checked, not gpt-5.4 or gpt-5.4-mini. A deployment whose model doesn't support Flex answers HTTP 400. The results for the other designs used gpt-5.4 and gpt-5.4-mini on Global Batch, so a Flex-versus-Batch comparison also changes the model.

## How the code handles it

[flex.py](flex.py), step by step in [WALKTHROUGH.md](WALKTHROUGH.md):

- **Pacing.** It sends `--flex-concurrency` requests at a time, with the SDK's own retries off, so it counts every attempt itself.
- **The ledger.** Every attempt goes into an append-only ledger, `flex-attempts.jsonl` in the work folder: a start row, written to disk before the request leaves, then an outcome row. [report.py](../evaluation/report.py) never copies the ledger.
- **Retries.** HTTP 408, 429 and 5xx get at most 3 attempts per line in a round. Between attempts it waits at least as long as `Retry-After` asks. If the service asks for more than 600 seconds, that wait is taken before the next automatic round instead, for up to an hour.
- **Automatic rounds.** A line that used all its attempts is sent again in a second and a third round, 60 and 300 seconds later. After that it's held, and sent again only with `--retry-failed`. `--no-auto-retry` turns the rounds off.
- **Held.** A request that timed out, lost its connection, or was in flight when a run stopped may still have been processed and billed. Its line is held, never sent again automatically, and sent again only with `--retry-ambiguous`.
- **Stops.** HTTP 400 "Flex not supported", 401, 403 or 404, or a failed Microsoft Entra ID sign-in, stops the job: every other request would fail the same way.
- **Other tiers.** A response processed on another tier is marked failed and isn't priced at Flex rates. Nothing falls back to Standard.
- **Ctrl+C.** The first Ctrl+C sends nothing new, and waits for the requests in flight so their outcomes are recorded. A second Ctrl+C leaves at once, and those lines are held.

## Cost

- **Meters.** Lines are priced at the model's Flex meters, read live from the Azure Retail Prices API, including the prompt-cache-write meter that gpt-6-sol has.
- **Cache writes.** Whether written tokens are billed instead of input or on top of it isn't documented. `run.json` gives the actual cost with writes billed instead of input, plus `total_if_cache_writes_billed_on_top` for the other reading.
- **Forecasts.** A forecast at Flex rates prices all input at the cache-write rate. That errs high if writes are billed instead of input, as the actual cost in `run.json` assumes. If they're billed on top, the forecast can be low: the second Web IQ check forecast $16.65 per 1,000 records, and cost $13.85, or $17.56 on that reading.
- **Failed attempts.** An HTTP 429 that returned no usage is counted, not priced. Timeouts and 5xx errors are counted as unknown charges.

## Benefits and drawbacks

Benefits:
- **Results in seconds per record**, not a batch window.
- **A model without a Global Batch SKU** can run the same lines.
- **The same lines, results and cost report** as Batch: only the way they're sent changes.

Drawbacks:
- **Preview, with no SLA.** Not for a job that needs a guaranteed finish time.
- **Your client paces the work** and shares the deployment's tokens-per-minute quota with any Standard traffic. Size `--flex-concurrency` to it.
- **Fewer models.** The gpt-5.4 family isn't supported, so you may need a different model from your Batch jobs.
- **Prompt-cache writes cost extra on gpt-6-sol.** A short no-tools prompt can pay for writes it never reuses.
- **A wrong `--base-url`, or a network outage, holds every line** rather than stopping the job. Nothing is billed twice, but after fixing it you need `--retry-ambiguous`.

## What you need

- A Global Standard deployment of a Flex-capable model (gpt-6-sol here), and a role that can call it, such as Cognitive Services OpenAI User.
- For Flex with Web IQ, what [option 2](../option-2-two-stage-web-iq/README.md#what-you-need) needs for stage 1: Web IQ, and a Foundry toolbox or the Web IQ key.
- Python 3.10 or later and the packages in [requirements.txt](../requirements.txt), and the Azure CLI for `az login`.

## Run it

Once, from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
az login
cd flex-web-search-and-web-iq
```

Then, from this folder:

```bash
# Flex with web search
python flex_job.py run ../samples/brand-owner/records.jsonl --design bounded-web-search --workdir runs/flex-bounded \
    --deployment <gpt-6-sol Global Standard deployment> --price-model gpt-6-sol \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --flex-concurrency 2

# Flex with Web IQ: forecast at Flex rates first (no calls), then run
python flex_job.py plan ../samples/brand-owner/records.jsonl --design two-stage --plan lean --price-model gpt-6-sol
python flex_job.py run ../samples/brand-owner/records.jsonl --design two-stage --plan lean --workdir runs/flex-two-stage \
    --deployment <gpt-6-sol Global Standard deployment> --price-model gpt-6-sol \
    --base-url https://<resource>.openai.azure.com/openai/v1/ \
    --toolbox-url "<project endpoint>/toolboxes/<name>/mcp?api-version=v1" --flex-concurrency 2 --max-cost 1
```

To resume, run the same command again: completed lines are never sent again. Add `--retry-failed` or `--retry-ambiguous` only when the run tells you to, and only if you accept that a held line may be paid twice. Check the answers with `python ../evaluation/evaluate.py runs/flex-bounded --labels ../samples/brand-owner/labels.jsonl`.

### Exit codes

Every `run` ends with one of these codes ([Run it unattended](../docs/07-deploy-and-run.md#run-it-unattended)). Held records are also listed in `held-records.jsonl`, in the input format, to run again as a new job.

| Code | Meaning |
| --- | --- |
| `0` | Done: every line was answered, or failed for good with an error retrying can't fix (listed in `results.jsonl`) |
| `1` | Needs a person: bad input, a deployment without Flex, sign-in or permissions, a `--max-cost` forecast, or every record held because searches failed with an error such as 401 or 403. Fix it and run the same command again |
| `3` | Finished, with lines held: they used up their attempts in every round (`--retry-failed` sends them again), or their outcome is unknown (`--retry-ambiguous`), or their searches still failed. The working files are kept for the flags |
| `75` | A line is still asked to wait, or `--no-auto-retry` left work for a re-run: run the same command again later |
| `130` | Interrupted with Ctrl+C: run the same command again to resume |

## Settings that matter

All in [shared/settings.toml](../shared/settings.toml). A command-line option overrides a value for one run. If you use your own copy of the file (`SEARCH_THEN_BATCH_SETTINGS`), it needs the `[flex]` section.

| Setting | Line | Default |
| --- | --- | --- |
| `[flex] concurrency` (`--flex-concurrency`) | [141](../shared/settings.toml#L141) | `4` |
| `[flex] max_attempts`: per line, per round | [142](../shared/settings.toml#L142) | `3` |
| `[flex] backoff_base_seconds` and `max_delay_seconds`: our own backoff | [143](../shared/settings.toml#L143), [144](../shared/settings.toml#L144) | `10`, doubling up to `120` |
| `[flex] max_server_wait_seconds`: the longest `Retry-After` waited out between attempts; a longer one is waited out before the next automatic round | [145](../shared/settings.toml#L145) | `600` |
| `[flex] client_timeout_seconds`: Learn suggests 15 minutes | [146](../shared/settings.toml#L146) | `900` |
| `[flex] auto_rounds` and `round_waits_seconds`: rounds in all for lines that used up their attempts, and the pauses before rounds 2 and 3 | [147](../shared/settings.toml#L147), [148](../shared/settings.toml#L148) | `3`, `[60, 300]` |
| `[flex] max_round_wait_seconds`: the longest pause that waits out a `Retry-After` | [149](../shared/settings.toml#L149) | `3600` |
| The gpt-6-sol Flex meters, including cache writes | [60](../shared/settings.toml#L60) | Read live |

## Files

| File | What it does |
| --- | --- |
| [WALKTHROUGH.md](WALKTHROUGH.md) | How a Flex request is sent, attempt by attempt, and both variants step by step |
| [flex_job.py](flex_job.py) | The command: `plan` and `run`, for either design |
| [flex.py](flex.py) | Sends the lines: pacing, attempts, the ledger, held lines, Ctrl+C |
| [../option-1-bounded-web-search/web_search_lines.py](../option-1-bounded-web-search/web_search_lines.py) | Writes the lines for Flex with web search |
| [../option-2-two-stage-web-iq/](../option-2-two-stage-web-iq/README.md) | Stage 1 and the lines for Flex with Web IQ |
| [../shared/](../shared/) | The job runner ([pipeline.py](../shared/pipeline.py)), costs and live prices ([cost.py](../shared/cost.py), [prices.py](../shared/prices.py)) and [settings.toml](../shared/settings.toml) |

## More

- [Use Flex processing (preview)](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing) and [Prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching) on Microsoft Learn.
- [03 - Options compared](../docs/03-options-compared.md#flex-processing-for-either-design) and [07 - Deploy and run](../docs/07-deploy-and-run.md#run-on-flex-instead-of-batch).
