# 07 - Deploy and run

This guide takes you from an empty Azure subscription to a finished job on the sample, then to your own records. Every command runs from the repository root. Option 1 needs only a Global Batch deployment; the Web IQ resource, the Foundry project and the toolbox are for [option 2](../option-2-two-stage-web-iq/README.md) and Flex with Web IQ. Each option folder's WALKTHROUGH follows one record through the same steps, with the code behind each.

## What you need

| Item | Why | Reference |
| --- | --- | --- |
| A paid Azure subscription | Web IQ requires a paid subscription; Azure credits don't apply to it | [Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#which-azure-subscriptions-can-i-use) |
| A Microsoft Web IQ resource, connected in the Web IQ portal | Connecting the resource creates your Web IQ AppID, which holds your API keys, allowed APIs and rate limit (15 queries per second by default) | [Web IQ overview](https://learn.microsoft.com/en-us/web-iq/overview), [Quickstart](https://learn.microsoft.com/en-us/web-iq/get-started/quickstart) |
| A Microsoft Foundry resource and project | The project holds the Web IQ connection and the toolbox that stage 1 calls | [Toolbox](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox) |
| A Global Batch (or Data Zone Batch) deployment of your model | Option 1, and option 2's stage 2, run on the Batch API | [Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch), [Deployment types](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types) |
| Or, to run on Flex instead of Batch: a Global Standard deployment of a Flex-capable model, such as gpt-6-sol | [flex_job.py](../flex-web-search-and-web-iq/flex_job.py) sends each line as a synchronous request with `service_tier: "flex"` ([Run on Flex instead of Batch](#run-on-flex-instead-of-batch)) | [Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing) |
| Python 3.10 or later, and the Azure CLI | The sample is Python; sign-in uses Microsoft Entra ID | [requirements.txt](../requirements.txt) |

The sample was tested with Python 3.12 and the package versions noted in [requirements.txt](../requirements.txt).

## 1. Install and test offline

```bash
git clone <this repository> search-then-batch
cd search-then-batch
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

The tests call no Azure or Web IQ service, and price with the saved 31 October 2026 table. Defaults, limits and retry settings are in [shared/settings.toml](../shared/settings.toml); a command-line option overrides them for one run. There's nothing to install beyond the requirements: each option's script finds the [shared/](../shared/) folder on its own.

## 2. Forecast the sample (no cost)

```bash
python option-2-two-stage-web-iq/two_stage.py plan samples/brand-owner/records.jsonl --plan lean --show-searches
```

You get every planned search, how many records share it, and a cost forecast at current list prices, read from the public Azure Retail Prices API without signing in; nothing in your subscription is called. Without internet access, pass `--prices tests/fixtures/prices-2026-10-31.json` (the 31 October 2026 table) or a table you saved with `python shared/prices.py --save <file>`. Change `--plan full`, `--price-model gpt-5.4` or `--max-results` to see how each choice moves the forecast. Option 1 can't be forecast this way, because the model decides how many searches to run: `python option-1-bounded-web-search/bounded_web_search.py plan samples/brand-owner/records.jsonl` prints only its prompt tokens.

## 3. Create the Azure resources

**Global Batch deployment.** In the Foundry portal, deploy the model with the deployment type **Global-Batch**, or use the Azure CLI. The SKU name in code is `GlobalBatch` ([Deployment types](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types)):

```bash
az cognitiveservices account deployment create \
  --name <foundry-resource> --resource-group <resource-group> \
  --deployment-name gpt-5.4-mini-batch \
  --model-name gpt-5.4-mini --model-version <version> --model-format OpenAI \
  --sku-name GlobalBatch --sku-capacity <capacity>
```

The simplest way to size the deployment is in the Foundry portal, which shows how much capacity you can assign. A Global Batch deployment draws on the model's enqueued-token quota: tokens in submitted files count against it until each job finishes. For gpt-5.4 and gpt-5.4-mini, the default Global Batch quota is 200 million enqueued tokens, 5 billion for Enterprise and MCA-E subscriptions ([Batch quota](https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits#batch-quota)). Enable dynamic quota, which the Batch documentation recommends for all Global Batch deployments ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). If your data must be processed inside a data zone (US, EU or APAC), use **Data Zone Batch** (`DataZoneBatch`) instead; Global types may process data in any Azure region ([Deployment types](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types)). Web IQ has no data-residency commitment either way ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#what-data-residency-commitments-does-web-iq-provide)).

**Web IQ key.** Create a Web IQ resource in your Azure subscription ([portal](https://learn.microsoft.com/en-us/web-iq/get-started/deploy-azure-portal) or [Azure CLI](https://learn.microsoft.com/en-us/web-iq/get-started/deploy-azure-cli)), then sign in to the Web IQ portal and connect it. That creates your Web IQ AppID. Create an API key in the portal's [Profile Management](https://webiq.microsoft.ai/profiles) page ([Web IQ authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)). Store it in a secret manager, not in code or source control.

**Web IQ connection in the Foundry project.** In the Foundry portal, open your project, go to **Build** > **Tools**, and connect Web IQ with that key. If Web IQ isn't in your tool catalog, choose **Connect a tool** > **Custom** > **MCP** and enter the server `https://api.microsoft.ai/v3/mcp` with key-based authentication on the header `x-apikey` ([Custom MCP tool](https://learn.microsoft.com/en-us/azure/foundry/mcp/build-your-own-mcp-server#connect-by-using-a-custom-mcp-tool), [MCP authentication](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/mcp-authentication), [Web IQ MCP server](https://learn.microsoft.com/en-us/web-iq/integrations/mcp)). Either way, the key is stored in a project connection. Note the connection's **name**: `toolbox.py create --connection` takes the name, which the toolbox API calls `project_connection_id`.

**Roles.**

| Identity | Needs | Notes |
| --- | --- | --- |
| Whoever creates or calls the toolbox (you, or the job's managed identity) | **Foundry User** on the project | Required by the toolbox ([Toolbox prerequisites](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox)) |
| Whoever uploads files and creates batch jobs | A role with Azure OpenAI files and batches data actions on the Foundry resource | **Cognitive Services OpenAI Contributor** includes `Microsoft.CognitiveServices/accounts/OpenAI/*`; **Cognitive Services OpenAI User** covers responses and reads but not file uploads ([Built-in roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/ai-machine-learning)). Our tests ran with a broader role; we didn't test the least-privilege combination |

Role definitions above were read with `az role definition list` on 31 October 2026.

## 4. Create and test the toolbox

```bash
az login
python option-2-two-stage-web-iq/toolbox.py create \
  --project-endpoint https://<foundry-resource>.services.ai.azure.com/api/projects/<project> \
  --name webiq-search --connection <web-iq-connection-name>
python option-2-two-stage-web-iq/toolbox.py test \
  --project-endpoint https://<foundry-resource>.services.ai.azure.com/api/projects/<project> \
  --name webiq-search
```

`create` prints the consumer endpoint (`.../toolboxes/webiq-search/mcp?api-version=v1`); pass it as `--toolbox-url`. `test` lists the toolbox's tools and runs one search (one billable Web IQ call). The tool appeared as `webiq___web` in our tests; the code discovers the name with `tools/list` instead of assuming it, because the toolbox page describes a `{server_label}.{tool_name}` format ([Toolbox](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox#verify-tool-availability)).

**Without a toolbox.** Use `--via rest` to call the Web IQ REST API directly, with your key in the `WEBIQ_API_KEY` environment variable, or `--webiq-auth entra` for an app-only Microsoft Entra token (scope `https://api.microsoft.ai/.default`, client ID bound to your AppID in the Web IQ portal) ([Web IQ authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)). The Entra REST path is implemented but wasn't tested for this sample.

## 5. Run the sample

Option 2, the lowest-cost option on the sample:

```bash
python option-2-two-stage-web-iq/two_stage.py run samples/brand-owner/records.jsonl \
  --workdir runs/lean-mini --plan lean \
  --toolbox-url "https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/webiq-search/mcp?api-version=v1" \
  --base-url https://<foundry-resource>.openai.azure.com/openai/v1/ \
  --deployment gpt-5.4-mini-batch --price-model gpt-5.4-mini --max-cost 1
```

What happens, in order (the [job flow diagram](../diagrams/02-job-flow.png) shows the same; [option 2's walkthrough](../option-2-two-stage-web-iq/WALKTHROUGH.md#step-5---run-the-whole-job) links each step to its code):

1. Builds the search plan and a forecast; stops before calling anything if the forecast is above `--max-cost` (USD for the whole job).
2. Runs the searches: rate-limited (`--qps`, default 5), retried with backoff, and saved one by one as each finishes, so an interrupted run never repeats a finished search. Searches that still fail with HTTP 408, 429 or 5xx, or a network error, run again in a second and a third round. The records that use a search that still fails are held: they aren't sent to the model, and `held-records.jsonl` lists them at the end.
3. Builds the no-tools batch file, counts its prompt text exactly, forecasts again, and stops if that's above `--max-cost`.
4. Uploads the file, creates the batch job and waits until it ends, printing progress every `--poll-seconds`. A job over 100,000 lines or 200 MB runs as several batch jobs, waited on together. If the deployment's enqueued-token quota is full, whether the create is refused or the job fails after validating, it waits and creates the job again when there's room. Input and output files are created with a 14-day expiry as a backstop, in case cleanup never runs ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)).
5. Collects the output, and sends lines that failed with HTTP 408, 429 or 5xx, or that an expired job never processed, again in a new batch job, up to three rounds in all. Then writes `results.jsonl` (one decision per record, in the records' order) and `run.json` (plan, forecasts, search attempts, batch jobs, held records, actual cost, timings). Prices are read once, when the job starts, and saved as `prices-used.json`, so a resumed job is priced the same way.
6. Deletes the job's files from Azure OpenAI and the local `evidence.jsonl` and `batch-input.jsonl`.

Option 1 needs only the Global Batch deployment. It builds the lines and goes straight to step 4; it has no `--max-cost`, because the model decides how many searches to run ([option 1's walkthrough](../option-1-bounded-web-search/WALKTHROUGH.md#step-3---run-the-job)):

```bash
python option-1-bounded-web-search/bounded_web_search.py run samples/brand-owner/records.jsonl \
  --workdir runs/bounded-mini \
  --base-url https://<foundry-resource>.openai.azure.com/openai/v1/ \
  --deployment gpt-5.4-mini-batch --price-model gpt-5.4-mini
```

If anything interrupts either command, run the same command again: it resumes from the last completed step, and never sends a line that was already answered. While a job runs, the work folder's `state.json` holds the endpoint and job IDs, and `kept-lines.jsonl` the lines already settled; both are deleted when the job finishes (unless `--keep-working-files`), and they're in [.gitignore](../.gitignore). [Run it unattended](#run-it-unattended) lists what `run` handles by itself, what it never sends again, and the exit codes a scheduler can act on.

The `--base-url` is the Azure OpenAI endpoint of the Foundry resource (`https://<resource>.openai.azure.com/openai/v1/`), the endpoint the Batch documentation's examples use ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). The Foundry project endpoint isn't used for batch.

## 6. Check quality and compare designs

```bash
python evaluation/evaluate.py runs/lean-mini --labels samples/brand-owner/labels.jsonl
```

To reproduce every row in [09](09-results-on-the-sample.md), run the same records once per design, each in its own `--workdir`:

| Row | Script | Options that differ from the commands in step 5 |
| --- | --- | --- |
| Model-driven web search | `option-1-bounded-web-search/bounded_web_search.py run` | `--design web-search --max-output-tokens 16000` |
| Bounded web search | `option-1-bounded-web-search/bounded_web_search.py run` | None: `--max-tool-calls 2` is the default |
| Search-then-batch, full plan | `option-2-two-stage-web-iq/two_stage.py run` | `--plan full --qps 3` |
| Search-then-batch, lean plan | `option-2-two-stage-web-iq/two_stage.py run` | `--qps 3` |
| The same on gpt-5.4 | Either | `--deployment <gpt-5.4 Global Batch deployment> --price-model gpt-5.4` |

Then pass every work folder to `evaluate`. Each run's `run.json` records the settings it used.

## 7. Share results without leaking your environment

```bash
python evaluation/report.py runs/lean-mini --out results/my-run --redact-records R22,R23
```

`report` copies only `run.json`, `results.jsonl`, `metrics.jsonl` and `prices-used.json`, and refuses if they contain endpoints, local paths or job IDs. It never copies `state.json`, `evidence.jsonl` or `batch-input.jsonl`. It removes citation URLs that point to pages about individuals (profile, social media and company-officer pages). `--redact-records` also removes the findings and citations of the records you name, keeping their statuses so `evaluate` still works. The published sample results used it for the two fictional records, whose evidence was about unrelated real businesses and people. Read your results before publishing anyway: no pattern list catches everything.

## 8. Clean up

```bash
python option-2-two-stage-web-iq/toolbox.py delete --project-endpoint <project endpoint> --name webiq-search
```

Global Batch is billed per token: the Azure Retail Prices API lists only per-token meters for it, so an idle deployment adds no charge. Delete it anyway if you don't need it. Delete the Web IQ connection if you created it only for this sample.

## Run on Flex instead of Batch

Flex processing (preview) runs the same lines as synchronous requests with `service_tier: "flex"`, at about half the Standard token price, on a **Global Standard** deployment of a Flex-capable model such as gpt-6-sol ([Flex processing](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/flex-processing)). Use [flex_job.py](../flex-web-search-and-web-iq/flex_job.py) with `--design two-stage` (Flex with Web IQ) or `--design bounded-web-search` (Flex with web search), name that deployment, and price with its Flex rates:

```bash
python flex-web-search-and-web-iq/flex_job.py plan samples/brand-owner/records.jsonl --design two-stage --plan lean --price-model gpt-6-sol
python flex-web-search-and-web-iq/flex_job.py run samples/brand-owner/records.jsonl --design two-stage --plan lean --workdir runs/flex-lean \
  --deployment <gpt-6-sol Global Standard deployment> --price-model gpt-6-sol \
  --toolbox-url "https://<foundry-resource>.services.ai.azure.com/api/projects/<project>/toolboxes/webiq-search/mcp?api-version=v1" \
  --base-url https://<foundry-resource>.openai.azure.com/openai/v1/ --flex-concurrency 2 --max-cost 1
```

- **Models.** A deployment whose model doesn't support Flex answers HTTP 400, and the job stops before sending more. The gpt-5.4 family didn't support Flex when we checked.
- **Pacing.** Flex shares the deployment's tokens-per-minute quota with Standard requests: size `--flex-concurrency` to it.
- **Resuming.** Every attempt is recorded in the work folder's `flex-attempts.jsonl` before it's sent, and a re-run of the same command never sends a completed line again.
- **Rounds and held lines.** Lines that use up their attempts on HTTP 408, 429 or 5xx are sent again in a second and a third round; after that they're held, and `--retry-failed` sends them again. A request that timed out, lost its connection, or was in flight when a run stopped may already have been billed, so its line is held and sent again only with `--retry-ambiguous`.
- **Roles.** Calling the deployment needs a role with the responses data action, such as Cognitive Services OpenAI User; no file uploads are involved.

For Flex with web search, use `--design bounded-web-search` and drop `--plan`, the toolbox and `--max-cost`. [Flex with web search or Web IQ](../flex-web-search-and-web-iq/README.md) explains the retry rules and how the cost is calculated, and its [walkthrough](../flex-web-search-and-web-iq/WALKTHROUGH.md) follows one request, attempt by attempt.

## Run it unattended

One `run` command per job takes it to the end without anyone watching. It retries what retrying can fix, and stops only for what it can't.

### What runs by itself

| Problem | What `run` does | Settings ([settings.toml](../shared/settings.toml)) |
| --- | --- | --- |
| A Web IQ search gets HTTP 408, 429 or 5xx, or a network error | Retries it up to 6 times, waiting as long as Web IQ asks. A search asked to wait longer than 600 seconds waits for the next round instead. Searches that still fail run again in round 2 and round 3, after 60 and 300 seconds, or after the wait Web IQ asked for if that's longer, up to an hour | `[webiq] max_attempts`, `max_retry_after_seconds`, `auto_rounds`, `round_waits_seconds`, `max_round_wait_seconds` |
| Web IQ answers a search made through a Foundry toolbox with an HTTP error | The toolbox relays a 429 as an HTTP 200 response whose JSON-RPC message holds an error (code `-32008`) saying `Response status code does not indicate success: 429 (Too Many Requests)` and `Retry after: 60s`, with no `Retry-After` header; other statuses probably arrive the same way, though only the 429 has been seen live. The client reads the status, and for a 429 the wait, from that text: a 429 is waited out, and a 400, 401, 403, 404 or 422 isn't sent again. Over REST, the wait is read from the 429's body (`"retryAfter": "60s"`) | - |
| Searches with idle gaps between them | Keeps idle connections open for 120 seconds. Measured on 2 November 2026 through a toolbox: after 6 seconds or more idle, a search took 1.6-1.9 seconds with the HTTP client's default 5-second keep-alive, which reconnected each time, against 0.38-0.49 seconds with the connection kept open | `[webiq] keepalive_seconds` |
| A search still fails after round 3 | Holds the records that use it, and sends the others to the model | - |
| More than 100,000 lines or 200 MB | Splits the lines into batch jobs of at most 100,000 lines and 180 MB, submits them and waits for them together, then merges the results in the records' order | `[batch] part_bytes` |
| The deployment's enqueued-token quota is full | In 10 regions (australiaeast, eastus, eastus2, germanywestcentral, italynorth, northcentralus, polandcentral, swedencentral, switzerlandnorth and westus), creating a job fails at once with HTTP 400 `token_limit_exceeded`, and nothing is created or billed. Elsewhere the job is created, validates for up to a few minutes, then fails with `token_limit_exceeded` ([Batch: queueing batch jobs](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch#queueing-batch-jobs)). Either way, `run` creates the job again from the file it already uploaded, after 5 seconds, doubling up to 600, for up to 24 hours of waiting in one run; a job that failed this way isn't a round. Then it exits with 75, and running the same command again tries again | `[batch] quota_wait_minutes` |
| Creating a job gets no answer: the run is stopped while the create is in flight, or the call times out, loses its connection or fails with 5xx | The create is sent with the SDK's own retries off, because the SDK would send it again with no idempotency key. Each attempt is recorded in `state.json` before it's sent, and before each new attempt `run` lists the batch jobs and waits for one already made from the same file instead: two jobs from one file would bill twice. If the list gets no answer (a timeout, a dropped connection, HTTP 408, 409, 429 or 5xx), the run exits with 75; if listing is refused (a sign-in or access problem: HTTP 401, 403 or 404, or no Microsoft Entra ID token), it exits with 1. Either way nothing is deleted, and the next run lists the jobs before anything else. Once an attempt may have gone through, the uploaded file and the record of the attempts are kept, whatever happens next. One risk remains: if the service's job list lags behind a create that went through, a re-run could still miss that job and create a second one. Listing first narrows that window, but it can't close it without an idempotency key | - |
| A batch job runs for hours | Waits until every job ends, with no time limit unless you set one | `--max-wait-minutes` (`[batch] max_wait_minutes`) |
| Batch lines fail with HTTP 408, 429 or 5xx, or a job expires before processing them | Sends them again in a new batch job, up to 3 rounds in all. The rounds count from the run that sent the lines, so lines sent again after a person's fix, or after a run with `--no-auto-retry`, get all 3 again | `[batch] max_rounds` |
| Flex lines use up their 3 attempts on HTTP 408, 429 or 5xx | Sends them again in round 2 and round 3, after 60 and 300 seconds. A `Retry-After` longer than 600 seconds is waited out before the next round, for up to an hour | `[flex] auto_rounds`, `round_waits_seconds`, `max_round_wait_seconds` |

`--no-auto-retry` turns the rounds off: failed searches and lines then wait for a re-run of the same command (on Flex, with `--retry-failed`).

### What is never sent again automatically, and why

- **A line that was answered**, including an incomplete answer: sending it again pays twice.
- **A line that failed with another 4xx**, such as a bad request: it fails the same way every time.
- **A Web IQ search refused with HTTP 400, 401, 403, 404 or 422**: a bad query, a wrong key or missing access fails the same way every time. Through a toolbox such a refusal probably arrives inside an HTTP 200 response, as a relayed 429 does (only the 429 has been seen live); the client reads the status either way. If every record is held for that reason, the run exits with 1; fix the cause and run the same command again, and the failed searches run again.
- **A Flex request that timed out, lost its connection, or was in flight when a run stopped**: the service may have processed and billed it. `--retry-ambiguous` sends it again, if you accept paying twice.
- **The lines of a batch job someone cancelled outside the run**: someone stopped it on purpose, so they're held. A job the run cancelled itself, with `--cancel-on-stall`, is sent again.
- **A batch job that failed validation**: the input must be fixed first (exit code 1). A job that failed because the quota was full (`token_limit_exceeded`) is created again, as above.

### Held records

A record is held when it can't be answered now but might be later: a search it needs still failed, its line still failed after the last round, or its batch job was cancelled outside the run. A held record isn't sent to the model again by this job. It's in `results.jsonl` with the status `held` and an error that says why, it's counted in `run.json` (`held`), and it's written to `held-records.jsonl` in the work folder, in the input records format, so you can run it as a new job later.

### Exit codes

Every option's `run` uses the same exit codes ([exits.py](../shared/exits.py)):

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Done: every line was answered, or failed for good with an error retrying can't fix (listed in `results.jsonl`) | Nothing |
| `1` | Needs a person: bad input, sign-in or permissions, a `--max-cost` forecast, a batch job that failed validation, or every record held for a reason retrying can't fix (another 4xx, such as 401 or 403) | Read the message, fix the cause, and run the same command again |
| `3` | Finished, but records are held after the automatic rounds; `held-records.jsonl` lists them | Run `held-records.jsonl` as a new job later. On Flex you can instead add `--retry-failed` or `--retry-ambiguous` in the same work folder |
| `75` | Still running or waiting: `--max-wait-minutes` or `quota_wait_minutes` ran out, a create whose answer was lost couldn't be checked yet (the batch jobs couldn't be listed), a Flex line is still asked to wait, or `--no-auto-retry` left work for a re-run | Run the same command again later |
| `130` | Interrupted with Ctrl+C | Run the same command again to resume |

Running a finished job again sends nothing and exits with the same code, 0 or 3.

### Scheduling

A scheduler, for example a scheduled [Azure Container Apps job](https://learn.microsoft.com/en-us/azure/container-apps/jobs), runs one command per job and acts on its exit code: nothing on 0; alert someone on 1; on 3, queue `held-records.jsonl` as a new job or alert someone; on 75 or 130, run the same command again later. Keep the work folder on storage that outlives the container, because the re-run resumes from it. To bound how long one execution runs, add `--max-wait-minutes` and let the scheduler re-run the command on 75.

## Running your own records at scale

- **File limits.** Up to 100,000 requests and 200 MB per batch input file (1 GB with your own storage) ([Batch limits](https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits#batch-limits)). `run` splits a larger job into several batch jobs and waits for them together.
- **Split by shared key.** Searches are de-duplicated within a job only, so one large job shares more searches than several small ones. If you split records into several jobs yourself, sort them by the field that drives shared searches (the owner, in the sample) first, so each shared search runs once instead of once per job.
- **Search cap.** `run` refuses a plan with more than 3 searches per record (`max_queries_per_record`), a guard against a bad search template; `--max-queries` sets an absolute cap instead.
- **Search throughput.** The Web IQ limit (15 queries per second by default) is shared by every key and Entra client ID of the AppID; requests above it get HTTP 429, which `run` waits out. Set `--qps` so all concurrent jobs together stay below it, or request a higher limit with your expected traffic ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-there-a-rate-limit)). At 10 queries per second, 100,000 searches take about 2.8 hours.
- **Batch throughput.** Each deployment has an enqueued-token quota; very large jobs can queue behind each other, and `run` waits while the quota is full. See "Queueing batch jobs" in [Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch#queueing-batch-jobs).
- **Where to run it.** Any host with Python and an Entra identity works. A scheduled [Azure Container Apps job](https://learn.microsoft.com/en-us/azure/container-apps/jobs) with a managed identity is a natural fit; we haven't tested that hosting. [Scheduling](#scheduling) says how to use the exit codes.
- **Turnaround.** Batch targets 24 hours and doesn't expire jobs that run longer ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). `run` waits in one process until every job ends, or for `--max-wait-minutes`; re-running resumes the wait.

## Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `No Web IQ 'web' tool in the toolbox` | The connection's key is missing or wrong, so the toolbox can't list the server's tools | Check the project connection; the toolbox page lists this cause for empty `tools/list` results ([Toolbox troubleshooting](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox)) |
| HTTP 401 from the toolbox | Expired token or wrong scope | The code refreshes tokens for `https://ai.azure.com/.default`; run `az login` again |
| HTTP 403 from Web IQ REST with `--webiq-auth entra` | Your Entra client ID isn't bound to the AppID | Bind it in the Web IQ portal ([Web IQ authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)) |
| HTTP 429 during stage 1 | Above the AppID's rate limit, possibly because of other callers. Through a toolbox it arrives as a JSON-RPC error (`-32008`, `auth_user_throttled`) | The code waits as long as Web IQ asks and runs failed searches again in later rounds; lower `--qps` |
| `Every record is held: searches failed with HTTP 401` (or 403), exit code 1 | Web IQ refused the searches: the key in the toolbox's connection, or your access. Through a toolbox the refusal may arrive inside an HTTP 200 response; the code reads its status either way | Fix the key or the access, and run the same command again: the failed searches run again |
| `Validation failed: A valid model deployment name couldn't be extracted` | A byte-order mark in the file, or lines that target different deployments | The tool writes UTF-8 without a BOM and refuses mixed deployments ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)) |
| The job shows `in_progress` with 0 completed for a long time | Small jobs often report progress only at the end | Wait; `--stall-minutes` warns, `--cancel-on-stall` cancels, and the lines that weren't answered are sent again in a new job |
| The job stays `cancelling` | Cancellation finishes in-flight work first; a 3-line test job took about 22 minutes to reach `cancelled` | Wait: `run` keeps polling until the job reaches a final state, then keeps completed lines |
| `A batch job failed validation`, exit code 1 | A problem in the input file or the deployment; the errors are in the message and in `run.json` (`batches`) | Fix the cause and run the same command again: settled lines are kept, and only the others are sent again, without searching again |
| `token_limit_exceeded`, then exit code 75 | The deployment's enqueued-token quota stayed full for `quota_wait_minutes`: creates were refused, or jobs failed with `token_limit_exceeded` after validating | Run the same command again later: it creates the jobs again from the files it already uploaded |
| `a create may have gone through, but the batch jobs couldn't be listed`, exit code 75 | A create got no answer, and the check for the job it may have made got no answer either (a timeout, a dropped connection, HTTP 408, 409, 429 or 5xx) | Run the same command again later: it lists the jobs first, waits for the job if it exists, and creates it only if it doesn't |
| `a create may have gone through, but listing the batch jobs to check was refused`, exit code 1 | A create got no answer, and the check was refused: an expired sign-in, missing access (HTTP 401 or 403) or a wrong endpoint (HTTP 404) | Fix the sign-in (`az login`), your access or the endpoint, then run the same command again: it lists the jobs first, waits for the job if it exists, and creates it only if it doesn't |
| `Stopped waiting (--max-wait-minutes)`, exit code 75 | The batch jobs are still running | Run the same command again later to collect them |
| `Forecast ... is above --max-cost` | The job is bigger than you allowed | Raise `--max-cost` deliberately, or split the job |
| Records with the status `held`, exit code 3 | Searches or lines still failing after the automatic rounds, or a batch job cancelled outside the run; each record's `error` in `results.jsonl` says which | Run `held-records.jsonl` as a new job later |
| `... searches to run, above the safety cap` | More than 3 searches per record: usually a search template that varies more than intended | Check the plan with `plan --show-searches`; pass `--max-queries` to raise the cap deliberately |
| Forecast says `estimate` instead of `exact` | `tiktoken` isn't installed, or couldn't download its encoding file | `pip install tiktoken` on a machine with internet access |
| `Couldn't read list prices from the Azure Retail Prices API` | No internet access, or Microsoft renamed a meter named in [settings.toml](../shared/settings.toml) | Pass `--prices <file>` with a saved table, or find the new meter name with `python shared/prices.py --contains <text>` and update settings.toml |
