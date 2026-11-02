# Option 1, step by step: bounded web search on Global Batch

This walkthrough follows one sample record through every step of option 1: what goes in, each command, the code that runs, a real line, and what comes out. It assumes no knowledge of the code. Every command runs in this folder, after the setup in the [README](README.md#run-it).

| What changes | Built-in `web_search` tool, capped at two calls, with a search budget in the prompt, low reasoning effort, the record's market as `user_location` and a strict JSON schema |
| --- | --- |
| What doesn't | One Global Batch job per file; the model still searches |
| Code in this folder | [bounded_web_search.py](bounded_web_search.py) (the command), [web_search_lines.py](web_search_lines.py) (one line) |
| Code it shares | [../shared/pipeline.py](../shared/pipeline.py) (the job runner), [../shared/batch.py](../shared/batch.py) (the Batch API), [../shared/workloads/brand_owner.py](../shared/workloads/brand_owner.py) (the sample's prompt and schema) |

## Step 0 - What goes in: one record

The sample is [../samples/brand-owner/records.jsonl](../samples/brand-owner/records.jsonl), 23 records. The first:

```json
{"id":"R01","brand":"Gatorade","market":"US","claimed_owner":"PepsiCo, Inc.","claimed_owner_hq_country":"US"}
```

Each record asks two questions: does `claimed_owner` own `brand` in `market` (`brand_owner`), and is the owner headquartered in `claimed_owner_hq_country` (`owner_hq`)? The `id` becomes the batch line's `custom_id`. Everything specific to these records (the prompt, the schema, which field is the market) is in one class, [BrandOwner](../shared/workloads/brand_owner.py#L68); [10 - Adapt it to your workload](../docs/10-adapt-to-your-workload.md) shows how to write your own.

## Step 1 - Measure the job you run today (optional)

```bash
python ../shared/analyze.py my-batch-output.jsonl --price-model gpt-5.4-mini
```

Run it on the output file of a batch job you already have; nothing leaves your machine except a request for public list prices. [analyze_output](../shared/analyze.py#L67) reads each line's `tool_usage.web_search.num_requests` (the count web search is billed on, [Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)) and its token usage, prices them, and reports the search share of the cost. On an input file, [analyze_input](../shared/analyze.py#L35) reports the tools, caps and prompt size of each line instead. If search is a small share of your cost, a cap won't save much.

## Step 2 - Build the lines

```bash
python bounded_web_search.py build ../samples/brand-owner/records.jsonl --deployment <Global Batch deployment> --out bounded.jsonl
```

It prints `Wrote 23 bounded-web-search lines to bounded.jsonl` and calls nothing in Azure. In order:

1. [main](bounded_web_search.py#L34) defines the `build` command and fixes the design to `bounded-web-search`; `--design web-search` gives the uncapped lines instead.
2. [cmd_build](../shared/pipeline.py#L191) calls [load_records](../shared/pipeline.py#L96), which reads the file and stops if a record has a data problem ([problems](../shared/workloads/brand_owner.py#L73): a missing field, or a market that isn't a two-letter ISO code), unless you pass `--allow-problems`.
3. [build_lines](../shared/pipeline.py#L122) calls [web_search_line](web_search_lines.py#L16) once per record:
   - [line 32](web_search_lines.py#L32) takes the prompts from the workload ([web_search_prompts](../shared/workloads/brand_owner.py#L119)): the task, the decision rules and a three-line search budget.
   - [Line 33](web_search_lines.py#L33) adds the current `web_search` tool, not `web_search_preview`, which Learn calls "supported but not recommended".
   - [Line 44](web_search_lines.py#L44) sets `user_location` to the record's market ([user_country](../shared/workloads/brand_owner.py#L129)), to steer results to that market.
   - [Line 45](web_search_lines.py#L45) sets `max_tool_calls`; the API ignores tool calls beyond it.
   - [Line 46](web_search_lines.py#L46) sets the reasoning effort; reasoning tokens are billed as output tokens.
   - [Line 47](web_search_lines.py#L47) adds the strict JSON schema ([output_format](../shared/workloads/brand_owner.py#L100)).
   - [Line 39](web_search_lines.py#L39) sets `store: false`: the answer is already in the batch output file.
4. [write_jsonl](../shared/util.py#L31) writes UTF-8 without a byte-order mark and with `\n` line endings, as the Batch API requires.

The real line for R01, with the schema shortened:

```json
{"custom_id": "R01", "method": "POST", "url": "/v1/responses",
 "body": {"model": "<Global Batch deployment>",
          "input": [{"role": "system", "content": "You check brand ownership records using web search. Return only the JSON object required by the response schema."},
                    {"role": "user", "content": "Check two facts about the RECORD at the end of this message: ... Search budget: ... RECORD: {\"id\":\"R01\", ...}"}],
          "tools": [{"type": "web_search", "user_location": {"type": "approximate", "country": "US"}}],
          "max_output_tokens": 8000, "store": false, "max_tool_calls": 2, "reasoning": {"effort": "low"},
          "text": {"format": {"type": "json_schema", "name": "brand_owner_check", "strict": true, "schema": {"...": "record_id, brand_owner, owner_hq, all_verified, summary"}}}}}
```

The search budget at the end of the user message, word for word:

```text
Search budget:
- Use at most two web searches: one about who owns the brand in the record's market, one about the claimed owner's headquarters.
- Stop as soon as both facts are settled. If a fact is still unsettled within the budget, use "insufficient_evidence" rather than searching further.
- Cite only URLs you actually relied on.
```

### Where each setting comes from

| In the line | Default, in [settings.toml](../shared/settings.toml) | Command-line option ([cli.py](../shared/cli.py#L34)) | Written by |
| --- | --- | --- | --- |
| `max_tool_calls: 2` | `[defaults] max_tool_calls`, [line 90](../shared/settings.toml#L90) | `--max-tool-calls` | [line 45](web_search_lines.py#L45) |
| `reasoning.effort: low` | `[defaults] reasoning_effort`, [line 88](../shared/settings.toml#L88) | `--reasoning-effort` | [line 46](web_search_lines.py#L46) |
| `max_output_tokens: 8000` | `[defaults] max_output_tokens`, [line 89](../shared/settings.toml#L89) | `--max-output-tokens` | [line 38](web_search_lines.py#L38) |
| `user_location.country` | The record's `market` | - | [line 44](web_search_lines.py#L44) |
| The search budget and the schema | In code: [brand_owner.py](../shared/workloads/brand_owner.py#L119) | - | [line 32](web_search_lines.py#L32), [line 47](web_search_lines.py#L47) |

To check a file before submitting it, `python ../shared/analyze.py bounded.jsonl` reports, for the 23 sample lines: one deployment, `web_search` on every line, `max_tool_calls` 2, effort low, a JSON schema, and 9,527 prompt tokens in all (412 to 419 a line).

## Step 3 - Run the job

```bash
python bounded_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/bounded \
    --base-url https://<resource>.openai.azure.com/openai/v1/ \
    --deployment <Global Batch deployment> --price-model gpt-5.4-mini
```

[cmd_run](../shared/pipeline.py#L205), in order:

1. **Checks first.** There's no `--max-cost` option: the model decides how many searches to run, so no forecast can bound the cost. (Through [flex_job.py](../flex-web-search-and-web-iq/flex_job.py), which has the option, `cmd_run` refuses it for this design, [line 208](../shared/pipeline.py#L208).) A work folder that already finished is reported as done, and nothing is sent ([line 217](../shared/pipeline.py#L217)).
2. **Prices.** [run_prices](../shared/pipeline.py#L45) reads list prices once, live from the Azure Retail Prices API, and saves them as `prices-used.json`, so a resumed job is priced the same way ([line 225](../shared/pipeline.py#L225)).
3. **Lines.** It builds the lines as in step 2 and writes `batch-input.jsonl` ([line 298](../shared/pipeline.py#L298)).
4. **Submit.** [submit](../shared/batch.py#L62) ([line 312](../shared/pipeline.py#L312)):
   - [check_lines](../shared/batch.py#L45) refuses a file the Batch API would reject: more than one deployment or endpoint, repeated `custom_id`s, or more than 100,000 lines. It also refuses a file over 200 MB.
   - It uploads the file with a 14-day expiry, a backstop in case clean-up never runs.
   - It waits until the service has processed the file, then creates the job.
   - The work folder's `state.json` records the endpoint and the job ([line 313](../shared/pipeline.py#L313)).
5. **Wait.** [wait](../shared/batch.py#L96) polls every `--poll-seconds` (60 by default) and warns if nothing completes or fails for `--stall-minutes`; `--cancel-on-stall` cancels the job instead ([line 322](../shared/pipeline.py#L322)).
6. **Collect.** [collect](../shared/batch.py#L157) reads the output file and turns each response into one row of `metrics.jsonl` with [response_metrics](../shared/batch.py#L117): tokens, billed web search requests, search calls, and whether the answer is valid JSON ([line 323](../shared/pipeline.py#L323)).
7. **Clean up.** It deletes the job's input, output and error files in Azure OpenAI ([line 324](../shared/pipeline.py#L324)).
8. **A job that didn't complete.** If it ended `failed`, `expired` or `cancelled`, the completed lines were billed, so they're kept in `kept-lines.jsonl`. Running the same command again resubmits only the other lines ([line 325](../shared/pipeline.py#L325)).
9. **Results.** It writes `results.jsonl`, one answer per record ([line 344](../shared/pipeline.py#L344)), prices the job with [actual](../shared/cost.py#L103) ([line 346](../shared/pipeline.py#L346)), and deletes the local working files ([line 348](../shared/pipeline.py#L348)). `run.json` keeps the settings, timings and cost.

What came out for the 23 sample records on gpt-5.4-mini, 31 October 2026 ([run.json](../results/2026-10-31/bounded-web-search-gpt54mini/run.json)):

- the job took 7.6 minutes, and all 23 lines completed with valid JSON;
- 2.52 billed web search requests per record, from 1.48 search calls;
- 10,230 input and 530 output tokens per record;
- $0.93 in all: **$40.33 per 1,000 records**, 88% of it search.

R01 used 8,808 input tokens, 358 output tokens and 2 billed requests. Its answer, shortened:

```json
{"record_id": "R01",
 "brand_owner": {"status": "verified", "finding": "Gatorade is a PepsiCo brand ... PepsiCo owns the brand in the US market.", "suggested_value": null,
                 "citations": ["https://www.pepsico.com/brands/gatorade?utm_source=openai", "..."]},
 "owner_hq": {"status": "verified", "finding": "... placing its global headquarters in the United States.", "suggested_value": null, "citations": ["..."]},
 "all_verified": true, "summary": "..."}
```

## Step 4 - Check the answers

```bash
python ../evaluation/evaluate.py runs/bounded --labels ../samples/brand-owner/labels.jsonl
```

[score_run](../evaluation/evaluate.py#L19) compares each fact with the labels in [labels.jsonl](../samples/brand-owner/labels.jsonl) and prints one row per run: cost, search share, facts and records right, and invalid or missing answers. Pass several run folders to compare them. On the 23 sample records, accuracy was at a similar level for every option, and 23 records are too few to rank them: measure on your own labeled records ([08](../docs/08-evaluate-quality.md)). One behavior to expect from the cap: a fact the model can't settle within two searches is answered `insufficient_evidence`, as the search budget tells it to. On the sample, that happened for two headquarters facts.

## Step 5 - Share the results

```bash
python ../evaluation/report.py runs/bounded --out ../results/my-run
```

[cmd_report](../evaluation/report.py#L74) copies only `run.json`, `results.jsonl`, `metrics.jsonl` and `prices-used.json`. [check_text](../evaluation/report.py#L37) refuses them if they contain an endpoint, a local path or a job ID. It also removes citation URLs that point to pages about individuals.

## If something goes wrong

| Situation | What to do |
| --- | --- |
| `Refusing to submit: ...` | Fix what it names: one deployment per file, unique record ids, at most 100,000 lines and 200 MB per file |
| The command was interrupted while the job ran | Run the same command again: it finds the job in `state.json` and waits for it, without submitting again |
| The job ended `failed`, `expired` or `cancelled` | Run the same command again: the completed lines are kept, and only the others are resubmitted |
| `unrecognized arguments: --max-cost` | Option 1 has no cost cap: the model decides the search count, so only [option 2](../option-2-two-stage-web-iq/README.md) can forecast before spending |
| `Couldn't read list prices from the Azure Retail Prices API` | No internet access, or a meter in settings.toml was renamed: pass `--prices <file>` with a table saved by `python ../shared/prices.py --save <file>` |
| Many answers are `insufficient_evidence` | The budget is too tight for those records: send them to a second pass with a larger `--max-tool-calls`, or to a person |

## Option 1 or option 2?

- **Option 1** is a change to the lines only. Choose it when you can't add a service now, and `analyze` shows the model making many searches per line.
- **[Option 2](../option-2-two-stage-web-iq/README.md)** cost a quarter as much on the sample, and forecasts before spending. Choose it when the searches can be built from each record's fields.
- They combine: option 2 for most records, and option 1 as the second pass for records whose research is multi-hop.

## Sources

- Microsoft Learn: [Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search) (the tool, `user_location`, billing on `num_requests`); [Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch); [Structured outputs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs); [Reasoning models](https://learn.microsoft.com/azure/foundry/openai/how-to/reasoning#reasoning-effort).
- Measured: [results/2026-10-31/](../results/2026-10-31/), [09 - Results on the sample](../docs/09-results-on-the-sample.md).
