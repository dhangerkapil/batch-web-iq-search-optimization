# 09 - Results on the sample

Six batch jobs ran the same 23 records ([samples/brand-owner/records.jsonl](../samples/brand-owner/records.jsonl)) on Global Batch deployments in East US 2 on 31 October 2026. They're priced at the list prices of 31 October 2026, saved in [tests/fixtures/prices-2026-10-31.json](../tests/fixtures/prices-2026-10-31.json), and scored against [labels.jsonl](../samples/brand-owner/labels.jsonl). Every number in the tables of those runs comes from the files in [results/2026-10-31](../results/2026-10-31/): one folder per run with `run.json` (settings, plan, forecasts, actual usage and cost), `results.jsonl` (one decision per record) and `metrics.jsonl` (per-line usage), plus [evaluation.md](../results/2026-10-31/evaluation.md). [Speed](#speed) and [the one-command runs of 2 November](#the-one-command-runs-live-2-november-2026) say where their numbers come from.

**Twenty-three records show cost mechanics. They're too few to rank accuracy**: one record is about 4 points of record accuracy. Treat the accuracy columns as "no worse on this set", not as a benchmark.

## Cost and accuracy

| Run | Model | Search per record | Search $ | Tokens $ | **Total $ per 1,000 records** | vs web search, same model | Facts right (of 46) | Records right (of 23) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [Model-driven web search](../results/2026-10-31/web-search-gpt54mini/run.json) | gpt-5.4-mini | 2.87 Bing requests | 40.17 | 2.94 | **43.12** | - | 42 | 19 |
| [Bounded web search](../results/2026-10-31/bounded-web-search-gpt54mini/run.json) | gpt-5.4-mini | 2.52 Bing requests | 35.30 | 5.03 | **40.33** | -6% | 42 | 19 |
| [Search-then-batch, full plan](../results/2026-10-31/two-stage-full-gpt54mini/run.json) | gpt-5.4-mini | 1.26 Web IQ calls | 8.83 | 1.67 | **10.49** | -76% | 44 | 21 |
| [Search-then-batch, lean plan](../results/2026-10-31/two-stage-lean-gpt54mini/run.json) | gpt-5.4-mini | 1.13 Web IQ calls | 7.91 | 1.87 | **9.78** | -77% | 44 | 21 |
| [Model-driven web search](../results/2026-10-31/web-search-gpt54/run.json) | gpt-5.4 | 2.52 Bing requests | 35.30 | 9.43 | **44.73** | - | 44 | 21 |
| [Search-then-batch, lean plan](../results/2026-10-31/two-stage-lean-gpt54/run.json) | gpt-5.4 | 1.13 Web IQ calls | 7.91 | 6.16 | **14.08** | -69% | 45 | 22 |

Totals can differ from the sum of the parts by a cent because of rounding.

**What the runs had in common**: 23 lines each, all completed, 0 failed, all valid JSON; 7.1-9.4 minutes from creation to completion; a status outside the allowed values in none of them. The web-search runs used the built-in `web_search` tool. The two-stage runs reached Web IQ through a Microsoft Foundry toolbox with Microsoft Entra ID, at 3 queries per second, 5 results of up to 1,200 characters per search.

## Tokens and searches per record

| Run | Input tokens (cached) | Output tokens (reasoning) | Tool calls | Billed searches |
| --- | --- | --- | --- | --- |
| Web search, gpt-5.4-mini | 8,713 (2,905) | 292 (68) | 1.13 `web_search_call` items | 2.87 Bing requests |
| Bounded web search, gpt-5.4-mini | 10,230 (0) | 530 (354) | 1.48 | 2.52 |
| Two-stage, full, gpt-5.4-mini | 3,731 (1,213) | 301 (82) | none | 1.26 Web IQ calls |
| Two-stage, lean, gpt-5.4-mini | 3,534 (234) | 278 (68) | none | 1.13 |
| Web search, gpt-5.4 | 8,751 (3,384) | 304 (74) | 1.09 | 2.52 Bing requests |
| Two-stage, lean, gpt-5.4 | 3,534 (690) | 336 (83) | none | 1.13 |

What these show:

- **Billed requests aren't tool calls.** The unbounded web-search runs were billed 2.3-2.5 Bing requests per `web_search_call` item, and the bounded run 1.7, as the documentation warns ([Web search billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)).
- **The cap didn't bound the requests.** With `max_tool_calls: 2`, the bounded run stayed at or under 2 call items per record (1.48 on average) but was billed 2.52 requests per record.
- **Planned search used less than half the input.** 3,534-3,731 input tokens per record against 8,713-10,230 when the model searched.
- **Caching varied.** 7-33% of input tokens were billed as cached in the two-stage runs, and 33-39% in the unbounded web-search runs, whose search loop re-reads its own context. Treat caching as a bonus, not a plan.
- **Stage 1 took longer than its rate limit explains.** 26-29 searches took 33-80 seconds per run; at 3 queries per second per run, about 10 seconds was expected. There were 0 errors, 5 results per search and no `premium` results (`stage1` in each `run.json`). These runs didn't record retries, so we can't tell whether any search was retried or rate-limited, and we don't know the cause. The code now records attempts and failed status codes per run (`stage1.failed_attempts_by_status`).

## Forecasts

| Run | Forecast before searching | Forecast before submitting | Actual |
| --- | --- | --- | --- |
| Two-stage, lean, gpt-5.4-mini | $10.97 | $10.73 | $9.78 |
| Two-stage, full, gpt-5.4-mini | $11.98 | $11.72 | $10.49 |
| Two-stage, lean, gpt-5.4 | $18.11 | $17.31 | $14.08 |

- **Every forecast was high, by design.** Input tokens counted before submitting were 2.2-2.3% above the billed count.
- **The output assumption caused most of the gap.** It was 650 tokens per record; the runs used 278-336. [04](04-cost-model.md#forecasts-against-actual-cost) shows the effect of the updated defaults.
- **The web-search runs have no forecast**: the model decided their search count.

## Speed

**On Global Batch, the batch scheduler sets the job time, not the search design.** Every run above took 7.1-9.4 minutes from creation to completion, whatever its design, and so did the one-command runs of 2 November ([below](#the-one-command-runs-live-2-november-2026)). On Batch, Web IQ saves cost, not time.

**Where each record is a request you wait for, the design shows.** The first 5 sample records on gpt-6-sol with Flex processing, 2 requests at a time, 2 November 2026:

| Flex run | Seconds per request, median (mean) | Before the first request |
| --- | --- | --- |
| With web search (option 1's lines) | 6.3 (9.8) | - |
| With Web IQ evidence (option 2's lines) | 4.1 (5.1) | Stage 1: 6 searches in 8.8 seconds |

Five records are too few to size the difference. On another workload, 37 records on the same model measured a median of 7.4 seconds per request with web search and 3.0 with Web IQ evidence.

**One search.** Measured on 2 November 2026 from a laptop over the internet against test resources in East US 2, with 35 queries of another workload, each sent through every route in a random order, one call at a time. These are medians. From a host in Azure the times will differ; the differences between routes are the finding.

| Route | Time per search | Against one built-in web search |
| --- | --- | --- |
| Built-in web search: the time one search adds to a response (gpt-6-sol, one forced search, paired with the same request without the tool) | 1.98 and 2.17 seconds (two runs) | - |
| Web IQ through a Foundry toolbox, connection kept open | 0.42-0.43 seconds | 78-80% less time, 4.6-5 times faster |
| Web IQ through a Foundry toolbox, reconnecting after 6 or more idle seconds | 1.60-1.88 seconds | 14-26% less |
| Web IQ REST API | 0.13-0.22 seconds | 90-94% less |

- **The toolbox adds about 0.3 seconds per search** with its connection open. Web IQ's own MCP server, called directly with the key, answered as fast as REST, so the extra time is the Foundry hop. Starting a toolbox session took about 0.5 seconds, once per run.
- **Keep the connection open.** With the HTTP client's default keep-alive of 5 seconds, every toolbox call after 6 or more idle seconds opened a new connection. With 120 seconds, 10 of 10 such calls took 0.38-0.49 seconds. The code keeps idle connections for 120 seconds (`[webiq] keepalive_seconds`).
- **A short burst still opens a connection for each search in flight**, because the client sends up to 5 at a time over HTTP/1.1. The 6 searches of each 2 November run averaged 1.5-1.6 seconds. In a long job, the connections are reused.
- **Each built-in search also added about 8,140 input tokens** to the response in that test. That's why the web-search runs above read 8,713-10,230 input tokens per record, against 3,534-3,731 with planned search.

## Where each design went wrong

| Record | Expected | Web search, mini | Bounded, mini | Two-stage full, mini | Two-stage lean, mini | Web search, gpt-5.4 | Two-stage lean, gpt-5.4 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R05 Minute Maid / PepsiCo | HQ verified | **HQ contradicted** | ok | ok | ok | ok | ok |
| R09 Nespresso / Nestlé in FR | owner verified | **owner contradicted** | ok | ok | ok | ok | ok |
| R10 Nestlé Pure Life, US | owner contradicted | **verified** | ok | **verified** | **verified** | **verified** | **verified** |
| R11 Dove / Unilever | HQ verified | ok | **HQ insufficient** | ok | ok | ok | ok |
| R13 Persil, DE / Unilever | HQ verified | ok | **HQ insufficient** | ok | ok | ok | ok |
| R14 Ben & Jerry's / Unilever | owner contradicted | ok | **verified** | ok | ok | **verified** | ok |
| R15 Gillette / Unilever | owner contradicted, HQ verified | **HQ contradicted** | **owner verified** | ok | ok | ok | ok |
| R22 fictional | HQ insufficient | ok | ok | **HQ contradicted** | **HQ contradicted** | ok | ok |

Reading the misses (`results.jsonl` in each run holds the model's own finding for every fact, except the two fictional records, whose text was removed before publishing; see [results/README.md](../results/README.md)):

- **Facts leaking into each other** (web search, gpt-5.4-mini: R05, R09, R15). With no per-fact rules, the model marked a correct headquarters as "contradicted" because the owner was wrong, and the reverse. Its finding for R05 says the headquarters claim "is verified, not contradicted", next to the status `contradicted`. The two-stage prompt defines each status per fact, and none of its runs made this mistake.
- **A search budget can starve a fact** (bounded: R11, R13). Within two searches, the model didn't find a page stating Unilever's headquarters.
- **Stale pages** (R14 in two runs). Both misses cited Ben & Jerry's own pages saying it became a wholly-owned subsidiary of Unilever in 2000, and missed the December 2025 demerger. Every two-stage run got R14 right; that's luck of the evidence as much as design.
- **The lean rule** (R10, lean runs). As intended, the rule told the model to assume a brand whose name contains its owner's name belongs to that owner. Nestlé sold Pure Life in the US in 2021, so the rule is wrong for this record. A cheap plan with a known blind spot needs business sign-off.
- **Mixed evidence** (R10, full plan and model-driven web search). The full plan's brand search returned pages describing the US rights as licensed alongside the sale, and the model applied the rule that licensees aren't owners. Model-driven web search on gpt-5.4 found Nestlé's announcement that the sale had closed and still concluded Nestlé owns the brand in the US. Nestlé's own press release lists Pure Life among the brands sold ([labels](../samples/brand-owner/labels.jsonl)). A market-specific template or a "prefer primary sources" rule are the fixes to test. Only the bounded run got R10 right.
- **Name confusion** (R22, gpt-5.4-mini two-stage). The model treated results about Microsoft's fictitious "Contoso" as evidence about "Contoso Beverages Ltd" and suggested a US headquarters (the published `suggested_value`; its finding text was removed with the other fictional-record text). R23, with the same headquarters evidence, was answered correctly, and gpt-5.4 got both right. A name-matching rule ("a different full name is a different company") is the fix to test.

## The one-command runs, live (2 November 2026)

Once each option ran a job to the end by itself, each one's command ran once on the first 5 sample records, from a fresh clone of this repository, with nothing done by hand until it ended:

| Option | The one command | Model | Ended | Wall time | $ per 1,000 records (forecast) | Facts right (of 10) |
| --- | --- | --- | --- | --- | --- | --- |
| Option 1, bounded web search | `bounded_web_search.py run` | gpt-5.4-mini, Global Batch | done, exit code 0 | 9.5 minutes; the batch job 8.7 | 31.58; 2.0 billed requests per record | 10 |
| Option 2, lean plan through a toolbox | `two_stage.py run --plan lean --max-cost 1` | gpt-5.4-mini, Global Batch | done, exit code 0 | 9.7 minutes; 6 searches in 9.1 seconds, the batch job 8.8 minutes | 10.22 (10.52) | 10 |
| Flex with web search | `flex_job.py run --design bounded-web-search` | gpt-6-sol, Flex | done, exit code 0 | 0.5 minutes | 36.63 | 9 |
| Flex with Web IQ | `flex_job.py run --design two-stage --plan lean --max-cost 1` | gpt-6-sol, Flex | done, exit code 0 | 0.4 minutes; 6 searches in 8.8 seconds | 13.87 (16.65) | 10 |

- **Each command ran its job to the end:** search, forecast, send, wait, collect, price, clean up and results, with no `--retry-failed` and no second command. Each Batch run deleted its 2 service files.
- **Running each command again** reported the job done and sent nothing.
- **The miss:** Flex with web search answered R02's owner `insufficient_evidence` within its two-search budget, the behaviour described for option 1 under [Where each design went wrong](#where-each-design-went-wrong).
- **What these runs didn't need:** a throttling wait, a full quota, a second round, a split job or a held record. Those paths are covered by the offline tests in [tests/](../tests/) only.
- The toolbox was created for the runs and deleted after them: HTTP 204, then 404 on a read.

## What the sample doesn't show

- **Accuracy at scale.** Use a few hundred labeled records of your own ([08](08-evaluate-quality.md)).
- **Bigger sharing effects.** The sample has 6 owners for 23 records. Real jobs with thousands of records per shared key save more searches per record ([04](04-cost-model.md#the-formulas)).
- **Long jobs and rate limits.** The jobs were small. We didn't test sustained load near the 15 queries-per-second limit or the enqueued-token quota.
- **Other models, Data Zone Batch, the Entra REST path, and a second pass.** None were tested.
- **Results stability.** Each design ran once. Web IQ results and model decisions can vary between runs.
