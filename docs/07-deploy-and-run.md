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
2. Runs the searches: rate-limited (`--qps`, default 5), retried with backoff, and saved one by one as each finishes, so an interrupted run never repeats a finished search. Stops before submitting if any search failed.
3. Builds the no-tools batch file, counts its prompt text exactly, forecasts again, and stops if that's above `--max-cost`.
4. Uploads the file, creates the batch job and waits, printing progress every `--poll-seconds`. Input and output files are created with a 14-day expiry as a backstop, in case cleanup never runs ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)).
5. Collects the output, writes `results.jsonl` (one decision per record) and `run.json` (plan, forecasts, search attempts, actual cost, timings). Prices are read once, when the job starts, and saved as `prices-used.json`, so a resumed job is priced the same way.
6. Deletes the job's files from Azure OpenAI and the local `evidence.jsonl` and `batch-input.jsonl`.

Option 1 needs only the Global Batch deployment. It builds the lines and goes straight to step 4; it has no `--max-cost`, because the model decides how many searches to run ([option 1's walkthrough](../option-1-bounded-web-search/WALKTHROUGH.md#step-3---run-the-job)):

```bash
python option-1-bounded-web-search/bounded_web_search.py run samples/brand-owner/records.jsonl \
  --workdir runs/bounded-mini \
  --base-url https://<foundry-resource>.openai.azure.com/openai/v1/ \
  --deployment gpt-5.4-mini-batch --price-model gpt-5.4-mini
```

If anything interrupts either command, run the same command again: it resumes from the last completed step. If the batch itself ends `failed`, `expired` or `cancelled`, `run` deletes the job's files in Azure OpenAI and keeps three things locally: the lines that completed (they were billed), the evidence and the batch file. Running the same command again resubmits only the other lines, without searching again, and the final results and cost include the kept lines. While a job runs, the work folder's `state.json` holds the endpoint and job IDs; it's deleted when the job finishes (unless `--keep-working-files`), and it's in [.gitignore](../.gitignore).

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
- **Held lines.** Lines that used up their attempts are sent again only with `--retry-failed`. A request that timed out, lost its connection, or was in flight when a run stopped may already have been billed, so its line is held and sent again only with `--retry-ambiguous`.
- **Roles.** Calling the deployment needs a role with the responses data action, such as Cognitive Services OpenAI User; no file uploads are involved.

For Flex with web search, use `--design bounded-web-search` and drop `--plan`, the toolbox and `--max-cost`. [Flex with web search or Web IQ](../flex-web-search-and-web-iq/README.md) explains the retry rules and how the cost is calculated, and its [walkthrough](../flex-web-search-and-web-iq/WALKTHROUGH.md) follows one request, attempt by attempt.

## Running your own records at scale

- **File limits.** Up to 100,000 requests and 200 MB per batch input file (1 GB with your own storage) ([Batch limits](https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits#batch-limits)). Split larger jobs.
- **Split by shared key.** Searches are de-duplicated within a job only. Sort records by the field that drives shared searches (the owner, in the sample) before you split, so each shared search runs once instead of once per file.
- **Search throughput.** The Web IQ limit (15 queries per second by default) is shared by every key and Entra client ID of the AppID; requests above it get HTTP 429. Set `--qps` so all concurrent jobs together stay below it, or request a higher limit with your expected traffic ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-there-a-rate-limit)). At 10 queries per second, 100,000 searches take about 2.8 hours.
- **Batch throughput.** Each deployment has an enqueued-token quota; very large jobs can queue behind each other. See "Queueing batch jobs" in [Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch#queueing-batch-jobs).
- **Where to run it.** Any host with Python and an Entra identity works. A scheduled [Azure Container Apps job](https://learn.microsoft.com/en-us/azure/container-apps/jobs) with a managed identity is a natural fit; we haven't tested that hosting.
- **Turnaround.** Batch targets 24 hours and doesn't expire jobs that run longer ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). `run` waits in one process; re-running resumes the wait.

## Troubleshooting

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| `No Web IQ 'web' tool in the toolbox` | The connection's key is missing or wrong, so the toolbox can't list the server's tools | Check the project connection; the toolbox page lists this cause for empty `tools/list` results ([Toolbox troubleshooting](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/tools/toolbox)) |
| HTTP 401 from the toolbox | Expired token or wrong scope | The code refreshes tokens for `https://ai.azure.com/.default`; run `az login` again |
| HTTP 403 from Web IQ REST with `--webiq-auth entra` | Your Entra client ID isn't bound to the AppID | Bind it in the Web IQ portal ([Web IQ authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)) |
| HTTP 429 during stage 1 | Above the AppID's rate limit, possibly because of other callers | The code retries with backoff; lower `--qps` |
| `Validation failed: A valid model deployment name couldn't be extracted` | A byte-order mark in the file, or lines that target different deployments | The tool writes UTF-8 without a BOM and refuses mixed deployments ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)) |
| The job shows `in_progress` with 0 completed for a long time | Small jobs often report progress only at the end | Wait; `--stall-minutes` warns, `--cancel-on-stall` cancels |
| The job stays `cancelling` | Cancellation finishes in-flight work first; a 3-line test job took about 22 minutes to reach `cancelled` | Wait: `run` keeps polling until the job reaches a final state, then keeps completed lines |
| `The batch ended 'failed'` (or `expired`, `cancelled`) | Validation, quota or service problems; check the errors in `run.json` (`incomplete_batches`) | Fix the cause and run the same command again: completed lines are kept, and only the others are resubmitted, without searching again |
| `Forecast ... is above --max-cost` | The job is bigger than you allowed | Raise `--max-cost` deliberately, or split the job |
| `N searches failed; nothing was submitted` | Network errors or Web IQ errors that outlasted the retries | Re-run the same command; only the failed searches run again |
| Forecast says `estimate` instead of `exact` | `tiktoken` isn't installed, or couldn't download its encoding file | `pip install tiktoken` on a machine with internet access |
| `Couldn't read list prices from the Azure Retail Prices API` | No internet access, or Microsoft renamed a meter named in [settings.toml](../shared/settings.toml) | Pass `--prices <file>` with a saved table, or find the new meter name with `python shared/prices.py --contains <text>` and update settings.toml |
