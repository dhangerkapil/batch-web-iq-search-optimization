# One request per line, with the web search tool: directly or through an agent

Many jobs send one Responses API request per record with the built-in `web_search` tool and let the model search. When you move such a job off Global Batch, to Flex processing or to your own service, the question is whether each record is still one request, or whether you have to run the tool calls yourself. **It's one request.** The `web_search` tool runs on the service side: inside one request the model can search, open a page, search again and then answer, and one response comes back with all of it ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)). Your code sends one HTTPS request and reads one response. There's no tool call to run, no loop and nothing to send back.

This folder shows it on your batch lines or the sample's, **on Flex processing by default** (`service_tier: "flex"`). Flex tokens cost 50% of the Standard token rates, with variable latency and no SLA ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)); the discount is stated for tokens, not for web search requests. It measures what happened inside each request: the `web_search_call` items and their actions, the queries, the billed search requests, the tokens, the seconds, the service tier that processed it, and the rate-limit headers. It sends each line one of two ways:

| `--mode` | What's sent | Choose it when |
| --- | --- | --- |
| `request` (default) | The line's body, as it is, to a model deployment's Responses API. That's the JSON a Global Batch line carries | Your service calls the model directly, as a batch job does |
| `agent` | The same line through a Microsoft Foundry **prompt agent** with the `web_search` tool attached directly. The model, instructions and tool sit in the agent's definition; each call sends the record and an `agent_reference` | You want the definition kept in Foundry, with its versions and the portal, and still one request per record |

**Web IQ behind it.** "When you add web search directly to a prompt agent or a Responses API request, it uses Web IQ. When you add web search to a toolbox, it still uses Grounding with Bing Search, not Web IQ" ([Web search tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search), since 17 November 2026). This sample always attaches the tool directly, and agent mode refuses a toolbox. The response doesn't name the provider; the documentation and your bill do.

## What we measured

On 20 November 2026, the sample's first 10 records, each line sent once ([all runs and their files](../results/2026-11-20-agent-web-search/README.md)). The last two rows are this version, with Flex processing by default:

| How each line was sent | Model, tier | Answered | `web_search_call` items per response | Billed search requests per response | Input tokens, median | Seconds, median |
| --- | --- | --- | --- | --- | --- | --- |
| Request: the line as a batch job runs today, no cap | gpt-5.4, Global Standard | 10 of 10, one HTTP request each | 1 | 2-3 | 8,510 | 5.9 |
| Request: the same line with reasoning effort medium | gpt-5.4, Global Standard | 9 of 10 (one refused with HTTP 429 after 4 tries) | 2-13 | 3-24 | 22,759 | 33.8 |
| Request: [option 1](../option-1-bounded-web-search/README.md)'s bounded line | gpt-5.4, Global Standard | 10 of 10, one HTTP request each | 1-2 | 2 | 10,324 | 9.3 |
| Agent: `web_search` attached directly | gpt-5.4, Global Standard | 10 of 10, one HTTP request each | 1 | not reported | 8,519 | 7.5 |
| Request, Flex (`service_tier: "flex"`) | gpt-6-sol, Flex | 10 of 10, one HTTP request each | 2-4 | 3-8 | 18,003 | 13.3 |
| Agent, Flex | gpt-6-sol, Flex | 3 of 3 | 2-3 | not reported | 16,885 | 21.2 |
| The same lines as one Global Batch job ([option 1's runner](../option-1-bounded-web-search/README.md), `--design web-search`) | gpt-5.4, Global Batch | 10 of 10, in a 7.7-minute job | 1 | 2-3 | 8,553 | - |
| Request, Flex by default (this version; first 5 records) | gpt-6-sol, Flex | 5 of 5, one HTTP request each, every response on `flex` | 2-5 | 3-11 | 21,119 | 16.4 |
| Agent, Flex by default (this version; first 3 records) | gpt-6-sol, Flex | 3 of 3, one HTTP request each, every response on `flex` | 2-3 | not reported | 21,043 | 13.7 |

- **One request, the searches inside it.** No response, in any run, held a tool call for the client to answer. With reasoning effort medium, one request held 13 `web_search_call` items: 7 searches, 4 `open_page` and 2 `find_in_page`. [WALKTHROUGH.md](WALKTHROUGH.md#step-3---what-came-back-the-searches-inside-the-response) shows a response's structure.
- **The model decides how much to search,** and reasoning effort moves it most: the same 10 records took a median of 2 billed requests with effort not set, and 8 with effort medium. Bound it with option 1's settings (a cap, low effort, a search budget in the prompt).
- **What's billed is the queries.** Learn bills web search on `tool_usage.web_search.num_requests` ([usage and billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)). In every response with both counts, 59 of 59 (the batch job's rows have no query counts), it equalled the queries in the response's `search` actions; `open_page` and `find_in_page` added none.
- **Batch behaves the same.** The Global Batch job of the same lines gave the same shape as the request run: 1 `web_search_call` and 2-3 billed requests per line.
- **Flex was used, and it's recorded.** In the Flex-by-default runs every response reported `service_tier` `flex`, through the agent too, and every line kept the deployment's rate-limit headers: `x-ratelimit-limit-tokens` 600,000, the deployment's quota, so no temporary reduction was in force.

## Benefits and drawbacks

| | Request mode | Agent mode |
| --- | --- | --- |
| Calls per record | One | One, plus creating the agent once per run (one version per distinct definition) and deleting it at the end |
| Where the prompt and tool live | In each request, as in a batch line | In the agent's definition: the model, the system message, the tool, the schema and the reasoning effort |
| Billing count in the response | `tool_usage.web_search.num_requests` | Not reported: responses through the project endpoint carried no `tool_usage`, so read the bill |
| Flex | `service_tier: "flex"` by default, on a Flex-capable model | The same, sent with each call; the responses reported `service_tier` "flex". Learn doesn't document Flex through an agent; it worked in our runs |
| Per-record tool settings | Any, per line | Belong to the agent, so each distinct value, such as the record's country in `user_location`, needs its own agent version |
| Global Batch | The same body is a batch line | Not used: batch lines carry the request body |

**The cap limits calls, not actions.** Option 1's line sets `max_tool_calls` 2. Through the agent, its response held 3 `web_search_call` items: a search, an `open_page` and a `find_in_page`.

## What you need

- **Request mode:** a **Global Standard** deployment of a model that supports Flex processing and web search, such as gpt-6-sol, and Microsoft Entra ID sign-in (`az login`). Flex runs only on the models Learn lists ([supported models](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#review-supported-models): in November 2026 gpt-6.1-sol, gpt-6-sol, gpt-6-luna, gpt-6-astra and gpt-5.6-sol, -luna and -terra); other models, gpt-5.4 among them, answer HTTP 400. On such a model, run with `--service-tier default` (Standard).
- **Agent mode:** a Microsoft Foundry project with that deployment, and the **Foundry User** role on the project ([Web search tool, prerequisites](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search#prerequisites)).
- **Quota:** Flex and Standard share the deployment's quota ([Handle Flex errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#handle-flex-errors)). A request with web search used 8,400 to 51,600 input tokens here, so give the deployment room: at 600,000 tokens per minute, one line at a time, nothing was refused.
- Nothing new to install: [requirements.txt](../requirements.txt) has everything (`openai`, `azure-identity`, `httpx`).

## Run it

From this folder:

```bash
# The request each line becomes, and with --mode agent the agent's definition and a call: calls nothing
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2 --mode agent

# Send 10 lines, each once, to a deployment's Responses API, on Flex processing
python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/request --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment of a Flex model>

# The same through a prompt agent, created for the run and deleted after it
python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/agent --mode agent \
    --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --deployment <model deployment>

# Your own batch input file: the first 5 lines, sent as they are, apart from the changes printed
python agent_web_search.py run --lines my-batch-input.jsonl --first 5 --workdir runs/mine --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment of a Flex model>

# A run's table again, and its summary.md
python agent_web_search.py report runs/request
```

Add `--service-tier default` to send on Standard instead (for a model without Flex, or to compare), `--fallback-default` to send a line Flex refused once more on Standard, `--retries 0` to send each line exactly once and see what happened, and `--concurrency 2` for two lines in flight. With records, `--design bounded-web-search` sends option 1's bounded line instead of the line as a job runs today.

**Stopping a run.** An error every request would get (sign-in, access, a missing deployment, a model without Flex, an unexpected error) stops the run, and so does Ctrl+C: lines not sent yet aren't sent, a line waiting to retry isn't tried again, the ones in flight finish and are recorded (they may be billed already), and `run.json` and `summary.md` are written for what came back. Pressing Ctrl+C again only prints a note, so that wait isn't cut short. A Flex HTTP 400 stops the run only while no line has come back on Flex: once one has, the model takes Flex, so a later Flex 400 is about that line (its size or settings) and fails that line alone. An agent the run created is deleted, unless `--keep-agent`. This is a measuring tool, not a job runner: it doesn't resume. Run it again with a new `--workdir`, and every line is sent again. It exits with 0 when every line came back completed, 1 otherwise, and 130 after Ctrl+C.

**What's changed before sending.** Lines built from records are option 1's, exactly, plus `service_tier: "flex"` (`--service-tier none` leaves it out). With `--lines`, the body is sent as it is, except:

- `store: false`, so the service doesn't keep the response;
- `service_tier`: `flex`, or what `--service-tier` asks for;
- the changes you ask for. `--deployment` replaces the model, because a batch line names a Global Batch deployment, and `--tool web_search` replaces `web_search_preview`, which Learn calls "supported but not recommended".

In agent mode the line is split: the model, system messages, tools, `text`, `reasoning`, `temperature`, `top_p` and `tool_choice` go into the agent's definition, and the other messages, `max_output_tokens`, `max_tool_calls`, `service_tier`, `include` and `store` go with each call. A line setting that neither takes, such as `metadata`, isn't sent.

`plan` and `run` print every change, and in agent mode every setting not sent.

## Retries, throttling and Flex: what Learn recommends, and what this sample does

| Learn's guidance | This sample |
| --- | --- |
| Retry HTTP 408, 429 and 5xx with exponential backoff and random jitter; honor `Retry-After`; set a maximum retry count and maximum delay ([Handle Flex errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#handle-flex-errors)). "Set a maximum number of retries (for example, 5–10)" ([Rate limit best practices](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#rate-limit-best-practices)) | Only 408, 429 and 5xx are retried, up to 5 more tries by default. Each wait is the longer of backoff with jitter (10 s, doubling, capped at 120 s) and the `retry-after-ms` or `Retry-After` the service sent, as [flex.py](../flex-web-search-and-web-iq/flex.py) does ([send](one_request.py#L155)) |
| "Limit retry attempts and total elapsed time"; "Retry Flex only while the delay remains acceptable" ([Apply production best practices](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#apply-production-best-practices)) | A `Retry-After` longer than 600 seconds isn't waited out, and a line's waits stay within 900 seconds in all; past either, the line fails with its last error, explicitly |
| Don't retry HTTP 400, 401 or 403 unchanged; check a 404's deployment and endpoint | 400 fails the line without a retry. 401, 403 and 404, and the Flex 400 before any line has come back on Flex, stop the run, with a hint to use `--service-tier default` for a model without Flex |
| "Make fallback explicit. Set `service_tier` to `default`"; fall back to Standard only "if the retry budget is exhausted and the application allows the higher Standard cost" ([Fall back to Standard processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#fall-back-to-standard-processing)) | Off by default. `--fallback-default` sends a line Flex refused once more with `service_tier: "default"`, at Standard price, and records it as a fallback |
| Set a longer client timeout, such as 15 minutes; don't submit the same job again after a client-side timeout | 900-second timeout. A request that timed out, lost its connection or failed with an unexpected error is never sent again, because the service may have processed and billed it |
| "Track the processed tier": record `service_tier` with latency, status and token usage | Every row has the tier asked for and the tier the response reports; `summary.md` counts lines answered on another tier |
| With your own retry loop, set the SDK's `max_retries=0`, or each try can retry again | `max_retries=0`: every HTTP request is counted |
| Monitor `x-ratelimit-remaining-tokens` and `-requests`; compare `x-ratelimit-limit-tokens` with your quota to spot a temporary reduction; check the error message to tell a rate limit from busy capacity ([Types of 429 errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#types-of-429-errors)) | Each row keeps the four `x-ratelimit-*` numbers and, for HTTP 429, `throttle`: `rate_limit` (more quota helps), `capacity` (waiting helps) or `unknown`, read from the message. The request IDs (`x-request-id`, `apim-request-id`), for a support request, stay in `responses.jsonl` |
| Avoid sharp spikes; ramp up gradually; spread delay-tolerant work to off-peak hours ([Distribute traffic evenly](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#distribute-traffic-evenly), [Handle Flex errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#handle-flex-errors)) | One line at a time by default (`--concurrency`). It doesn't pace itself on the headers; for a large job, use the [Flex runner](../flex-web-search-and-web-iq/README.md), with its rounds, attempt ledger and resumable runs |

**Not used, and why.** Background mode would free the client from a long wait, but it "requires `store=true`" ([Responses API, background tasks](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/responses)), and this sample sends `store: false`; it waits synchronously, with the 15-minute timeout Learn suggests for Flex. An agent holds at most 1,000 versions ([Agent Service limits](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions)); agent mode refuses lines that would need more.

## Files

| File | What's in it | Share it? |
| --- | --- | --- |
| `metrics.jsonl` | One row per line, keyed by the line's `custom_id`: HTTP requests, seconds, status, the service tier asked for and the one reported, fallbacks, tries refused with HTTP 429 and their cause, waits, the `x-ratelimit-*` numbers, `web_search_call` items and actions, queries, billed search requests, tokens. The fields are an allow-list (`METRIC_FIELDS` in [one_request.py](one_request.py#L52)) | Yes: counts only. Check first that your `custom_id` values don't identify anyone |
| `summary.md` | The run's table, by `custom_id`, and what it shows | Yes: counts only. It names the deployment, as the responses report it |
| `responses.jsonl` | The whole response for each line, and the request IDs of each try | No: it holds search content. Keep it local; see [06](../docs/06-security-privacy-and-terms.md#web-iq-terms-storing-content). `.gitignore` excludes it |
| `run.json` | The run's settings, changes, the agent's name and versions, and the summary | Keep it with the run |

## Settings that matter

| Setting | In [settings.toml](../shared/settings.toml#L153) | Default |
| --- | --- | --- |
| `[agent_sample] api_version`: the Foundry Agent Service REST API version | [154](../shared/settings.toml#L154) | `v1` |
| `[agent_sample] foundry_scope`: the Entra ID scope of the project endpoint | [155](../shared/settings.toml#L155) | `https://ai.azure.com/.default` |
| `[agent_sample] agent_name_prefix`: the agent is `<prefix>-<UTC time>-<random>` unless `--agent-name` names it; a name that exists is refused, so give runs you start at the same time different names | [156](../shared/settings.toml#L156) | `web-search-one-request` |
| `[agent_sample] first`: lines sent (`--first`) | [157](../shared/settings.toml#L157) | `10` |
| `[agent_sample] retries`: extra tries after HTTP 408, 429 and 5xx (`--retries`) | [158](../shared/settings.toml#L158) | `5` |
| `[agent_sample] retry_budget_seconds`: the most one line waits between its tries, in all | [159](../shared/settings.toml#L159) | `900` |
| `[agent_sample] service_tier`: the tier each request asks for (`--service-tier`: `flex`, `default`, `priority` or `none`) | [160](../shared/settings.toml#L160) | `flex` |
| `[agent_sample] client_timeout_seconds`: per request | [161](../shared/settings.toml#L161) | `900` |
| `[flex] max_server_wait_seconds`: the longest `Retry-After` waited out before another try, shared with the Flex runner | [145](../shared/settings.toml#L145) | `600` |
| `[azure_openai] token_scope`: the Entra ID scope of the account endpoint (request mode) | [93](../shared/settings.toml#L93) | `https://cognitiveservices.azure.com/.default` |

The backoff itself is the Flex runner's: `[flex] backoff_base_seconds` 10 and `max_delay_seconds` 120.

## Limits, stated plainly

- **Flex is a preview, with no latency or service SLA.** Its capacity can be taken back, so refusals are likelier at peak hours; a request refused for lack of capacity isn't billed, but Flex and Standard share the deployment's quota ([Handle Flex errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#handle-flex-errors)). Learn documents Flex for the Responses API; it doesn't document it with the web search tool or through an agent. Both worked in our runs (gpt-6-sol, 20 November 2026), and every response reported `flex`, but confirm before you depend on it.
- **The 429's cause is read from its message.** Learn's table of 429 types names phrases ("Rate limit is exceeded", "temporarily unable to process your request"); the Flex page doesn't name the code of a Flex capacity refusal. `throttle` is our reading, so check the message and `x-ratelimit-limit-tokens` too. No run here was refused at 600,000 tokens per minute, so a real Flex capacity 429 hasn't been seen.
- **Small deployments throttle web search.** A response here used 8,400 to 51,600 input tokens in a few seconds. Test deployments of 100,000 to 500,000 tokens per minute refused some requests with "exceeded token rate limit", even one line at a time ([throttled runs](../results/2026-11-20-agent-web-search/README.md#throttled-runs-kept-to-show-what-happens)). Size the quota; the retries handle the rest. The token limit counts actual tokens, not `max_output_tokens` ([Understanding rate limits](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#understanding-rate-limits)).
- **Agent mode doesn't report the billing count;** read it on the bill. Agent calls send `store: false`, and their responses don't echo it. Deleting each one by ID afterwards said "not found", while a control call without `store: false` was found and deleted.
- **The price per search isn't settled here.** This sample reports counts, not cost. Since the tool moved to Web IQ, check the meter on your bill ([04](../docs/04-cost-model.md)).
- **Answers aren't scored here.** These runs measure the request pattern; [08](../docs/08-evaluate-quality.md) and [09](../docs/09-results-on-the-sample.md) cover quality.

## More

- Step by step, with the code: [WALKTHROUGH.md](WALKTHROUGH.md).
- The line itself and how to bound its search: [option 1](../option-1-bounded-web-search/README.md). Flex at scale, with an attempt ledger and resumable runs: [flex-web-search-and-web-iq](../flex-web-search-and-web-iq/README.md).
