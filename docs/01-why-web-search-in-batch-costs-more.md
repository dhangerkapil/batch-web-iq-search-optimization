# 01 - Why web search inside batch lines costs more than it needs to

Many teams enrich or check records in bulk with Azure OpenAI Global Batch: one JSONL line per record, each asking a model to "use web search" to verify something about that record. It works, and batch halves the token price compared with Global Standard ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). But in these jobs, search is usually most of the bill, and the model decides how big that part is.

## How the built-in web search tool is billed

The built-in `web_search` tool in the Responses API uses Grounding with Bing. Charges are based on the number of Bing search requests, reported per response in `tool_usage.web_search.num_requests`. The documentation warns not to count `search` actions or `web_search_call` output items to estimate billing, because they "don't reliably represent the number of Bing search requests" ([Web search usage and billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)). The rate is the Grounding with Bing Search price, $14 per 1,000 transactions at list price ([Grounding with Bing pricing](https://www.microsoft.com/en-us/bing/apis)).

Three properties of that design add up in a batch job:

1. **The model decides how many searches to run.** Nothing in a typical line says how many, so each record gets as many as the model wants. Reasoning models can also open pages and search inside them (`open_page`, `find_in_page`) ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)).
2. **One tool call can be several billed requests.** On the sample, gpt-5.4-mini made 26 `web_search_call` items across 23 records and was billed for 66 Bing requests ([results](../results/2026-10-31/web-search-gpt54mini/run.json)).
3. **Shared facts are searched again for every record.** If 5,000 records name the same company, each line searches for that company on its own. Batch lines can't share work.

On top of that, every search result the model reads becomes input tokens, and the line's cost is known only after the job finishes.

## What it looks like on the sample

The sample checks 23 brand records for two facts each: who owns the brand in a market, and where that owner is headquartered ([samples/brand-owner](../samples/brand-owner/records.jsonl)). With the built-in web search tool on gpt-5.4-mini Global Batch ([results](../results/2026-10-31/web-search-gpt54mini/run.json)):

| Per 1,000 records | Amount | Share |
| --- | --- | --- |
| Web search (2.87 Bing requests per record at $14 per 1,000) | $40.17 | 93% |
| Tokens (8,713 input, 2,905 of them cached, and 292 output per record) | $2.94 | 7% |
| **Total** (rounded) | **$43.12** | |

Bounding the search inside the batch line (at most 2 tool calls, low reasoning effort, a strict schema, a search budget in the prompt) cut that by only 6%, to $40.33, because the model still made 2.52 billed requests per record ([results](../results/2026-10-31/bounded-web-search-gpt54mini/run.json)). The tool-call cap limits call items, not the requests inside each call. And the starting point was already lean: its lines set no reasoning effort, so gpt-5.4-mini ran at its default, `none`, which searches little, while the bounded lines set `low` ([03](03-options-compared.md#option-1---bound-the-built-in-web-search)).

Your numbers will differ by model, prompt and record mix: bigger reasoning models tend to search more, and prompts that ask for many citations search more. The shape is common: **search is the cost, and the model controls it**.

## Measure your own job first

You don't need to change anything to see your split. Run `analyze` on the output file of a batch job you already ran:

```bash
python shared/analyze.py my-batch-output.jsonl --price-model gpt-5.4-mini
```

It reads `tool_usage.web_search.num_requests` and token usage for each line, prices them at current list prices from the public Azure Retail Prices API ([prices.py](../shared/prices.py)), and prints the search share of cost ([analyze.py](../shared/analyze.py)). Run it on the input file to see tools, caps and reasoning settings per line. Your files stay local: the only call it makes is the price lookup, which sends meter names, and `--prices <file>` skips even that.

If search is a small share of your cost, the pattern in this repository won't help much. If it's most of the cost, read [02 - The search-then-batch pattern](02-the-search-then-batch-pattern.md).
