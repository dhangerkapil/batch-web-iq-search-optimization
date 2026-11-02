# Flex, step by step: Flex processing with web search or with Web IQ

This walkthrough follows sample record R01 through both Flex variants: each command, the code that runs, how one request is sent and recorded, and what came out. It assumes no knowledge of the code. Every command runs in this folder, after the setup in the [README](README.md#run-it).

| What changes | Each line is sent as its own synchronous Responses API request with `service_tier: "flex"`, to a Global Standard deployment of a Flex-capable model, instead of being uploaded in one Global Batch job |
| --- | --- |
| What doesn't | The lines. Flex with web search sends [option 1](../option-1-bounded-web-search/WALKTHROUGH.md)'s lines; Flex with Web IQ runs [option 2](../option-2-two-stage-web-iq/WALKTHROUGH.md)'s stage 1 and sends its no-tools lines |
| Code in this folder | [flex_job.py](flex_job.py) (the command), [flex.py](flex.py) (sending, retries and the attempt ledger) |
| Code it shares | [../shared/pipeline.py](../shared/pipeline.py) (the job runner), option 1's [web_search_lines.py](../option-1-bounded-web-search/web_search_lines.py), option 2's [webiq.py](../option-2-two-stage-web-iq/webiq.py) and [two_stage_lines.py](../option-2-two-stage-web-iq/two_stage_lines.py), [../shared/cost.py](../shared/cost.py) |

## Before you start

- **A Flex-capable model on Global Standard.** gpt-6-sol here. When we checked, Flex supported the gpt-6 and gpt-5.6 families, not gpt-5.4 or gpt-5.4-mini; a deployment whose model doesn't support Flex answers HTTP 400 ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)). So a Flex-versus-Batch comparison also changes the model.
- **Preview, with no SLA.** A request can get HTTP 429 when Flex capacity is short, and nothing falls back to Standard.
- **Your client paces the work.** Flex and Standard requests share the deployment's quota, so size `--flex-concurrency` to its tokens-per-minute quota.

## Step 0 - What goes in: the same record

```json
{"id":"R01","brand":"Gatorade","market":"US","claimed_owner":"PepsiCo, Inc.","claimed_owner_hq_country":"US"}
```

`--design` chooses the variant, and it's required ([main](flex_job.py#L41)):

| `--design` | Variant | Lines |
| --- | --- | --- |
| `bounded-web-search` | Flex with web search | [web_search_line](../option-1-bounded-web-search/web_search_lines.py#L16): the `web_search` tool, `max_tool_calls` 2, the search budget |
| `two-stage` | Flex with Web IQ | Stage 1 with Web IQ ([run_searches](../option-2-two-stage-web-iq/webiq.py#L447)), then [two_stage_line](../option-2-two-stage-web-iq/two_stage_lines.py#L40): no tools, the evidence in the prompt |

## Step 1 - Forecast at Flex rates (no cost)

```bash
python flex_job.py plan ../samples/brand-owner/records.jsonl --design two-stage --plan lean --price-model gpt-6-sol
```

The output for the 23 sample records:

```text
Plan lean: 26 unique searches for 23 records = 1.13 per record {'brand_owner': 20, 'owner_hq': 6}; sharing saves 17 of 43
Forecast before searching at Flex rates (gpt-6-sol): 23 records, 26 Web IQ calls, 97,979 input tokens (estimate), 700 output tokens/record assumed -> $0.3850 (Web IQ $0.1820 + input $0.1225 + output $0.0805) = $16.74 per 1,000 records
```

It's option 2's plan and forecast ([option 2, step 1](../option-2-two-stage-web-iq/WALKTHROUGH.md#step-1---plan-the-searches-and-forecast-the-cost-no-cost)), priced at the model's Flex meters, read live from the Azure Retail Prices API. gpt-6-sol has a prompt-cache-write meter, $1.25 per million tokens against $1.00 for input, so the forecast prices all input at $1.25 ([forecast](../shared/cost.py#L26)). With `--design bounded-web-search`, `plan` prints only the prompt tokens (9,527): the model decides how many searches to run, so no forecast can bound the cost.

## Step 2 - Run Flex with web search

```bash
python flex_job.py run ../samples/brand-owner/records.jsonl --design bounded-web-search --workdir runs/flex-bounded \
    --deployment <gpt-6-sol Global Standard deployment> --price-model gpt-6-sol \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --flex-concurrency 2
```

[cmd_run](../shared/pipeline.py#L287), the same runner as Global Batch, in order:

1. **Checks.** It refuses `--max-cost` for this design ([line 293](../shared/pipeline.py#L293)). It refuses a work folder that holds a Batch job ([line 307](../shared/pipeline.py#L307)), and a finished job is reported as done, with nothing sent ([line 302](../shared/pipeline.py#L302)).
2. **Prices.** It reads list prices live and saves them as `prices-used.json`. It stops before anything is sent if the model has no Flex prices ([line 315](../shared/pipeline.py#L315)).
3. **Lines.** It builds option 1's lines ([line 396](../shared/pipeline.py#L396)) and writes `batch-input.jsonl`.
4. **Send.** Instead of uploading the file, it calls [run_flex](../shared/pipeline.py#L754), which hands the lines to [run_lines](flex.py#L541). Step 3 shows how each line is sent.

## Step 3 - How one line is sent and recorded

In [run_lines](flex.py#L541):

1. **Checks.** Every `custom_id` must be unique, and every line must be a Responses API line.
2. **The request.** [flex_body](flex.py#L72) copies the line's body, sets the model to your deployment and adds `service_tier: "flex"` ([line 76](flex.py#L76)). [request_sha256](flex.py#L80) fingerprints the exact request, and every ledger row records the fingerprint.
3. **What each line needs.** It reads the attempt ledger, `flex-attempts.jsonl` in the work folder ([line 561](flex.py#L561)). For each line, [line_state](flex.py#L196) decides:

   | State | Sent by this run? |
   | --- | --- |
   | `completed`: it has a Flex response | Never again |
   | `pending`: not sent yet, or a round cut short | Yes |
   | `held`: a request whose outcome is unknown | Only with `--retry-ambiguous` |
   | `used_up`: its attempts are used up | In an automatic round, until it has had 3 rounds ([_auto_line](flex.py#L514)); after that, only with `--retry-failed` |
   | `failed`: an error retrying can't fix | Never again |

   If a completed line was sent with a different request than the one it would send now, for example after a settings change, it warns and keeps the result ([line 562](flex.py#L562)).
4. **Pacing.** [_send](flex.py#L479) sends `--flex-concurrency` lines at a time (4 by default). The client has the SDK's own retries turned off, so every attempt is counted here, and it waits up to 900 seconds for an answer.
5. **Before each request,** [send_line](flex.py#L349) writes a start row to the ledger ([line 355](flex.py#L355)), and [write](flex.py#L310) forces it to disk with `fsync` ([line 314](flex.py#L314)). If the process dies while the request is in flight, the ledger still shows that it was sent.
6. **A response.** [_answered](flex.py#L376) writes an outcome row. A response processed on the `flex` tier completes the line, with its usage priced at Flex rates. A response on another tier fails the line and isn't priced at Flex rates; nothing falls back to Standard.
7. **An error.** [_failed](flex.py#L398) writes an outcome row and decides what happens next:

   | Error | What happens |
   | --- | --- |
   | HTTP 400 "Flex not supported", 401, 403 or 404, or no Microsoft Entra ID token | The job stops: every other request would fail the same way ([line 408](flex.py#L408)) |
   | A client timeout or a dropped connection | The line is **held**: the request may still have been processed and billed, so resending it automatically could pay twice ([line 412](flex.py#L412)) |
   | HTTP 408, 429, 500, 502, 503 or 504 | Retried, up to 3 attempts in a round. The wait is the longer of `Retry-After` and our own backoff: 10 seconds, doubling, with jitter, up to 120 ([line 428](flex.py#L428)). If the service asks for more than 600 seconds, the line waits for the next automatic round ([line 421](flex.py#L421)). After 3 attempts, the line is `used_up` |
   | Another 4xx | The line fails; not retried |
   | Not an HTTP error | The line is held and the job stops |

8. **Ctrl+C.** The first one stops new requests and waits for those in flight, so their outcomes are recorded. A second one leaves at once, and those lines are held.

The real ledger rows for R01 (1 November 2026), with the fingerprint shortened. The outcome row also holds the response's metrics and answer, left out here:

```json
{"event":"start","custom_id":"R01","round":1,"attempt":1,"request_sha256":"01f39fcc6fe8...","at":"2026-11-01T23:49:18+00:00"}
{"event":"outcome","custom_id":"R01","round":1,"attempt":1,"request_sha256":"01f39fcc6fe8...","at":"2026-11-01T23:49:28+00:00",
 "http_status":200,"error":null,"service_tier":"flex",
 "usage":{"input_tokens":8261,"cached_tokens":0,"cache_write_tokens":4634,"output_tokens":192,"reasoning_tokens":55,"web_search_requests":2},
 "billing":"measured","result":"completed","seconds":10.6}
```

`billing` says how each attempt is counted ([summarize](flex.py#L259)): `measured` (a Flex response, priced from its usage), `not_billed` (HTTP 429, no usage), `rejected_no_usage` (another 4xx), `unknown` (408, 5xx, a timeout, a dropped connection or no outcome: counted, not priced) or `other_tier`.

## Step 4 - Results and cost

[run_flex](../shared/pipeline.py#L754) reads the ledger back and writes `metrics.jsonl` and `results.jsonl` in the same shape as a Batch job, then prices the job at the model's Flex rates. Before that, [run_lines](flex.py#L541) sends the lines that used up their attempts on HTTP 408, 429 or 5xx again, in a second and a third round, 60 and 300 seconds later, and waits out a `Retry-After` longer than 600 seconds before the next round, for up to an hour ([line 584](flex.py#L584), [round_pause](flex.py#L525)). Lines whose outcome is unknown are never sent again automatically. Then:

- **If the run stopped** (Ctrl+C, or an error that stops the job), `run.json` says `flex stopped` ([line 779](../shared/pipeline.py#L779)), and it exits with 130 or 1. Run the same command again to continue.
- **If a line is still asked to wait**, it says `flex incomplete` and exits with 75 ([line 785](../shared/pipeline.py#L785)): run the same command again later.
- **If lines are held**, used up in every round or with an unknown outcome, it says `flex incomplete`, lists them in `held-records.jsonl`, keeps the working files for `--retry-failed` and `--retry-ambiguous`, and exits with 3 ([line 791](../shared/pipeline.py#L791)).
- **Otherwise** the job is done: it deletes the working files, keeps the ledger, and exits with 0 (or 3, if records were held because their searches failed).

**Cache writes.** gpt-6-sol bills prompt-cache writes, and it isn't documented whether written tokens are billed instead of input or on top of it. `run.json` gives the actual cost with writes billed instead of input, and `total_if_cache_writes_billed_on_top` for the other reading.

What came out for the first 5 sample records on gpt-6-sol, 1 November 2026, 2 requests at a time:

| | Flex with web search | Flex with Web IQ |
| --- | --- | --- |
| Lines | 5 of 5 completed on the first attempt, no HTTP 429 | 5 of 5 completed on the first attempt, no HTTP 429 |
| Time sending | 22.4 seconds; 7.5 per request | 11.9 seconds; 3.9 per request |
| Searches | 2 billed web search requests a record, from 1 search call | 6 Web IQ calls for 5 records, over REST, in 3.4 seconds |
| Tokens per record | 9,065 input (2,093 of them cache writes), 221 output | 3,716 input (3,713 of them cache writes), 160 output |
| Forecast | None: the model decides the searches | $16.65 per 1,000 records before sending |
| **Cost** | **$36.41 per 1,000 records**; $38.50 if cache writes are billed on top | **$13.85 per 1,000 records**; $17.56 if cache writes are billed on top |

All 10 facts in each run matched the labels. Running each command again sent nothing and reported the job done. Five records check that the code works end to end; they can't rank the variants or compare them with Batch, which also used different models ([09](../docs/09-results-on-the-sample.md)).

## Step 5 - Run Flex with Web IQ

```bash
python flex_job.py run ../samples/brand-owner/records.jsonl --design two-stage --plan lean --workdir runs/flex-two-stage \
    --deployment <gpt-6-sol Global Standard deployment> --price-model gpt-6-sol \
    --base-url https://<resource>.openai.azure.com/openai/v1/ \
    --toolbox-url "https://<resource>.services.ai.azure.com/api/projects/<project>/toolboxes/<name>/mcp?api-version=v1" \
    --flex-concurrency 2 --max-cost 1
```

[cmd_run](../shared/pipeline.py#L287) runs option 2 exactly as on Global Batch, up to the last forecast:

1. the forecast before searching, at Flex rates, and the `--max-cost` stop before any search ([line 349](../shared/pipeline.py#L349));
2. stage 1 with Web IQ, through the toolbox or REST, retried and resumable ([line 355](../shared/pipeline.py#L355); [option 2, step 3](../option-2-two-stage-web-iq/WALKTHROUGH.md#step-3---stage-1-run-the-searches));
3. the no-tools lines, and the exact forecast before anything is sent, with its `--max-cost` stop ([line 388](../shared/pipeline.py#L388)).

Then it sends the lines with [run_flex](../shared/pipeline.py#L754), as in steps 3 and 4. When the job is done, it deletes the evidence file with the other working files.

## Step 6 - Resume, retry and check

- **Resume.** Run the same command again. Completed lines are never sent again, finished searches aren't repeated, and a round cut short continues.
- **`--retry-failed`** sends the `used_up` lines again, in a new round of 3 attempts, once they've had their automatic rounds. Use it once the cause has passed, for example after a long spell of HTTP 429s.
- **`--retry-ambiguous`** sends the `held` lines again. Each may have been processed and billed already, so add it only if you accept paying twice for those lines.
- **Check the answers:** `python ../evaluation/evaluate.py runs/flex-bounded --labels ../samples/brand-owner/labels.jsonl`, as in [option 1, step 4](../option-1-bounded-web-search/WALKTHROUGH.md#step-4---check-the-answers).
- **Share the results:** `python ../evaluation/report.py runs/flex-bounded --out ../results/my-run`. It never copies the ledger.

## If something goes wrong

| Message or situation | What to do |
| --- | --- |
| `Stopped: deployment '...' doesn't support Flex processing` | Use a Global Standard deployment of a Flex-capable model, such as gpt-6-sol |
| `Stopped: HTTP 404 for deployment '...'` | Check `--deployment` and `--base-url` |
| `Stopped: HTTP 401` or `403`, or `couldn't get a Microsoft Entra ID token` | Run `az login`, and check that you have a role that can call the deployment, such as Cognitive Services OpenAI User |
| Many HTTP 429s, then lines `used_up` after three rounds (exit code 3) | Flex capacity was short or your quota is too small: lower `--flex-concurrency`, then re-run with `--retry-failed` later |
| `the service asked to wait ... longer than max_server_wait_seconds`, then exit code 75 | Still asked to wait after the automatic rounds: run the same command again after the time it prints |
| Every line held | Usually a wrong `--base-url` or a network outage. Fix it, then re-run with `--retry-ambiguous`; nothing is billed twice for a request that never reached the service |
| `holds a Batch job (state.json or kept-lines.jsonl)` | Finish that job with the option's own script, or use a new `--workdir` |
| `WARNING: ... were sent with a different request` | Results already received are kept. To send everything with the new settings, use a new `--workdir` |

## Flex or Global Batch?

- **Choose Flex** when you want results in seconds per record rather than a batch window, or a model without a Global Batch deployment.
- **Choose Global Batch** for scheduled jobs: it has no client-side pacing, the gpt-5.4 family is available, and the job doesn't depend on a preview feature. On the same 5 records, [option 2](../option-2-two-stage-web-iq/README.md) on Global Batch with gpt-5.4-mini cost $9.95 per 1,000; Flex with Web IQ on gpt-6-sol cost $13.85.
- Either way, the search choice matters more than the processing mode: Web IQ evidence cost about a quarter as much as model-driven web search on Global Batch, and 38% as much on Flex.

## Sources

- Microsoft Learn: [Use Flex processing (preview)](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing) (`service_tier`, the 50% discount, HTTP 429, shared quota, supported models, the 15-minute timeout); [Prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching) (cache writes on GPT-5.6 and later); [Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search).
- Measured: the live checks above, and [README](README.md) for the earlier run.
