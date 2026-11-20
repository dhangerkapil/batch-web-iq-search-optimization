# One request per line, with the web search tool: directly or through an agent

Many jobs send one Responses API request per record with the built-in `web_search` tool and let the model search. When you move such a job off Global Batch, to Flex processing or to your own service, the question is whether each record is still one request, or whether you have to run the tool calls yourself. **It's one request.** The `web_search` tool runs on the service side: inside one request the model can search, open a page, search again and then answer, and one response comes back with all of it ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)). Your code sends one HTTPS request and reads one response. There's no tool call to run, no loop and nothing to send back.

This folder shows it on your batch lines or the sample's. It measures what happened inside each request: the `web_search_call` items and their actions, the queries, the billed search requests, the tokens and the seconds. It sends each line one of two ways:

| `--mode` | What's sent | Choose it when |
| --- | --- | --- |
| `request` (default) | The line's body, as it is, to a model deployment's Responses API. That's the JSON a Global Batch line carries | Your service calls the model directly, as a batch job does |
| `agent` | The same line through a Microsoft Foundry **prompt agent** with the `web_search` tool attached directly. The model, instructions and tool sit in the agent's definition; each call sends the record and an `agent_reference` | You want the definition kept in Foundry, with its versions and the portal, and still one request per record |

**Web IQ behind it.** "When you add web search directly to a prompt agent or a Responses API request, it uses Web IQ. When you add web search to a toolbox, it still uses Grounding with Bing Search, not Web IQ" ([Web search tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search), since 17 November 2026). This sample always attaches the tool directly, and agent mode refuses a toolbox. The response doesn't name the provider; the documentation and your bill do.

## What we measured

On 20 November 2026, the sample's first 10 records, each line sent once ([all runs and their files](../results/2026-11-20-agent-web-search/README.md)):

| How each line was sent | Model, tier | Answered | `web_search_call` items per response | Billed search requests per response | Input tokens, median | Seconds, median |
| --- | --- | --- | --- | --- | --- | --- |
| Request: the line as a batch job runs today, no cap | gpt-5.4, Global Standard | 10 of 10, one HTTP request each | 1 | 2-3 | 8,510 | 5.9 |
| Request: the same line with reasoning effort medium | gpt-5.4, Global Standard | 9 of 10 (one refused with HTTP 429 after 4 tries) | 2-13 | 3-24 | 22,759 | 33.8 |
| Request: [option 1](../option-1-bounded-web-search/README.md)'s bounded line | gpt-5.4, Global Standard | 10 of 10, one HTTP request each | 1-2 | 2 | 10,324 | 9.3 |
| Agent: `web_search` attached directly | gpt-5.4, Global Standard | 10 of 10, one HTTP request each | 1 | not reported | 8,519 | 7.5 |
| Request, Flex (`service_tier: "flex"`) | gpt-6-sol, Flex | 10 of 10, one HTTP request each | 2-4 | 3-8 | 18,003 | 13.3 |
| Agent, Flex | gpt-6-sol, Flex | 3 of 3 | 2-3 | not reported | 16,885 | 21.2 |
| The same lines as one Global Batch job ([option 1's runner](../option-1-bounded-web-search/README.md), `--design web-search`) | gpt-5.4, Global Batch | 10 of 10, in a 7.7-minute job | 1 | 2-3 | 8,553 | - |

- **One request, the searches inside it.** No response, in any run, held a tool call for the client to answer. With reasoning effort medium, one request held 13 `web_search_call` items: 7 searches, 4 `open_page` and 2 `find_in_page`. [WALKTHROUGH.md](WALKTHROUGH.md#step-3---what-came-back-the-searches-inside-the-response) shows a response's structure.
- **The model decides how much to search,** and reasoning effort moves it most: the same 10 records took a median of 2 billed requests with effort not set, and 8 with effort medium. Bound it with option 1's settings (a cap, low effort, a search budget in the prompt).
- **What's billed is the queries.** Learn bills web search on `tool_usage.web_search.num_requests` ([usage and billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)). In every response with both counts, 54 of 54 (the batch job's rows have no query counts), it equalled the queries in the response's `search` actions; `open_page` and `find_in_page` added none.
- **Batch behaves the same.** The Global Batch job of the same lines gave the same shape as the request run: 1 `web_search_call` and 2-3 billed requests per line.

## Benefits and drawbacks

| | Request mode | Agent mode |
| --- | --- | --- |
| Calls per record | One | One, plus creating the agent once per run (one version per distinct definition) and deleting it at the end |
| Where the prompt and tool live | In each request, as in a batch line | In the agent's definition: the model, the system message, the tool, the schema and the reasoning effort |
| Billing count in the response | `tool_usage.web_search.num_requests` | Not reported: responses through the project endpoint carried no `tool_usage`, so read the bill |
| Flex | `service_tier: "flex"` on a Flex-capable model | The same, sent with each call; the responses reported `service_tier` "flex" |
| Per-record tool settings | Any, per line | Belong to the agent, so each distinct value, such as the record's country in `user_location`, needs its own agent version |
| Global Batch | The same body is a batch line | Not used: batch lines carry the request body |

**The cap limits calls, not actions.** Option 1's line sets `max_tool_calls` 2. Through the agent, its response held 3 `web_search_call` items: a search, an `open_page` and a `find_in_page`.

## What you need

- **Request mode:** a Global Standard deployment of a model that supports web search (gpt-5.4 here), and Microsoft Entra ID sign-in (`az login`). Flex needs a Flex-capable model, such as gpt-6-sol ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)).
- **Agent mode:** a Microsoft Foundry project with that deployment, and the **Foundry User** role on the project ([Web search tool, prerequisites](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search#prerequisites)).
- Nothing new to install: [requirements.txt](../requirements.txt) has everything (`openai`, `azure-identity`, `httpx`).

## Run it

From this folder:

```bash
# The request each line becomes, and with --mode agent the agent's definition and a call: calls nothing
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2 --mode agent

# Send 10 lines, each once, to a deployment's Responses API
python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/request --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment>

# The same through a prompt agent, created for the run and deleted after it
python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/agent --mode agent \
    --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --deployment <model deployment>

# Your own batch input file: the first 5 lines, sent as they are, apart from the changes printed
python agent_web_search.py run --lines my-batch-input.jsonl --first 5 --workdir runs/mine --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment>

# A run's table again, and its summary.md
python agent_web_search.py report runs/request
```

Add `--service-tier flex` for Flex processing, `--retries 3` to retry HTTP 408, 429 and 5xx (the default sends each line once and shows what happened), and `--concurrency 2` for two lines in flight. With records, `--design bounded-web-search` sends option 1's bounded line instead of the line as a job runs today.

**Stopping a run.** An error every request would get (sign-in, access, a missing deployment, a model without Flex) stops the run, and so does Ctrl+C: lines not sent yet aren't sent, the ones in flight finish and are recorded, and `run.json` and `summary.md` are written for what came back. An agent the run created is deleted, unless `--keep-agent`. This is a measuring tool, not a job runner: it doesn't resume. Run it again with a new `--workdir`, and every line is sent again. It exits with 0 when every line came back completed, 1 otherwise, and 130 after Ctrl+C.

**What's changed before sending.** Lines built from records are option 1's, exactly. With `--lines`, the body is sent as it is, except:

- `store: false`, so the service doesn't keep the response;
- the changes you ask for. `--deployment` replaces the model, because a batch line names a Global Batch deployment, and `--tool web_search` replaces `web_search_preview`, which Learn calls "supported but not recommended".

In agent mode the line is split: the model, system messages, tools, `text`, `reasoning`, `temperature`, `top_p` and `tool_choice` go into the agent's definition, and the other messages, `max_output_tokens`, `max_tool_calls`, `service_tier`, `include` and `store` go with each call. A line setting that neither takes, such as `metadata`, isn't sent.

`plan` and `run` print every change, and in agent mode every setting not sent.

## Files

| File | What's in it | Share it? |
| --- | --- | --- |
| `metrics.jsonl` | One row per line, keyed by the line's `custom_id`: HTTP requests, seconds, status, `web_search_call` items and actions, queries, billed search requests, tokens. The fields are an allow-list (`METRIC_FIELDS` in [one_request.py](one_request.py#L35)) | Yes: counts only. Check first that your `custom_id` values don't identify anyone |
| `summary.md` | The run's table, by `custom_id`, and what it shows | Yes: counts only. It names the deployment, as the responses report it |
| `responses.jsonl` | The whole response for each line | No: it holds search content. Keep it local; see [06](../docs/06-security-privacy-and-terms.md#web-iq-terms-storing-content). `.gitignore` excludes it |
| `run.json` | The run's settings, changes, the agent's name and versions, and the summary | Keep it with the run |

## Settings that matter

| Setting | In [settings.toml](../shared/settings.toml#L153) | Default |
| --- | --- | --- |
| `[agent_sample] api_version`: the Foundry Agent Service REST API version | [154](../shared/settings.toml#L154) | `v1` |
| `[agent_sample] foundry_scope`: the Entra ID scope of the project endpoint | [155](../shared/settings.toml#L155) | `https://ai.azure.com/.default` |
| `[agent_sample] agent_name_prefix`: the agent is `<prefix>-<UTC time>` unless `--agent-name` names it; a name that exists is refused | [156](../shared/settings.toml#L156) | `web-search-one-request` |
| `[agent_sample] first`: lines sent (`--first`) | [157](../shared/settings.toml#L157) | `10` |
| `[agent_sample] retries`: extra tries after HTTP 408, 429 and 5xx (`--retries`) | [158](../shared/settings.toml#L158) | `0` |
| `[azure_openai] token_scope`: the Entra ID scope of the account endpoint (request mode) | [93](../shared/settings.toml#L93) | `https://cognitiveservices.azure.com/.default` |

Retries follow [flex.py](../flex-web-search-and-web-iq/flex.py)'s rules: `Retry-After` honored, otherwise backoff with jitter. A request that timed out or lost its connection is never sent again, because the service may have processed and billed it.

## Limits, stated plainly

- **Small deployments throttle web search.** A response here used 8,400 to 51,600 input tokens in a few seconds. Test deployments of 100,000 to 500,000 tokens per minute refused some requests with "exceeded token rate limit", even one line at a time ([throttled runs](../results/2026-11-20-agent-web-search/README.md#throttled-runs-kept-to-show-what-happens)). Size the quota, and retry with backoff.
- **Agent mode doesn't report the billing count;** read it on the bill. Agent calls send `store: false`, and their responses don't echo it. Deleting each one by ID afterwards said "not found", while a control call without `store: false` was found and deleted.
- **The price per search isn't settled here.** This sample reports counts, not cost. Since the tool moved to Web IQ, check the meter on your bill ([04](../docs/04-cost-model.md)).
- **Answers aren't scored here.** These runs measure the request pattern; [08](../docs/08-evaluate-quality.md) and [09](../docs/09-results-on-the-sample.md) cover quality.

## More

- Step by step, with the code: [WALKTHROUGH.md](WALKTHROUGH.md).
- The line itself and how to bound its search: [option 1](../option-1-bounded-web-search/README.md). Flex at scale, with an attempt ledger and resumable runs: [flex-web-search-and-web-iq](../flex-web-search-and-web-iq/README.md).
