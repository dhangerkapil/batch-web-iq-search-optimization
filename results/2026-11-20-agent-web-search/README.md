# One request per line, with the web search tool: runs of 20 November 2026

Measured with [agent_web_search.py](../../agent-web-search-one-request/README.md) in East US 2 on the first 10 records of the [brand-owner sample](../../samples/brand-owner/records.jsonl) (5 and 3 in the two Flex-by-default runs), plus one Global Batch job of the same 10 lines. Each folder holds one run's `metrics.jsonl`: counts, tokens, times and statuses for each line, keyed by the line's `custom_id` (R01 to R10), and no response text, search content, URLs, response IDs or request IDs. `agent_web_search.py run` wrote each file, except batch-web-search's: see [How they were run](#how-they-were-run). The two Flex-by-default runs used the current version, which also records the service tier asked for, the tries refused with HTTP 429 and the rate-limit headers; the others ran before it.

## The runs

| Folder | How each line was sent | Model, tier as the responses report it | Lines answered | HTTP requests | `web_search_call` items per response | Search queries per response | Billed search requests per response | Input tokens, median | Seconds, median |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [request-web-search](request-web-search/metrics.jsonl) | Request: the line as a batch job runs today, no cap, reasoning effort not set | gpt-5.4, default | 10 of 10 | 10 | 1 | 2-3 | 2-3 | 8,510 | 5.9 |
| [request-bounded-web-search](request-bounded-web-search/metrics.jsonl) | Request: option 1's line (`max_tool_calls` 2, effort low, schema, the market as `user_location`) | gpt-5.4, default | 10 of 10 | 10 | 1-2 | 2 | 2 | 10,324 | 9.3 |
| [request-web-search-effort-medium](request-web-search-effort-medium/metrics.jsonl) | Request: the first line with reasoning effort medium | gpt-5.4, default | 9 of 10 | 16 | 2-13 (median 5) | 3-24 | 3-24 (median 8) | 22,759 | 33.8 |
| [agent-web-search](agent-web-search/metrics.jsonl) | Agent: a prompt agent with `web_search` attached directly; each call sends the record and `agent_reference` | gpt-5.4, default | 10 of 10 | 10 | 1 | 2-3 | not reported | 8,519 | 7.5 |
| [agent-bounded-web-search-1-line](agent-bounded-web-search-1-line/metrics.jsonl) | Agent: option 1's line, one record | gpt-5.4, default | 1 of 1 | 1 | 3 | 2 | not reported | 13,295 | 17.5 |
| [agent-web-search-flex](agent-web-search-flex/metrics.jsonl) | Agent, with `service_tier: "flex"` on each call | gpt-6-sol, flex | 3 of 3 | 5 | 2-3 | 3-9 | not reported | 16,885 | 21.2 |
| [request-web-search-flex](request-web-search-flex/metrics.jsonl) | Request, with `service_tier: "flex"` | gpt-6-sol, flex | 10 of 10 | 10 | 2-4 | 3-8 | 3-8 (median 6) | 18,003 | 13.3 |
| [batch-web-search](batch-web-search/metrics.jsonl) | The same lines as one Global Batch job, with [option 1's runner](../../option-1-bounded-web-search/README.md) (`--design web-search`) | gpt-5.4, Global Batch | 10 of 10 | one job, 7.7 minutes | 1 | - | 2-3 | 8,553 | - |
| [request-web-search-flex-default](request-web-search-flex-default/metrics.jsonl) | Request, Flex by default (this version: `service_tier: "flex"`, 5 retries); first 5 records | gpt-6-sol, flex | 5 of 5 | 5 | 2-5 | 3-11 | 3-11 (median 7) | 21,119 | 16.4 |
| [agent-web-search-flex-default](agent-web-search-flex-default/metrics.jsonl) | Agent, Flex by default (this version); first 3 records | gpt-6-sol, flex | 3 of 3 | 3 | 2-3 | 3-12 | not reported | 21,043 | 13.7 |

What they show:

- **One request per line.** Every answered line came back in one HTTP response, with its searches inside it. Across the twelve single-request runs on this page, both tables, 66 of the 76 answered lines took one HTTP request; the other 10 took more only because they were retried after an error that may pass (`--retries` retries only HTTP 408, 429 and 5xx; every failure recorded was HTTP 429). No response held a tool call for the client to answer (`client_tool_calls` is 0 in every row).
- **The model decides how much to search.** The same 10 records took 1 `web_search_call` and 2-3 queries per request with reasoning effort not set, and up to 13 `web_search_call` items (7 searches, 4 `open_page`, 2 `find_in_page`) and 24 queries in one request with effort medium.
- **What's billed is the queries.** In each of the 59 responses here that carried both counts (every response that reported `tool_usage.web_search.num_requests`, all from request mode), it equalled the number of queries in the response's `search` actions; `open_page` and `find_in_page` added none. Learn names `num_requests` as the billing count and warns against counting `web_search_call` items ([Web search usage and billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)).
- **Flex was used where it was asked for.** Every response of the Flex runs reported `service_tier` `flex`, through the agent too. In the Flex-by-default runs, every row also kept `x-ratelimit-limit-tokens` (600,000, the deployment's quota, so no temporary reduction) and the remaining tokens (537,804 at the lowest); no try was refused.
- **Agent mode doesn't report the billing count.** Responses through the Foundry project endpoint carried no `tool_usage`, so those runs say "not reported"; read the searches on your bill.
- **The cap limits calls, not actions.** Option 1's line sets `max_tool_calls` 2; through the agent, its one response held 3 `web_search_call` items (a search, an `open_page` and a `find_in_page`).
- **Batch behaves like a single request.** The Global Batch job of the same lines gave the same shape as the request run on Global Standard: 1 `web_search_call` and 2-3 billed requests per line, about 8,500 input tokens.

## Throttled runs, kept to show what happens

| Folder | Deployment's limit | Lines answered | HTTP requests | Why lines failed |
| --- | --- | --- | --- | --- |
| [request-web-search-effort-medium-150k-tpm](request-web-search-effort-medium-150k-tpm/metrics.jsonl) | gpt-5.4, 150,000 tokens per minute, 2 lines at a time | 3 of 10 | 34 | HTTP 429 "exceeded token rate limit" after 4 attempts |
| [request-web-search-flex-100k-tpm](request-web-search-flex-100k-tpm/metrics.jsonl) | gpt-6-sol, 100,000 tokens per minute, 2 at a time | 4 of 10 | 26 | The same, after 3 attempts |
| [request-web-search-flex-300k-tpm](request-web-search-flex-300k-tpm/metrics.jsonl) | gpt-6-sol, 300,000 tokens per minute, 1 at a time | 8 of 10 | 22 | The same, after 4 attempts |

A request with web search uses many tokens in a few seconds: 8,400 to 51,600 input tokens per response here. Our small test deployments refused some of them with "exceeded token rate limit", even one at a time; the effort-medium run in the first table, at 500,000 tokens per minute, still lost one line of ten after four attempts. Size the deployment's quota for requests of this size, and retry HTTP 429 with backoff, as `--retries` does.

## How they were run

- **Where:** Microsoft test subscriptions, East US 2, 20 November 2026. Temporary Global Standard deployments of gpt-5.4 (2026-03-05) and gpt-6-sol (2026-09-22), a temporary prompt agent per agent run (deleted at the end of each run), and a Global Batch deployment of gpt-5.4 for the batch job. The temporary deployments were deleted afterwards.
- **The deployments' limits, and lines in flight:**
  - gpt-5.4 at 150,000 tokens per minute, 2 at a time: request-web-search, request-bounded-web-search, agent-web-search, and the throttled effort-medium run; 1 at a time: agent-bounded-web-search-1-line.
  - gpt-5.4 at 500,000, 1 at a time: request-web-search-effort-medium.
  - gpt-6-sol, 1 at a time: at 100,000, agent-web-search-flex; at 300,000, the throttled Flex run of that size; at 600,000, request-web-search-flex.
  - gpt-6-sol at 100,000, 2 at a time: the first throttled Flex run.
  - gpt-6-sol at 600,000, 1 at a time, on a second temporary deployment, later the same day: the two Flex-by-default runs.
- **Settings:** each line sent once, with `--retries 2` or `--retries 3` after HTTP 408, 429 and 5xx (the Flex-by-default runs: the default 5, with the waits bounded); `store: false` on every request. Agent calls send `store: false` too: their responses don't echo it, so we tried to delete each one by its ID afterwards and every delete said "not found", while a control call without it was found and deleted.
- **The web search tool:** `web_search`, attached directly, never through a toolbox. Learn: "When you add web search directly to a prompt agent or a Responses API request, it uses Web IQ" ([Web search tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search)). The responses don't name the provider.
- **batch-web-search's file:** option 1's runner ran the job, and this file was converted from the runner's output, not written by `agent_web_search.py`. It has the fields that output carries, under the same names (status, service tier, `web_search_call` items and actions, billed search requests, client tool calls, tokens, valid JSON), and no HTTP requests, seconds or query counts: hence the `-` in the table. It isn't among the 59 responses with both counts.
- **Not shown:** answers and accuracy. These runs measure the request pattern; [09](../../docs/09-results-on-the-sample.md) has the accuracy of each design on the full sample.
