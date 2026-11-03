# 04 - Cost model

All amounts are list prices in USD. The code reads them live from the [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices) whenever it forecasts or prices a job ([prices.py](../shared/prices.py)), using the meters named in [settings.toml](../shared/settings.toml): East US 2 Global meters for the models, Global meters for the two search services. The measured results were priced with the table retrieved on 31 October 2026, saved in [tests/fixtures/prices-2026-10-31.json](../tests/fixtures/prices-2026-10-31.json); live prices on 1 November 2026 were the same. Negotiated rates change the absolute numbers, not the structure.

## Prices used

| Item | Rate | Source |
| --- | --- | --- |
| gpt-5.4-mini, Global Batch | $0.375 input, $0.0375 cached input, $2.25 output per 1M tokens | Retail Prices API, service "Foundry Models", meters "5.4 mini Batch Inp/cd Inp/Opt Gl", effective 1 March 2026 |
| gpt-5.4, Global Batch | $1.25 input, $0.13 cached input, $7.50 output per 1M tokens | Same, meters "5.4 Batch inp/cd inp/opt Gl" |
| gpt-5.4-nano, Global Batch (not tested) | $0.10 input, $0.01 cached input, $0.625 output per 1M tokens | Same, meters "5.4 nano Batch Inp/cd Inp/Opt Gl" |
| gpt-6-sol, Flex processing ([flex_job.py](../flex-web-search-and-web-iq/flex_job.py)) | $1.00 input, $0.10 cached input, $1.25 prompt-cache write, $5.00 output per 1M tokens (short context) | Same service, meters "6-sol ShortCo Inp/Cd Inp/Cd Wr/Opt Flex Gl", read live on 1 November 2026. Whether cache writes are billed instead of input or on top of it isn't documented; `run.json` shows both ([Flex with web search or Web IQ](../flex-web-search-and-web-iq/README.md#cost)) |
| Built-in web search tool | $14 per 1,000 Bing requests, counted by `tool_usage.web_search.num_requests` | [Web search billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing) (unit); Retail Prices API, service "MS Bing Services", product "Grounding with Bing", meter "Search Transactions" (rate, also on the [Grounding with Bing pricing](https://www.microsoft.com/en-us/bing/apis) page). We assume no batch discount on search requests |
| Microsoft Web IQ `/web` | $7 per 1,000 calls | Retail Prices API, service "MS Bing Services", product "Microsoft Web IQ", meter "Web Transactions"; [Web IQ pricing](https://learn.microsoft.com/en-us/web-iq/resources/pricing), GA prices effective 16 October 2026 |

Global Batch costs 50% less than Global Standard for tokens ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). For gpt-5.4-mini, the Data Zone Batch meters are listed 10% above the Global ones. Check current prices without signing in:

```bash
python shared/prices.py                         # the table the code uses now
python shared/prices.py --contains "5.4 mini Batch" --service "Foundry Models"   # any meter
python shared/prices.py --save my-prices.json   # edit for negotiated rates, then pass --prices my-prices.json
```

The Web Search API reference describes a `contentTier` value of `premium` as "subject to billing"; the pricing page lists only per-call prices ([Web Search API reference](https://learn.microsoft.com/en-us/web-iq/reference/web-search)). Stage 1 counts premium results in `run.json` (`stage1.premium_results`). There were none on the sample.

## The formulas

**Model-driven web search** (cost known only afterwards):

```text
per record = Bing requests × $14/1,000  +  input tokens × input rate  +  output tokens × output rate
```

The model decides the Bing requests, and every result it reads adds input tokens.

**Search-then-batch** (both parts known before submitting):

```text
per job    = unique searches × $7/1,000  +  Σ input tokens × input rate  +  records × output tokens × output rate
searches   = Σ per-record searches  +  unique shared keys
per record = searches ÷ records
```

On the sample: 23 records, 6 owners. The full plan has 23 per-record searches plus 6 shared, 1.26 per record. The lean plan skips 3 per-record searches: 26 searches, 1.13 per record.

The cost counts one Web IQ call per unique search that returned results. `run.json` also lists failed attempts by status code (`stage1.failed_attempts_by_status`), because the pricing page doesn't say whether failed calls are billed ([Web IQ pricing](https://learn.microsoft.com/en-us/web-iq/resources/pricing)). An interrupted run doesn't repeat finished searches; at most the searches in flight when it stopped run again.

**Your mix decides the search count.** With many records per shared key, the shared part approaches zero per record; with every record naming a different owner, it approaches one. For this workload's templates:

| Records per owner | Full plan: searches per record | Web IQ $ per 1,000 records |
| --- | --- | --- |
| 1 (no sharing) | 2.00 | $14.00 |
| 3.8 (the sample) | 1.26 | $8.83 |
| 50 | 1.02 | $7.14 |
| 1,000 | 1.00 | $7.01 |

`plan` computes the exact count for a real records file without calling anything ([pipeline.py](../shared/pipeline.py) `cmd_plan`).

## Measured on the sample

Per 1,000 records, 23 records each, Global Batch, 31 October 2026. Every input is in [results/2026-10-31](../results/2026-10-31/).

| Run | Search | Tokens | Total | vs web search, same model |
| --- | --- | --- | --- | --- |
| Model-driven web search, gpt-5.4-mini | $40.17 (2.87 Bing requests) | $2.94 | **$43.12** | - |
| Bounded web search, gpt-5.4-mini | $35.30 (2.52 Bing requests) | $5.03 | **$40.33** | -6% |
| Search-then-batch, full plan, gpt-5.4-mini | $8.83 (1.26 Web IQ calls) | $1.67 | **$10.49** | -76% |
| Search-then-batch, lean plan, gpt-5.4-mini | $7.91 (1.13 Web IQ calls) | $1.87 | **$9.78** | -77% |
| Model-driven web search, gpt-5.4 | $35.30 (2.52 Bing requests) | $9.43 | **$44.73** | - |
| Search-then-batch, lean plan, gpt-5.4 | $7.91 (1.13 Web IQ calls) | $6.16 | **$14.08** | -69% |

Totals can differ from the sum of the parts by a cent because of rounding.

**Where the gpt-5.4-mini saving comes from** ($43.12 to $9.78; $33.33 per 1,000 records from unrounded figures), attributing the price change first and the count change second (the split depends on that order):

| Driver | Saving |
| --- | --- |
| Price per search: $14 per 1,000 Bing requests to $7 per 1,000 Web IQ calls, at the old count of 2.87 per record | $20.09 |
| Fewer searches: 2.87 to 1.13 per record, from planning and sharing | $12.17 |
| Fewer tokens: input fell from 8,713 to 3,534 per record ($1.04) because trimmed evidence replaces the model's own search loop; output fell slightly ($0.03) | $1.07 |

The bounded run's tokens cost more than the unbounded run's. It produced 354 reasoning tokens per record against 68, and no input was billed as cached. It changed several settings at once (low effort, schema, rules, search budget, call cap), so these runs can't isolate the cause. A later test on another workload, changing one setting at a time, points to the reasoning effort: the unbounded lines set no effort, so gpt-5.4-mini ran at its default, `none`, and setting `low` raised the reasoning tokens and the search calls when nothing else changed ([03](03-options-compared.md#option-1---bound-the-built-in-web-search)). The two-stage runs used the same `low` effort and produced 68-83: with no tools, there's no search loop to reason through. A setting meant to save money can cost more on a given model: measure it.

**With search-then-batch, Web IQ becomes most of the bill**: 81-84% on gpt-5.4-mini. Further savings come from fewer searches (sharing, lean rules, a second pass only where needed), not from the model.

## Forecasts against actual cost

| Run | Forecast before searching | Forecast before submitting | Actual | Forecast before submitting, above actual |
| --- | --- | --- | --- | --- |
| Lean, gpt-5.4-mini | $10.97 | $10.73 | $9.78 | 9.7% |
| Full, gpt-5.4-mini | $11.98 | $11.72 | $10.49 | 11.7% |
| Lean, gpt-5.4 | $18.11 | $17.31 | $14.08 | 23.0% |

Percentages use the unrounded job totals in each `run.json`.

- **Input tokens:** the count before submitting was 2.2-2.3% above the billed count (83,148 against 81,285 for the lean runs; 87,677 against 85,814 for the full run). The prompt text is counted exactly; the JSON schema and message framing that the service adds are estimated ([cost.py](../shared/cost.py)).
- **Output tokens:** almost all of the gap. The forecasts assumed 650 output tokens per record; the runs used 278-336. The defaults are now 350 (gpt-5.4-mini) and 400 (gpt-5.4). With them, the same forecasts would have been 2.8%, 5.2% and 9.6% above actual.
- **Cached input:** not assumed, so forecasts err high.

Forecasts for model-driven web search can't be made this way: the model decides the search count and the search-added input. `plan --design web-search` says so instead of guessing.

## What this means at volume

The table scales the measured per-record costs linearly. It's illustrative: your search count depends on your record mix (see the table above), and your token count on your prompt and evidence size.

| Records per month | Web search, gpt-5.4-mini | Bounded web search, gpt-5.4-mini | Search-then-batch, lean, gpt-5.4-mini | Web search, gpt-5.4 | Search-then-batch, lean, gpt-5.4 |
| --- | --- | --- | --- | --- | --- |
| 100,000 | $4,312 | $4,033 | $978 | $4,473 | $1,408 |
| 1 million | $43,120 | $40,330 | $9,780 | $44,730 | $14,080 |
| 10 million | $431,200 | $403,300 | $97,800 | $447,300 | $140,800 |

At 10 million records a month, the search stage alone is about 11.3 million Web IQ calls at the sample's 1.13 per record. That's about 4.4 queries per second, averaged over a 30-day month, under the default 15-per-second limit if spread evenly ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-there-a-rate-limit)).

## Levers, in order of effect

1. **Searches per record.** Share every fact that repeats across records. Sort records by the shared key before splitting jobs. Skip searches only under confirmed business rules.
2. **Price per search.** $7 per 1,000 Web IQ calls against $14 per 1,000 Bing requests, at list prices.
3. **Evidence size.** `--max-results` × `--max-chars` sets most of the input tokens. Cut it only as far as your labeled set allows.
4. **Model.** Matters once search is fixed: gpt-5.4 cost 3.3 times gpt-5.4-mini for tokens on the sample.
5. **Caching.** Put shared evidence first and keep the static prefix identical. It helps only when 1,024 or more leading tokens are identical, and isn't guaranteed ([Prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching)).
