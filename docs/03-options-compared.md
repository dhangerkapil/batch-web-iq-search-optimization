# 03 - Options compared

There's more than one way to cut web-search cost in a batch job. This page compares them, says which were measured on the sample, and lists what we rejected and why.

## At a glance

Measured rows ran the 23 sample records on gpt-5.4-mini Global Batch on 31 October 2026, priced at list prices ([09](09-results-on-the-sample.md)).

| Option | What changes | Measured cost per 1,000 records | Search per record | Facts right (of 46) | Main risk |
| --- | --- | --- | --- | --- | --- |
| 0. Model-driven web search (the common starting point) | Nothing | $43.12 | 2.87 Bing requests | 42 | The model decides search spend; known only afterwards |
| 1. Bounded web search ([folder](../option-1-bounded-web-search/README.md)) | Line settings only | $40.33 | 2.52 Bing requests | 42 | Still model-driven: the cap limits tool calls, not the requests inside them |
| **2. Search-then-batch, two-stage with Web IQ ([folder](../option-2-two-stage-web-iq/README.md))** | A search stage before submitting; no tools in the batch | **$9.78** (lean plan), $10.49 (full plan) | 1.13 or 1.26 Web IQ calls, fixed before submitting | 44 | Search templates and decision rules decide quality; evidence storage terms; Web IQ has no SLA |
| Flex processing, for option 1 or 2 ([folder](../flex-web-search-and-web-iq/README.md)) | Each line sent as one synchronous request with `service_tier: "flex"`, instead of one batch file | Not measured on the sample; three 5-record checks on gpt-6-sol: $36.41-$36.68 with web search, $13.83-$13.87 with Web IQ | As the option | - | Preview with no SLA; your client paces the requests; fewer models (not gpt-5.4) |
| A. Web IQ as an MCP tool inside batch lines | Tool type in each line, plus a Web IQ resource | Not measured on the sample | Model-driven | - | A key in every line; nothing throttles the calls; not documented for batch |
| B. Foundry Web IQ tool, called synchronously | A runner calls the Foundry project endpoint per record | Not measured on the sample | Model-driven | - | It isn't batch: Standard token rates unless Flex processing (preview) applies, and you run the runner |
| C. Decide shared facts once (add-on to option 2) | Validate each shared fact once per job, then join | Not measured | Fewer | - | Output-join logic; depends on how much your records repeat |

Options 1 and 2, and Flex for either, have a folder each in this repository, with a README, a step-by-step walkthrough and the code. A, B and C are alternatives we considered and didn't build.

Twenty-three records show cost mechanics. They're too few to rank accuracy: a two-fact difference is within noise. Measure accuracy on your own labeled set ([08](08-evaluate-quality.md)).

**Speed.** On Global Batch, the batch scheduler sets the job time: every run on the sample took 7.1-9.4 minutes, whatever its design, so Web IQ saves cost, not time. One Web IQ search through a Foundry toolbox took about 0.4 seconds with its connection open, against about 2 seconds that one built-in web search adds to a response. On Flex, where you wait for each record, that shows: on the first 5 sample records, each request took a median of 4.1 seconds with Web IQ evidence against 6.3 with web search ([09](09-results-on-the-sample.md#speed)).

## Option 1 - Bound the built-in web search

Change each line without changing the pipeline ([web_search_lines.py](../option-1-bounded-web-search/web_search_lines.py) `web_search_line`, `bounded=True`; [walkthrough](../option-1-bounded-web-search/WALKTHROUGH.md)):

- `max_tool_calls: 2`
- `reasoning.effort: low`
- a strict JSON schema ([Structured outputs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs))
- `user_location` set to the record's market
- a search budget written in the prompt

Also use the `web_search` tool type, not `web_search_preview`, which is "supported but not recommended" ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)).

**Why it's attractive.** It needs no new service, contract or code beyond the line settings, and batch accepted every setting.

**What we measured.** Only 6% cheaper on the sample: gpt-5.4-mini went from 2.87 to 2.52 billed requests per record. Part of the reason is the starting point: the model-driven lines set no reasoning effort, so gpt-5.4-mini ran at its default, `none`, with about one search call and 68 reasoning tokens per record, while the bounded lines set `low` (1.48 calls and 354 reasoning tokens per record; [run records](../results/2026-10-31/)).

- The cap limits tool-call items, not the requests inside each call: 1.48 call items per record still produced 2.52 billed requests.
- A cap can only remove searches the model was making. A model that searches more by default leaves more to cut.
- Two `owner_hq` facts came back `insufficient_evidence`: within the two-search budget, the model didn't find a page that stated the headquarters.
- **The record's market in `user_location` changed neither cost nor speed measurably** in a separate test on 2 November 2026: 37 records of another workload, bounded lines on gpt-5.4 Global Batch, two runs with the country and two without. Billed requests per record were 2.68 and 2.73 with it and 2.81 and 2.41 without, and every run's median was 13 seconds per response: the two arms differed less than two runs of the same arm. Learn says the location refines search results ([Web search: control results by user location](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#control-results-by-user-location)), so the lines keep it.

**Each setting on its own, on another workload.** On 3 November 2026 we ran 37 records of another workload on gpt-5.4 Global Batch, changing one group of settings per job, priced at list prices:

| Lines | Billed requests per record | Cost per 1,000 records |
| --- | --- | --- |
| As written: no cap, no schema, no reasoning effort set (the responses reported `none`) | 3.4 | $57.23 |
| ... plus `max_tool_calls: 2` | 3.4 | $57.28 |
| ... plus `reasoning.effort: low` | 5.4 | $92.98 |
| ... plus a short search budget, not specific to the task, at the end of the system message | 2.4 | $46.82 |
| Bounded lines with a search budget, rules and a strict schema written for the task (five runs) | 2.6-2.9 | $50.14-$55.77 |
| The same bounded lines with `max_tool_calls: 1` | 2.1 | $38.48 |

- **Reasoning effort moves the searches most.** At `none` the model made about one search call per record, so a cap of 2 changed nothing; at `low` with no budget it made 1.7 calls of about three queries each.
- **The cap limits calls, not queries:** one call carried up to four. A search budget in the prompt is what cut the queries per call, from about three to 1.9.
- **Cheaper wasn't better.** Every run at `none`, and the run with a cap of 1, answered that workload's labeled records less reliably than the bounded lines at `low` with the task's own budget, rules and schema; the cap of 1 also left more facts unsettled. Set the reasoning effort in every line, and measure a change on a labeled sample before you adopt it ([08](08-evaluate-quality.md)).
- **Per subscription, web search can only be switched on or off** ([Manage web search tool](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#manage-web-search-tool)). The cap, the effort and the budget go in each line, so put them in the code that writes your lines.

**When to choose it.** When you need a change this week with no new service, and `analyze` shows the model making many searches per line.

## Option 2 - Search-then-batch

Covered in [02](02-the-search-then-batch-pattern.md); measured in [09](09-results-on-the-sample.md); the code and a step-by-step walkthrough are in [option-2-two-stage-web-iq/](../option-2-two-stage-web-iq/README.md).

**Why it's the recommended Web IQ design:**

- Search spend is set by the plan, not the model, and input tokens are known before you submit.
- Shared facts are searched once per job.
- The batch carries no tools and no secrets.
- The only Web IQ caller is your own rate-limited code.

**What to accept with it:**

- **Evidence is minutes to hours old** when the batch runs.
- **Quality depends on your search templates and written decision rules.** Measure it.
- **Evidence is stored for the job's duration.** Review that with legal against the Web IQ terms ([06](06-security-privacy-and-terms.md#web-iq-terms-storing-content)).
- **No SLA.** A Web IQ outage stops a job before submission ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#does-web-iq-include-an-sla-rto-or-rpo-commitment)).

**A second pass for hard records.** Records that come back `insufficient_evidence` can be re-run with option 1, so the expensive model-driven search is paid only where planned search wasn't enough. This sample doesn't implement it.

## Alternative A - Web IQ as an MCP tool inside each batch line

Replace the built-in tool with the Web IQ MCP server ([Web IQ MCP](https://learn.microsoft.com/en-us/web-iq/integrations/mcp)):

```json
{"type": "mcp", "server_label": "webiq", "server_url": "https://api.microsoft.ai/v3/mcp",
 "headers": {"x-apikey": "<Web IQ key>"}, "require_approval": "never", "allowed_tools": ["web"]}
```

**Why it's attractive.** One batch file per job, as today, with search billed at the Web IQ rate.

**Why we don't recommend it:**

- **The key travels inside every line.** The input file in the Azure OpenAI file store then holds the key until it's deleted.
- **A Foundry project connection doesn't remove the problem.** It would keep the key out of the file, but the Batch API is on the resource's Azure OpenAI endpoint, and the Batch documentation doesn't describe resolving project connections in batch lines ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). Don't rely on it without testing it yourself.
- **Credential failures need watching.** If the key inside the lines is wrong, expired or rotated while the job runs, the lines can't search. Test how your jobs behave in that case before relying on it, watch `request_counts`, and rotate keys only between jobs.
- **Nothing throttles the calls.** Batch workers call Web IQ directly, so bursts can exceed the AppID's rate limit (15 queries per second by default) ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-there-a-rate-limit)).
- **Not documented for batch.** The Batch documentation's feature-support list doesn't mention MCP tools ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)).
- **Search is still model-driven.** The model decides how many searches to run.

This alternative wasn't run with this sample.

## Alternative B - Foundry Web IQ tool, called synchronously

Attach the Web IQ project connection as an MCP tool and call the Foundry project endpoint once per record, outside batch ([MCP tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/model-context-protocol), [MCP authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication)). Foundry injects the key, so it never appears in code or requests.

**The cost.** It isn't batch: tokens are billed at Standard rates, twice the batch rate ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). Flex processing (preview) gives synchronous requests the same 50% discount on supported models, but a Flex request can return HTTP 429 when capacity isn't available, and Flex has no latency or service SLA ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)). Either way you build and operate a runner with concurrency limits, retries, resume and monitoring, and size Global Standard quota for it. Choose it when results are needed within minutes per record, not in a nightly job.

## Add-on C - Decide shared facts once

Option 2 already searches each shared fact once per job, but every record's line still re-judges it: every PepsiCo record decides PepsiCo's headquarters again. Add-on C decides each shared fact in its own small batch, then joins the decision to every record that uses it. Savings grow with repetition: thousands of records per entity save thousands of judgments; the sample (2-5 records per owner) would save little. `plan` reports how many records share each search, so you can size it before building it.

## Flex processing, for either design

Flex processing (preview) runs a request at about half the Standard token price, on a Global Standard deployment of a Flex-capable model ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)). [flex_job.py](../flex-web-search-and-web-iq/flex_job.py) sends option 1's or option 2's lines that way, one request per line, instead of uploading a batch file. The option itself doesn't change ([Flex with web search or Web IQ](../flex-web-search-and-web-iq/README.md)).

**Why you might use it.** Results arrive in seconds per record rather than within a batch window, and a model without a Global Batch SKU can run the same lines.

**Why it isn't the default.**
- It's preview, with no latency or service SLA, and can return HTTP 429 when Flex capacity isn't available.
- It shares the deployment's quota with Standard requests, and your client paces and retries every request, where Batch is one submit.
- It supports fewer models: not gpt-5.4 or gpt-5.4-mini when we checked, so moving a job to Flex can also mean changing its model.
- gpt-6-sol charges for prompt-cache writes, which a short no-tools prompt can pay for without reusing them.

**What we measured.** Not measured on the 23-record sample. Three live checks on the first 5 records completed every line on gpt-6-sol: two on 1 November 2026, before and after this repository's folders were reorganized, and one on 2 November with the one-command code. They cost $36.68, $36.41 and $36.63 per 1,000 records with bounded web search, and $13.83, $13.85 and $13.87 with Web IQ. In the third, each request took a median of 6.3 seconds with web search and 4.1 with Web IQ evidence ([09](09-results-on-the-sample.md#speed)).

## Rejected alternatives

| Rejected | Why |
| --- | --- |
| A cheaper model only | Model price moves only the token share of the bill (7% on the sample with web search). Search requests cost the same on every model |
| Prompt caching alone | Caching discounts input tokens only, and needs at least 1,024 identical leading tokens ([Prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching)). Search requests aren't discounted |
| A lower `max_output_tokens` alone | You pay for tokens generated, not for the cap. Keep it as a guardrail |
| Stage 1 through the Responses API with a search tool | Pays model tokens to run a search the code already knows, and the model can reword it. The toolbox calls the same Web IQ tool with no model |
| Reuse evidence across jobs | A cross-job cache is a database of Web IQ content, which the terms don't allow ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#can-i-cache-or-store-web-iq-content)) |
| An Entra token in the MCP header of batch lines | Access tokens last 60-90 minutes by default ([Access tokens](https://learn.microsoft.com/en-us/entra/identity-platform/access-tokens#token-lifetime)); a batch line can run hours after submission |
| Web IQ Browse on known URLs instead of searching | Browse ($1 per 1,000 calls) fetches a URL you already have; it's enabled per AppID on request ([Web IQ pricing](https://learn.microsoft.com/en-us/web-iq/resources/pricing), [Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#why-cant-my-application-call-browse-or-auto)). Worth testing when records already carry a source URL that states the fact; it doesn't discover sources. Not tested |

## Choosing

- **Search is a small share of your cost** (check with `analyze`): none of this matters much. Tune the prompt and model instead.
- **You need a change this week with no new service:** option 1, after `analyze` shows the model making many searches per line.
- **Searches can be built from record fields, and facts change slowly:** option 2.
- **Research is multi-hop for some records:** option 2 for most records, plus a second pass with option 1 for the rest.
- **Many records share entities:** option 2 shares the searches. Add C to share the decisions too.
- **Answers are needed per record within minutes:** option 1 or 2 on Flex ([flex-web-search-and-web-iq/](../flex-web-search-and-web-iq/README.md)), if a preview tier without an SLA is acceptable; or alternative B.
