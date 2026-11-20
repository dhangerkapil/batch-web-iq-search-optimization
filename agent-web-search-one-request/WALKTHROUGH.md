# Walkthrough: one request per line, with the web search tool

| | |
| --- | --- |
| What it shows | Each batch line, sent as one Responses API request with the `web_search` tool: the searches happen inside that one request |
| What doesn't change | The line. `request` mode sends its body as it is; `agent` mode moves its model, instructions and tool into a prompt agent |
| Code in this folder | [agent_web_search.py](agent_web_search.py) (the command), [one_request.py](one_request.py) (send one request, measure it), [prompt_agent.py](prompt_agent.py) (the prompt agent, over REST) |
| Code it shares | Option 1's [web_search_line](../option-1-bounded-web-search/web_search_lines.py#L16) (the lines), [response_metrics](../shared/batch.py#L271) (the same measures as the Batch and Flex code), and flex.py's error rules: [retry_after](../flex-web-search-and-web-iq/flex.py#L101), [backoff](../flex-web-search-and-web-iq/flex.py#L121), [_kind](../flex-web-search-and-web-iq/flex.py#L143) |

## Before you start

- A Global Standard deployment of a model that supports Flex processing and web search, such as gpt-6-sol: each request asks for `service_tier: "flex"` unless you pass `--service-tier default` ([supported models](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#review-supported-models)). The runs below used gpt-5.4 on Standard, and gpt-6-sol on Flex.
- For agent mode, a Microsoft Foundry project with that deployment, and the Foundry User role on it.
- `az login`. Request mode signs in with the scope in `[azure_openai] token_scope`; agent mode with `https://ai.azure.com/.default` ([settings](../shared/settings.toml#L153)). Pass `--subscription` to pin the Azure CLI credential to one subscription.
- `pip install -r requirements.txt`, from the repository root.

## Step 1 - See the request (no cost)

```bash
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2
```

```text
2 line(s). run sends each one once, as one HTTPS request; plan sends nothing.
Changes made to the lines before sending them:
  - service_tier: None -> 'flex'

The request for R01: POST <base url>responses, for example https://<resource>.openai.azure.com/openai/v1/responses
{
  "model": "my-batch-deployment",
  "input": [
    {"role": "system", "content": "You are a research assistant who checks brand ownership facts using web search. Output only valid JSON."},
    {"role": "user", "content": "Check this brand record using web search.\nCheck two facts about the RECORD at the end of this message: ... RECORD:\n{\"id\":\"R01\",\"brand\":\"Gatorade\",...}"}
  ],
  "tools": [{"type": "web_search"}],
  "max_output_tokens": 8000,
  "store": false,
  "service_tier": "flex"
}
```

(The prompt is shortened here; `plan` prints it whole.)

- **The lines.** [load_lines](agent_web_search.py#L57) builds them with option 1's code, the same lines `bounded_web_search.py` would submit: `--design web-search` (the default) is the line as a batch job runs today, and `--design bounded-web-search` is option 1's bounded line. With `--lines`, it reads your batch input file instead.
- **The request.** [requests_for](agent_web_search.py#L83) makes each line's request with [apply_overrides](one_request.py#L76). The body is the line's body. The only changes are `service_tier` (`flex` by default), `store: false` and the ones you ask for (`--deployment` with `--lines`, `--tool`, `--reasoning-effort`, ...), and every change is printed. A test checks that requests built from records, with no tier, equal option 1's batch lines.

## Step 2 - Send one line

```bash
python agent_web_search.py run ../samples/brand-owner/records.jsonl --first 1 --workdir runs/one --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment of a Flex model>
```

```text
1 line(s), request mode: each sent once, as one HTTPS request.
Changes made to the lines before sending them:
  - service_tier: None -> 'flex'
  R01: completed on flex in 16.38 s, 1 HTTP request(s), 3 web_search_call item(s) find_in_page 1, search 2, billed search requests 5
```

[send](one_request.py#L155) makes one `responses.create` call per try, through `with_raw_response` so it can read the headers, with the SDK's own retries off ([make_client](../shared/batch.py#L38) with `max_retries=0`), so the count of HTTP requests is exact:

- **The answer comes back in that one response.** Nothing is sent back to the model, there's no `previous_response_id`, and there's no loop over tool calls.
- **Retries, as Learn recommends for Flex: only after HTTP 408, 429 or 5xx,** up to `--retries` (5) more tries, each after the longer of backoff with jitter and the `Retry-After` the service sent ([line 213](one_request.py#L213)). A `Retry-After` over 600 seconds isn't waited out, and a line's waits stay within 900 seconds in all; then the line fails with its last error. Once the run stops, a line waiting to retry isn't tried again.
- **A timeout or a dropped connection is held, never resent,** because the service may have processed and billed the request ([line 204](one_request.py#L204)). So is an error that isn't HTTP, and it stops the run, as in the Flex runner.
- **HTTP 401, 403 and 404 stop the run,** because every request would get them ([line 201](one_request.py#L201)), and so does HTTP 400 "Flex tier is not supported", with a hint to use `--service-tier default` ([line 196](one_request.py#L196)); once a line has come back on Flex, that 400 fails only its own line. [send_all](agent_web_search.py#L229) then sends nothing more; Ctrl+C stops it the same way, after the requests in flight finish.
- **What each try leaves behind:** the four `x-ratelimit-*` headers of the last response, for `metrics.jsonl`; for an HTTP 429, its cause read from the message (`rate_limit` or `capacity`); and the request IDs (`x-request-id`, `apim-request-id`) of every try, for `responses.jsonl` only. Learn says to read the headers to tell a rate limit from busy capacity, and to compare `x-ratelimit-limit-tokens` with your quota ([Types of 429 errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#types-of-429-errors)).
- **`--fallback-default`**, off unless you pass it, sends a line Flex refused once more with `service_tier: "default"`, at Standard price: the explicit fallback Learn describes ([Fall back to Standard processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#fall-back-to-standard-processing)). Not after a 429 that reads as a quota limit, because Flex and Standard share the quota and Learn says to limit fallback to capacity-related responses; never after a timeout, because that request may have been processed.

## Step 3 - What came back: the searches inside the response

This is a real response's structure, with each query, URL and the answer replaced. It's one request for record R04, on gpt-6-sol with `service_tier: "flex"` ([request-web-search-flex-300k-tpm](../results/2026-11-20-agent-web-search/request-web-search-flex-300k-tpm/metrics.jsonl)):

```json
{
  "status": "completed",
  "service_tier": "flex",
  "output": [
    {"type": "reasoning"},
    {"type": "web_search_call", "status": "completed", "action": {"type": "search", "queries": ["<query>", "<query>", "<query>"]}},
    {"type": "reasoning"},
    {"type": "web_search_call", "status": "completed", "action": {"type": "search", "queries": ["<query>", "<query>", "<query>", "<query>"]}},
    {"type": "reasoning"},
    {"type": "web_search_call", "status": "completed", "action": {"type": "search", "queries": ["<query>", "<query>", "<query>", "<query>"]}},
    {"type": "reasoning"},
    {"type": "web_search_call", "status": "completed", "action": {"type": "find_in_page", "url": "<url>", "pattern": "<pattern>"}},
    {"type": "reasoning"},
    {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "<the answer>"}]}
  ],
  "usage": {"input_tokens": 31383, "input_tokens_details": {"cached_tokens": 4399}, "output_tokens": 738,
            "output_tokens_details": {"reasoning_tokens": 482}},
  "tool_usage": {"web_search": {"num_requests": 11}}
}
```

- **Inside one request,** the model reasoned, searched three times with 3, 4 and 4 queries, looked inside a page, and answered. The client sent one request and read this one response.
- **`tool_usage.web_search.num_requests` is 11:** the three searches' 3 + 4 + 4 queries. The `find_in_page` added nothing. Learn bills web search on this count, not on `web_search_call` items ([Web search usage and billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)), and in all 59 responses here with both counts (the batch job's rows have no query counts), it equalled the queries.
- **Learn describes this agentic search:** with a reasoning model, "the model actively manages the search process and can perform web searches as part of its chain of thought, analyze results, and decide whether to keep searching" ([Options to use web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#options-to-use-web-search)).
- **Reasoning effort moves the count most.** The same 10 records on gpt-5.4 took 1 `web_search_call` per request with effort not set. With effort medium they took 2 to 13, one with 7 searches, 4 `open_page` and 2 `find_in_page`.

## Step 4 - How a response is measured

[measure](one_request.py#L277) reads the response with [response_metrics](../shared/batch.py#L271), the code the Batch and Flex paths use, and adds:

- the queries in the `search` actions;
- the sources, with `--count-sources`, which adds `include: ["web_search_call.action.sources"]`;
- the URL citations in the answer;
- the output items a client would have to answer (`function_call` and similar; 0 in every run);
- the service tier asked for, next to the one the response reports, and whether the line fell back to Standard: Learn's "track the processed tier";
- the tries refused with HTTP 429 and their cause, the waits, and the `x-ratelimit-*` numbers.

It keeps only the fields in [METRIC_FIELDS](one_request.py#L52), so `metrics.jsonl` and `summary.md` hold counts and never response text or request IDs. When a response has no `tool_usage`, the billed count is `None`, shown as "not reported", and not 0 ([line 295](one_request.py#L295)).

## Step 5 - The same line through a prompt agent

```bash
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 1 --mode agent
```

```text
Agent mode: run creates 1 version(s) of one prompt agent, with the web_search tool attached directly (no toolbox), and deletes the agent after the run. The definition for R01:
POST <project endpoint>/agents/<agent name>/versions?api-version=v1
{"definition": {"kind": "prompt", "model": "my-batch-deployment", "tools": [{"type": "web_search"}], "instructions": "You are a research assistant ..."}}

Then one call per line. For R01: POST <project endpoint>/openai/v1/responses
{"agent_reference": {"type": "agent_reference", "name": "<agent name>", "version": "<version>"},
 "input": [{"role": "user", "content": "Check this brand record using web search. ..."}], "max_output_tokens": 8000,
 "service_tier": "flex", "store": false}
```

- **The split.** [split_body](prompt_agent.py#L51) moves the line's model, system messages, tools, output schema (`text`), reasoning effort and the other settings a prompt agent's definition takes (`temperature`, `top_p`, `tool_choice`: [DEFINITION_KEYS](prompt_agent.py#L38)) into the definition, because "the model and tool settings belong to the agent definition, not the invocation request" ([Web search tool, image search](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search#image-search-results)). The rest of the line's messages are the call's input. A line setting that neither the definition nor the call takes, such as `metadata`, isn't sent, and `plan` and `run` list it with the changes.
- **What each call carries.** Besides the input, a call sends `max_output_tokens`, `max_tool_calls`, `service_tier` (`flex` by default) and `store`. All four were accepted, and the agent's responses reported `service_tier` "flex". Learn doesn't document Flex through an agent; it worked in every agent run here, on gpt-6-sol.
- **No toolbox.** `split_body` refuses any tool other than `web_search`, including a toolbox attached as an MCP tool: through a toolbox, web search uses Grounding with Bing Search, not Web IQ ([Web search tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search#add-web-search-directly-to-an-agent)).
- **The REST calls** are the ones Learn shows for a prompt agent: [create_version](prompt_agent.py#L133) (`POST {project}/agents/{name}/versions`), the call with `agent_reference` through the project's Responses API ([openai_client](prompt_agent.py#L150)), and [delete](prompt_agent.py#L139) (`DELETE {project}/agents/{name}/versions/{version}`, then the agent).
- **Naming and cleanup.** The run ([_run](agent_web_search.py#L365)) refuses a name that exists, creates one version per distinct definition (an agent holds at most 1,000, so it refuses lines that would need more), and deletes the agent when the run ends, also after an error or Ctrl+C, unless `--keep-agent`.

```bash
python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/agent --mode agent \
    --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --deployment <model deployment>
```

```text
Created agent 'web-search-one-request-<UTC time>-<random>' version 1: model <deployment>, tools ['web_search'] attached directly.
  R01: completed on flex in 13.7 s, 1 HTTP request(s), 2 web_search_call item(s) find_in_page 1, search 1, billed search requests not reported
  ...
Agent 'web-search-one-request-<UTC time>-<random>': deleted (GET returns 404)
```

Responses through the project endpoint carry no `tool_usage`, hence "not reported": read the searches on your bill. They do carry the `x-ratelimit-*` headers and the request IDs. Each record's tool settings belong to the agent. With option 1's bounded line, whose `user_location` is the record's market, each distinct country gets its own agent version.

## Step 6 - Flex, and Global Batch

- **Flex, by default.** Each request asks for `service_tier: "flex"`, on a Global Standard deployment of a model Learn lists for Flex ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)). Flex tokens cost 50% of the Standard rates, with no SLA. On gpt-6-sol, every line of every Flex run here answered with one HTTP request, and every response reported `flex`. `--service-tier default` sends on Standard instead; on a model without Flex, such as gpt-5.4, it's the only way to run. For a large job on Flex, with rounds, an attempt ledger and resumable runs, use [flex-web-search-and-web-iq](../flex-web-search-and-web-iq/README.md).
- **Global Batch.** The request body is the batch line. The same 10 lines as a Global Batch job, with option 1's runner, gave the same shape as the request run on Global Standard: 1 `web_search_call` and 2-3 billed requests per line ([results](../results/2026-11-20-agent-web-search/README.md)). Batch lines don't carry `service_tier`; `--service-tier none` sends them that way.

## Step 7 - Your own batch file

```bash
python agent_web_search.py run --lines my-batch-input.jsonl --first 5 --workdir runs/mine --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment of a Flex model> --tool web_search
```

```text
5 line(s), request mode: each sent once, as one HTTPS request.
Changes made to the lines before sending them:
  - model: 'my-batch-deployment' -> '<deployment>'
  - tool: 'web_search_preview' -> 'web_search'
  - service_tier: None -> 'flex'
  - store: None -> False
```

- **What changes.** A batch line names a Global Batch deployment, so `--deployment` points it at a Global Standard one. `service_tier: "flex"` asks for Flex processing. `store: false` keeps the service from storing the response. `--tool web_search` replaces the preview tool, which Learn calls "supported but not recommended" ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)). In request mode nothing else in the line changes.
- **Agent mode works on your lines too.** System messages become the agent's instructions; lines that share them share one agent version. A setting the agent and the call have no place for isn't sent, and it's listed with the changes.

## If something goes wrong

| What you see | Why, and what to do |
| --- | --- |
| HTTP 429, `throttle` `rate_limit` ("exceeded token rate limit") | The deployment's tokens or requests per minute. A request with web search can use tens of thousands of tokens in seconds. Raise the deployment's quota (Flex and Standard share it), or send fewer at once; the retries wait as asked, and `--fallback-default` doesn't fall back, since Standard would hit the same quota. If `x-ratelimit-limit-tokens` is below your quota, the limit is lowered for a while: wait it out ([Types of 429 errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#types-of-429-errors)) |
| HTTP 429, `throttle` `capacity` | The service or Flex capacity is busy. More quota doesn't help: wait, run delay-tolerant work off-peak, or pass `--fallback-default` to finish such lines on Standard, at Standard price ([Handle Flex errors](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing#handle-flex-errors)) |
| A line failed after "asked to wait" longer than 600 s, or used up its 900 s of waits | Bounded on purpose, as Learn recommends. Run it again later, or use the Flex runner, which waits for its next round |
| HTTP 400 "Flex tier is not supported ..." (stops the run) | The deployment's model doesn't take Flex. Use a model Learn lists for Flex, such as gpt-6-sol, or `--service-tier default` for Standard. After a line has come back on Flex, this 400 fails only its own line |
| HTTP 401 or 403 (stops the run) | Sign in (`az login`, with `--subscription` if you use several), and check your role: Foundry User for agent mode. A slow Azure CLI can also fail the sign-in; run again |
| "The project already has an agent named ..." | Agent mode never reuses an agent: pass another `--agent-name` |
| "... already holds a run" | Each run writes to a new `--workdir`, so no line is sent twice by mistake |
| You pressed Ctrl+C (exit code 130) | Lines not sent yet weren't sent, and a line waiting to retry wasn't tried again; the ones in flight finished and were recorded, and `run.json` and `summary.md` cover what came back. Pressing Ctrl+C again while it waits only prints a note. An agent the run created was deleted, unless `--keep-agent`. The command doesn't resume: run it again with a new `--workdir`, and every line is sent again |
| "Agent mode attaches only the web_search tool, directly" | The line has another tool, such as an MCP tool for a toolbox. Use request mode, or remove that tool |
| A response with no `web_search_call` | The model answered without searching. That's its choice: prompt it to search, or set `tool_choice` in the line |
| `--lines` refuses the file | It takes Responses API lines (`url` `/v1/responses`, with a `body`) |
| Sustained 429s below your quota | Learn says to open a support request; give it the request IDs from `responses.jsonl` ([When to retry vs. when to escalate](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota#when-to-retry-vs-when-to-escalate)) |

## Sources

- [Web search tool, Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search): direct web search uses Web IQ, a toolbox uses Grounding with Bing Search; prompt agents over REST; settings in the definition.
- [Web search, Azure OpenAI Responses API](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search): the `web_search` tool, agentic search with reasoning models, the response shape, usage and billing, `include`.
- [Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing): `service_tier: "flex"`, supported models, the 50% token discount, no SLA, error handling, fallback to Standard, production best practices.
- [Manage Azure OpenAI quota](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/quota): rate-limit headers, retry best practices, types of 429 errors, when to escalate.
- [Quotas and limits for Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/limits-quotas-regions): 1,000 versions per agent.
- The runs: [results/2026-11-20-agent-web-search](../results/2026-11-20-agent-web-search/README.md).
