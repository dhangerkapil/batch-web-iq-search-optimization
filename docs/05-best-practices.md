# 05 - Best practices

A checklist for web-grounded batch jobs, whichever design you use. Each item links to its source: Microsoft Learn, the Web IQ documentation, or a measured result in [results/](../results/2026-10-31/).

## Measure before you change anything

- [ ] **Find out what share of the bill is search.** Run `analyze` on a finished job's output file. It reads `tool_usage.web_search.num_requests`, the billing unit, rather than counting tool calls, which the documentation says don't reliably match billing ([Web search billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)). On the sample, 26 call items were 66 billed requests ([results](../results/2026-10-31/web-search-gpt54mini/run.json)).
- [ ] **Record a baseline**: cost per 1,000 records, search share, input and output tokens per record, and accuracy on a labeled set. Without a baseline, every later number is unanchored.
- [ ] **Price with your own rates if you have them.** Forecasts use list prices read live from the Azure Retail Prices API (`python shared/prices.py` shows them). For negotiated rates, save the table (`prices --save my-prices.json`), edit it, and pass `--prices my-prices.json`.

## Plan the searches

- [ ] **Write down the facts each record needs** before writing any search. One search per fact, one intent per search ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#can-web-search-handle-a-prompt-that-contains-several-questions)).
- [ ] **Build queries from record fields with templates.** Use keyword queries for names and natural language for questions ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#should-i-use-natural-language-or-keyword-queries)). Keep them short; the limit is 1,000 characters ([Web Search API reference](https://learn.microsoft.com/en-us/web-iq/reference/web-search)).
- [ ] **Find the shared facts.** Any fact that many records depend on (a parent company, a product line, a regulation) should be one search per job, not one per record.
- [ ] **Set region and language deliberately.** Use the record's market for market-specific facts, and no region for global facts so one search serves every market. Region defaults to `US` ([Web Search API reference](https://learn.microsoft.com/en-us/web-iq/reference/web-search)).
- [ ] **Use `site:` operators sparingly.** They "inherently reduce result relevance", and with `site:` results may include adult content regardless of `safeSearch` ([Web Search API reference](https://learn.microsoft.com/en-us/web-iq/reference/web-search)).
- [ ] **Treat every search you skip as a business rule.** Have the data owners confirm it and measure it. The sample's lean rule saves 3 of 29 searches and is wrong for one record by design ([09](09-results-on-the-sample.md#where-each-design-went-wrong)).
- [ ] **Group records by shared key before splitting jobs.** De-duplication happens within a job ([search_plan.py](../option-2-two-stage-web-iq/search_plan.py)).

## Shape the evidence

- [ ] **Use `contentFormat: passage`** and set `maxLength` on every request ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#should-i-set-contentformat-and-maxlength-on-every-request)).
- [ ] **Size evidence on your labeled set, not by guess.** Web IQ suggests 10 results of about 3,000 characters as a starting point for quality ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#how-should-i-tune-maxresults-and-maxlength-to-manage-context-window-and-token-costs)). The sample uses 5 × 1,200 to save tokens. Try both on your data.
- [ ] **Keep titles, URLs and dates with every passage**, so the model can cite sources and judge freshness. `lastUpdatedAt` is the publisher's date and isn't always present ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#what-is-the-difference-between-lastupdatedat-and-crawledat)).
- [ ] **Show a page once per record**, even if two searches returned it ([two_stage_lines.py](../option-2-two-stage-web-iq/two_stage_lines.py) `format_evidence`).
- [ ] **Treat a missing `webResults` as no results.** Web IQ omits empty list fields ([Web IQ MCP](https://learn.microsoft.com/en-us/web-iq/integrations/mcp)).
- [ ] **Mark evidence as untrusted data**, with delimiters and a system-prompt instruction to ignore instructions inside it ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#does-web-iq-protect-an-llm-from-prompt-injection-in-retrieved-content), [06](06-security-privacy-and-terms.md#retrieved-content-is-untrusted-input)).

## Write the batch line

- [ ] **No tools in the judgment stage.** Then input tokens are fixed before you submit, and a manipulated answer can't act.
- [ ] **A strict JSON schema with enum statuses** ([Structured outputs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs)). Downstream code then reads a fixed shape instead of parsing free-form verdicts. All 69 two-stage answers on the sample (3 runs × 23 records) were valid JSON with allowed status values ([09](09-results-on-the-sample.md)).
- [ ] **Write the decision rules per fact**, including what "verified", "contradicted" and "insufficient evidence" mean. Without them, the unbounded run let one fact's status leak into the other in 3 of 23 records ([09](09-results-on-the-sample.md#where-each-design-went-wrong)).
- [ ] **Put static text first, then shared evidence, then record-specific text.** Caching needs 1,024 or more identical leading tokens ([Prompt caching](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching)). On the sample, 7-33% of input tokens were billed as cached.
- [ ] **Set `store: false`.** The batch output file already holds the response.
- [ ] **Treat `max_output_tokens` as a guardrail**, not a saving: you pay for generated tokens.
- [ ] **Measure settings together; don't assume.** The bounded run used 354 reasoning tokens per record against the unbounded run's 68, after several settings changed at once ([04](04-cost-model.md#measured-on-the-sample)).
- [ ] **Use the record id as `custom_id`**, unique per file, so results join back exactly.
- [ ] **Respect file limits.** One deployment per file, up to 100,000 requests and 200 MB, UTF-8 without a byte-order mark ([Batch limits](https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits#batch-limits), [Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). `run` checks before uploading ([batch.py](../shared/batch.py) `check_lines`), and splits a larger job into several batch jobs of at most 100,000 lines and 180 MB (`split_parts`).

## Guard the spend

- [ ] **Forecast twice**: before searching (estimate) and before submitting (exact input tokens). Stop if either is above your limit (`--max-cost`).
- [ ] **Cap searches per record** (3 by default, `max_queries_per_record`) as a second guard against a bad plan. A cap that grows with the job doesn't stop a large one at the start; `--max-queries` sets an absolute cap.
- [ ] **Set an output-token assumption from your own runs** (`--output-tokens-per-line`). On the sample, a 650-token assumption made forecasts 10-23% high ([04](04-cost-model.md#forecasts-against-actual-cost)).
- [ ] **Add an Azure budget alert** on the subscription or resource group as a backstop ([Create budgets](https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-acm-create-budgets)).

## Make it reliable

- [ ] **Stay under the Web IQ rate limit across all callers.** 15 queries per second per AppID by default, shared by every key and client ID; above it you get HTTP 429 ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-there-a-rate-limit)). Use separate AppIDs for workloads that shouldn't compete.
- [ ] **Retry 408, 429 and 5xx with exponential backoff and jitter, and honor the wait the service asks for, in full** ([webiq.py](../option-2-two-stage-web-iq/webiq.py)). Look for it beyond the `Retry-After` header: Web IQ's REST API puts it in the 429's body (`"retryAfter": "60s"`), and a Foundry toolbox relays a Web IQ 429 as a JSON-RPC error in an HTTP 200 response, with `Retry after: 60s` in its text. Retrying sooner only gets throttled again. When the wait is longer than you're willing to wait between attempts (600 seconds here), leave the search for the next round, and pause that long before it.
- [ ] **Read the real status of a relayed error.** A toolbox relays Web IQ's HTTP 429 inside an HTTP 200 response, with the status only in the text: `Response status code does not indicate success: 429 (Too Many Requests)`. Other Web IQ errors probably arrive the same way (`... 403 (Forbidden)`), though only the 429 has been seen live. Read the status from that text, so a refusal (400, 401, 403, 404, 422) isn't retried as if it were a passing fault.
- [ ] **Retry in rounds, not just attempts.** Throttling and outages can outlast a few attempts. Send what still failed again after a pause (60, then 300 seconds here), and stop after a few rounds. Send nothing again that may already be billed, or that fails the same way every time ([07](07-deploy-and-run.md#what-is-never-sent-again-automatically-and-why)).
- [ ] **Keep connections open between searches.** On 2 November 2026, through a toolbox, a search after 6 seconds or more idle took 1.6-1.9 seconds with the HTTP client's default 5-second keep-alive, and 0.38-0.49 seconds with the connection kept open (`[webiq] keepalive_seconds`).
- [ ] **Treat a full quota as a wait.** While the deployment's enqueued-token quota is full, creating a batch job fails at once with HTTP 400 `token_limit_exceeded` in 10 regions; elsewhere the job is created, validates for up to a few minutes, then fails with `token_limit_exceeded` ([Batch: queueing batch jobs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch#queueing-batch-jobs)). Either way nothing is processed. Keep the uploaded file, create the job again after a growing pause, and don't count it as a retry of the lines ([batch.py](../shared/batch.py) `QuotaBudget`).
- [ ] **Create each batch job once.** Send the create with the SDK's own retries off: the SDK would send it again after a timeout or a 5xx, with no idempotency key. Record each attempt before sending it. If a create gets no answer (the process stops, or the call times out or fails with 5xx), list the batch jobs before creating another, and wait for one already made from the same file: two jobs from one file bill twice. If the list itself fails, stop without deleting anything and list again on the next run: later if it got no answer, after a person fixes it if the sign-in or access was refused. One risk remains: if the service's job list lags behind a create that went through, a re-run could still miss that job and create a second one; listing first narrows that window, but it can't close it without an idempotency key ([batch.py](../shared/batch.py) `create`, `find_job`; [pipeline.py](../shared/pipeline.py) `create_job`).
- [ ] **Make every stage resumable.** Save each search as soon as it finishes, so an interrupted run never repeats (and pays for) a finished search, and pick up submitted jobs from `state.json`. Keep every line that was answered (it was billed) and the evidence, so a later round or a re-run sends only the other lines, without searching again ([pipeline.py](../shared/pipeline.py)).
- [ ] **Record attempts and failures, not just successes.** `run.json` lists failed attempts by status code, so rate limiting shows up instead of hiding as slowness.
- [ ] **Don't send records whose searches failed.** A record without its evidence gets a confident wrong answer, not an error. Hold it, list it (`held-records.jsonl`), and send the others.
- [ ] **End every run with an exit code a scheduler can act on**: done, needs a person, records held, or run again later ([07](07-deploy-and-run.md#exit-codes)).
- [ ] **Plan for no SLA.** Web IQ has no SLA, RTO or RPO ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#does-web-iq-include-an-sla-rto-or-rpo-commitment)). Decide what a delayed job means for your business.
- [ ] **Enable dynamic quota on Global Batch deployments**, and plan around the enqueued-token quota for big jobs ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). `run` waits while it's full, for up to `quota_wait_minutes`.
- [ ] **Watch `request_counts`, but don't panic on zeros.** Small jobs often report progress only at the end; all sample jobs showed 0 completed until they finished.

## Keep it secure and compliant

- [ ] **Prefer Microsoft Entra ID to keys**: the toolbox with a managed identity, or app-only tokens for the Web IQ REST API ([Web IQ authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)).
- [ ] **Know who can read the stored key.** On 31 October 2026 the Azure CLI's Foundry User definition included `connections/listsecrets/action`; check yours ([06](06-security-privacy-and-terms.md#identities-and-keys)).
- [ ] **Never put a key in a batch line**, and rotate keys between jobs, not during one.
- [ ] **Delete working files and batch files when the job ends**, upload batch files with an expiry as a backstop ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)), and never commit `state.json`, evidence or batch inputs.
- [ ] **Read results before publishing them.** Citations can point to pages about people, especially for records that match nothing real. `report` removes profile, social-media and officer-page URLs, and `--redact-records` removes chosen records' text.
- [ ] **Keep personal data out of queries**, or scrub it first. Web IQ doesn't mask it ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#does-web-iq-detect-or-mask-personal-data-in-queries)).
- [ ] **Get legal review** of the Data Protection Addendum and EU Data Boundary exclusion and of the content-storage terms ([06](06-security-privacy-and-terms.md)).
- [ ] **Keep citations with every decision** you show to people ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#do-i-need-to-display-citations)).
- [ ] **Ask about Zero Query Logging** if a regulation requires it. It's reviewed per use case ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#can-i-turn-off-query-logging-for-regulated-or-sensitive-workloads)).

## Keep quality honest

- [ ] **Label a few hundred records** that cover the hard cases: recent ownership changes, regional differences, near-duplicate names, entities that don't exist ([08](08-evaluate-quality.md)).
- [ ] **Compare designs on the same labeled set and model** before switching.
- [ ] **Read the wrong answers, not just the score.** On the sample, two of the two-stage misses needed different fixes: one search template and one name rule ([09](09-results-on-the-sample.md#where-each-design-went-wrong)).
- [ ] **Don't tune prompts to a handful of records.** Re-measure on the full labeled set after every change.
- [ ] **Re-check a sample of results periodically.** Web facts change, and Web IQ results for the same query can vary as its index refreshes ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#can-identical-requests-return-different-results)).
- [ ] **Version your prompt, templates and schema with the results** (`run.json` records the settings), so a change in accuracy can be traced.
