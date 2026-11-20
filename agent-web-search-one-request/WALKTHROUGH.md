# Walkthrough: one request per line, with the web search tool

| | |
| --- | --- |
| What it shows | Each batch line, sent as one Responses API request with the `web_search` tool: the searches happen inside that one request |
| What doesn't change | The line. `request` mode sends its body as it is; `agent` mode moves its model, instructions and tool into a prompt agent |
| Code in this folder | [agent_web_search.py](agent_web_search.py) (the command), [one_request.py](one_request.py) (send one request, measure it), [prompt_agent.py](prompt_agent.py) (the prompt agent, over REST) |
| Code it shares | Option 1's [web_search_line](../option-1-bounded-web-search/web_search_lines.py#L16) (the lines), [response_metrics](../shared/batch.py#L271) (the same measures as the Batch and Flex code), and flex.py's error rules: [retry_after](../flex-web-search-and-web-iq/flex.py#L101), [backoff](../flex-web-search-and-web-iq/flex.py#L121), [_kind](../flex-web-search-and-web-iq/flex.py#L143) |

## Before you start

- A Global Standard deployment of a model that supports web search. The runs below used gpt-5.4, and gpt-6-sol for Flex.
- For agent mode, a Microsoft Foundry project with that deployment, and the Foundry User role on it.
- `az login`. Request mode signs in with the scope in `[azure_openai] token_scope`; agent mode with `https://ai.azure.com/.default` ([settings](../shared/settings.toml#L153)). Pass `--subscription` to pin the Azure CLI credential to one subscription.
- `pip install -r requirements.txt`, from the repository root.

## Step 1 - See the request (no cost)

```bash
python agent_web_search.py plan ../samples/brand-owner/records.jsonl --first 2
```

```text
2 line(s). run sends each one once, as one HTTPS request; plan sends nothing.
The lines are sent exactly as built: no changes.

The request for R01: POST <base url>responses, for example https://<resource>.openai.azure.com/openai/v1/responses
{
  "model": "my-batch-deployment",
  "input": [
    {"role": "system", "content": "You are a research assistant who checks brand ownership facts using web search. Output only valid JSON."},
    {"role": "user", "content": "Check this brand record using web search.\nCheck two facts about the RECORD at the end of this message: ... RECORD:\n{\"id\":\"R01\",\"brand\":\"Gatorade\",...}"}
  ],
  "tools": [{"type": "web_search"}],
  "max_output_tokens": 8000,
  "store": false
}
```

(The prompt is shortened here; `plan` prints it whole.)

- **The lines.** [load_lines](agent_web_search.py#L50) builds them with option 1's code, the same lines `bounded_web_search.py` would submit: `--design web-search` (the default) is the line as a batch job runs today, and `--design bounded-web-search` is option 1's bounded line. With `--lines`, it reads your batch input file instead.
- **The request.** [requests_for](agent_web_search.py#L76) makes each line's request with [apply_overrides](one_request.py#L56). The body is the line's body. The only changes are the ones you ask for (`--deployment` with `--lines`, `--tool`, `--service-tier`, `--reasoning-effort`, ...) and `store: false`, and every change is printed. A test checks that requests built from records equal option 1's batch lines.

## Step 2 - Send one line

```bash
python agent_web_search.py run ../samples/brand-owner/records.jsonl --first 1 --workdir runs/one --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment>
```

```text
1 line(s), request mode: each sent once, as one HTTPS request.
The lines are sent exactly as built: no changes.
  R01: completed in 12.67 s, 1 HTTP request(s), 1 web_search_call item(s) search 1, billed search requests 2
```

[send](one_request.py#L91) makes one `responses.create` call, with the SDK's own retries off ([make_client](../shared/batch.py#L38) with `max_retries=0`), so the count of HTTP requests is exact:

- **The answer comes back in that one response.** Nothing is sent back to the model, there's no `previous_response_id`, and there's no loop over tool calls.
- **Retries only with `--retries`, only after HTTP 408, 429 or 5xx,** honoring `Retry-After`, otherwise backing off with jitter ([line 110](one_request.py#L110)).
- **A timeout or a dropped connection is held, never resent,** because the service may have processed and billed the request ([line 107](one_request.py#L107)).
- **HTTP 401, 403 and 404 stop the run,** because every request would get them, and so does HTTP 400 "Flex tier is not supported for this deployment" ([line 104](one_request.py#L104)). [send_all](agent_web_search.py#L177) then sends nothing more; Ctrl+C stops it the same way, after the requests in flight finish.

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
- **`tool_usage.web_search.num_requests` is 11:** the three searches' 3 + 4 + 4 queries. The `find_in_page` added nothing. Learn bills web search on this count, not on `web_search_call` items ([Web search usage and billing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#web-search-usage-and-billing)), and in all 54 responses here with both counts (the batch job's rows have no query counts), it equalled the queries.
- **Learn describes this agentic search:** with a reasoning model, "the model actively manages the search process and can perform web searches as part of its chain of thought, analyze results, and decide whether to keep searching" ([Options to use web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search#options-to-use-web-search)).
- **Reasoning effort moves the count most.** The same 10 records on gpt-5.4 took 1 `web_search_call` per request with effort not set. With effort medium they took 2 to 13, one with 7 searches, 4 `open_page` and 2 `find_in_page`.

## Step 4 - How a response is measured

[measure](one_request.py#L125) reads the response with [response_metrics](../shared/batch.py#L271), the code the Batch and Flex paths use, and adds:

- the queries in the `search` actions;
- the sources, with `--count-sources`, which adds `include: ["web_search_call.action.sources"]`;
- the URL citations in the answer;
- the output items a client would have to answer (`function_call` and similar; 0 in every run).

It keeps only the fields in [METRIC_FIELDS](one_request.py#L35), so `metrics.jsonl` and `summary.md` hold counts and never response text. When a response has no `tool_usage`, the billed count is `None`, shown as "not reported", and not 0 ([line 141](one_request.py#L141)).

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
 "input": [{"role": "user", "content": "Check this brand record using web search. ..."}], "max_output_tokens": 8000, "store": false}
```

- **The split.** [split_body](prompt_agent.py#L48) moves the line's model, system messages, tools, output schema (`text`), reasoning effort and the other settings a prompt agent's definition takes (`temperature`, `top_p`, `tool_choice`: [DEFINITION_KEYS](prompt_agent.py#L35)) into the definition, because "the model and tool settings belong to the agent definition, not the invocation request" ([Web search tool, image search](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search#image-search-results)). The rest of the line's messages are the call's input. A line setting that neither the definition nor the call takes, such as `metadata`, isn't sent, and `plan` and `run` list it with the changes.
- **What each call carries.** Besides the input, a call sends `max_output_tokens`, `max_tool_calls`, `service_tier` and `store`. All four were accepted, and the agent's responses reported `service_tier` "flex" when a call asked for it.
- **No toolbox.** `split_body` refuses any tool other than `web_search`, including a toolbox attached as an MCP tool: through a toolbox, web search uses Grounding with Bing Search, not Web IQ ([Web search tool](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search#add-web-search-directly-to-an-agent)).
- **The REST calls** are the ones Learn shows for a prompt agent: [create_version](prompt_agent.py#L120) (`POST {project}/agents/{name}/versions`), the call with `agent_reference` through the project's Responses API ([openai_client](prompt_agent.py#L137)), and [delete](prompt_agent.py#L126) (`DELETE {project}/agents/{name}/versions/{version}`, then the agent).
- **Naming and cleanup.** The run ([_run](agent_web_search.py#L244)) refuses a name that exists, creates one version per distinct definition, and deletes the agent when the run ends, also after an error or Ctrl+C, unless `--keep-agent`.

```bash
python agent_web_search.py run ../samples/brand-owner/records.jsonl --workdir runs/agent --mode agent \
    --project-endpoint https://<resource>.services.ai.azure.com/api/projects/<project> --deployment <model deployment>
```

```text
Created agent 'web-search-one-request-<UTC time>' version 1: model <deployment>, tools ['web_search'] attached directly.
  R01: completed in 16.08 s, 1 HTTP request(s), 1 web_search_call item(s) search 1, billed search requests not reported
  ...
Agent 'web-search-one-request-<UTC time>': deleted (GET returns 404)
```

Responses through the project endpoint carry no `tool_usage`, hence "not reported": read the searches on your bill. Each record's tool settings belong to the agent. With option 1's bounded line, whose `user_location` is the record's market, each distinct country gets its own agent version.

## Step 6 - Flex, and Global Batch

- **Flex.** `--service-tier flex` adds `service_tier: "flex"` to each request, on a Global Standard deployment of a Flex-capable model ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)). On gpt-6-sol, 10 of 10 lines answered with one HTTP request each, with 2-4 `web_search_call` items and 3-8 billed requests per response. For a large job on Flex, with pacing, an attempt ledger and resumable runs, use [flex-web-search-and-web-iq](../flex-web-search-and-web-iq/README.md).
- **Global Batch.** The request body is the batch line. The same 10 lines as a Global Batch job, with option 1's runner, gave the same shape as the request run on Global Standard: 1 `web_search_call` and 2-3 billed requests per line ([results](../results/2026-11-20-agent-web-search/README.md)).

## Step 7 - Your own batch file

```bash
python agent_web_search.py run --lines my-batch-input.jsonl --first 5 --workdir runs/mine --mode request \
    --base-url https://<resource>.openai.azure.com/openai/v1/ --deployment <Global Standard deployment> --tool web_search
```

```text
5 line(s), request mode: each sent once, as one HTTPS request.
Changes made to the lines before sending them:
  - model: 'my-batch-deployment' -> '<Global Standard deployment>'
  - tool: 'web_search_preview' -> 'web_search'
  - store: None -> False
```

- **What changes.** A batch line names a Global Batch deployment, so `--deployment` points it at a Global Standard one. `store: false` keeps the service from storing the response. `--tool web_search` replaces the preview tool, which Learn calls "supported but not recommended" ([Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)). In request mode nothing else in the line changes.
- **Agent mode works on your lines too.** System messages become the agent's instructions; lines that share them share one agent version. A setting the agent and the call have no place for isn't sent, and it's listed with the changes.

## If something goes wrong

| What you see | Why, and what to do |
| --- | --- |
| HTTP 429 "exceeded token rate limit" | A request with web search can use tens of thousands of tokens in seconds. Raise the deployment's tokens per minute, send fewer at once, and pass `--retries 3` |
| HTTP 400 "Flex tier is not supported for this deployment" (stops the run) | The deployment's model doesn't take Flex; use a Flex-capable model such as gpt-6-sol |
| HTTP 401 or 403 (stops the run) | Sign in (`az login`, with `--subscription` if you use several), and check your role: Foundry User for agent mode |
| "The project already has an agent named ..." | Agent mode never reuses an agent: pass another `--agent-name` |
| "... already holds a run" | Each run writes to a new `--workdir`, so no line is sent twice by mistake |
| You pressed Ctrl+C (exit code 130) | Lines not sent yet weren't sent; the ones in flight finished and were recorded, and `run.json` and `summary.md` cover what came back. An agent the run created was deleted, unless `--keep-agent`. The command doesn't resume: run it again with a new `--workdir`, and every line is sent again |
| "Agent mode attaches only the web_search tool, directly" | The line has another tool, such as an MCP tool for a toolbox. Use request mode, or remove that tool |
| A response with no `web_search_call` | The model answered without searching. That's its choice: prompt it to search, or set `tool_choice` in the line |
| `--lines` refuses the file | It takes Responses API lines (`url` `/v1/responses`, with a `body`) |

## Sources

- [Web search tool, Foundry Agent Service](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/web-search): direct web search uses Web IQ, a toolbox uses Grounding with Bing Search; prompt agents over REST; settings in the definition.
- [Web search, Azure OpenAI Responses API](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search): the `web_search` tool, agentic search with reasoning models, the response shape, usage and billing, `include`.
- [Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing): `service_tier: "flex"`, supported models, no SLA.
- The runs: [results/2026-11-20-agent-web-search](../results/2026-11-20-agent-web-search/README.md).
