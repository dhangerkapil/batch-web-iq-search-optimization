# Glossary

| Term | Meaning |
| --- | --- |
| **Batch line** | One request in a batch input file: a JSON object with `custom_id`, `method`, `url` and `body` ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). One line per record in this repository |
| **Global Batch / Data Zone Batch** | Azure OpenAI deployment types for asynchronous jobs with a 24-hour target turnaround. Both cost 50% less for tokens than the matching Standard type (Global Standard or Data Zone Standard). Global may process data in any Azure region; Data Zone keeps processing in the US, EU or APAC data zone ([Deployment types](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types)) |
| **Enqueued-token quota** | The batch quota: how many input tokens a deployment can have queued at once ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)) |
| **Built-in web search tool** | The `web_search` tool in the Responses API. Uses Grounding with Bing and is billed per Bing request in `tool_usage.web_search.num_requests` ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)) |
| **Bing request** | The billing unit of the built-in web search tool. One `web_search_call` can produce several |
| **Microsoft Web IQ** | A Microsoft-hosted web search service for AI applications, reached over `https://api.microsoft.ai/v3` through REST, SDKs or an MCP server ([Web IQ overview](https://learn.microsoft.com/en-us/web-iq/overview)) |
| **Web IQ AppID** | Your application's identity in Web IQ: allowed APIs, rate limit (15 queries per second by default), API keys, and bound Entra client IDs ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#whats-the-difference-between-a-web-iq-resource-and-a-web-iq-appid)) |
| **Passage** | `contentFormat: passage`: the parts of a page most relevant to the query, at most 3,000 characters without full-document access ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#what-is-passage-and-why-use-it)) |
| **Microsoft Foundry toolbox** | A versioned bundle of tools in a Foundry project, exposed as one MCP endpoint that accepts Microsoft Entra ID tokens ([Toolbox](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox)) |
| **Project connection** | A Foundry project resource that stores a credential, here the Web IQ key, for tools to use |
| **MCP** | Model Context Protocol. The toolbox and Web IQ both speak it; stage 1 calls `tools/call` directly, with no model |
| **Record** | One item to check: a JSON object with a unique `id` ([samples/brand-owner/records.jsonl](../samples/brand-owner/records.jsonl)) |
| **Workload** | The record-specific code: searches, prompt, schema and how to read decisions back ([workloads](../shared/workloads/__init__.py)) |
| **Fact** | One thing the answer depends on. The sample has two per record: `brand_owner` and `owner_hq` |
| **Job** | One `run` over a records file: the plan, the searches and one or more batches. If a batch ends `failed`, `expired` or `cancelled`, running the same command again resubmits only the lines that didn't complete. The job finishes when a batch completes ([07](07-deploy-and-run.md)) |
| **Search plan** | Every unique search a job needs, with the records that use each one ([search_plan.py](../option-2-two-stage-web-iq/search_plan.py)) |
| **Shared search** | A search that more than one record uses, run once per job |
| **Full plan / lean plan** | Full runs every search template for every record. Lean skips searches that a confirmed business rule makes unnecessary |
| **Evidence** | The search results attached to a record's batch line: title, URL, date and passage per result. Job-scoped working data: deleted when the job finishes, and kept after an incomplete batch so a re-run doesn't search again |
| **Stage 0, 1, 2** | Plan and forecast; run the searches; submit the no-tools batch and collect results |
| **Option** | One folder in this repository, with its README, step-by-step walkthrough, code and command: [option 1](../option-1-bounded-web-search/README.md), bounded web search (`bounded_web_search.py`); [option 2](../option-2-two-stage-web-iq/README.md), two-stage with Web IQ (`two_stage.py`); and [Flex](../flex-web-search-and-web-iq/README.md), either of them on Flex processing (`flex_job.py`). Code they share is in [shared/](../shared/) |
| **Design** | Which lines a job sends, as recorded in `run.json` and the result folders: `two-stage` (option 2), `bounded-web-search` (option 1) or `web-search` (the model searches with no caps; option 1's script with `--design web-search`, to measure a job as it runs today). `flex_job.py` takes `--design bounded-web-search` or `--design two-stage` |
| **Executor** | Where the lines run, as recorded in `run.json`: `batch`, one Global Batch job (`bounded_web_search.py` and `two_stage.py`), or `flex`, one synchronous request per line with Flex processing (`flex_job.py`) |
| **Flex processing** | A preview processing tier at about half the Standard token price, selected per request with `service_tier: "flex"` on a Global Standard deployment of a supported model. No SLA; HTTP 429 when capacity isn't available ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)) |
| **Held line** | A Flex line a plain re-run doesn't send again: its attempts are used up (`--retry-failed` resends it), or its request may have been processed and billed without an answer arriving (`--retry-ambiguous` resends it) |
| **Status** | A fact's decision: `verified`, `contradicted` or `insufficient_evidence` |
| **Forecast** | The expected cost before spending: before searching (estimated input tokens) and before submitting (exact input tokens) |
| **Search share of cost** | Search spend (Bing requests or Web IQ calls) divided by the job's total cost |
| **Second pass** | Re-running records that came back `insufficient_evidence` with a design that lets the model search. Not implemented in this sample |
| **Labeled set** | Records with known correct answers and sources, used to measure accuracy ([08](08-evaluate-quality.md)) |
