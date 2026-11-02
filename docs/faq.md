# FAQ

**Is this an official Microsoft product?**
No. It's a sample with source code, measured results and guidance. The services it uses (Azure OpenAI Batch, Microsoft Foundry, Microsoft Web IQ) have their own documentation and support; see [SUPPORT.md](../SUPPORT.md).

**How much will I save?**
It depends on how much of your bill is search today and how many searches your records share. On the 23-record sample, the same model went from $43.12 to $9.78 per 1,000 records (77% less). Most of the saving came from search: half the price per call and fewer calls per record. Tokens contributed about $1 per 1,000 records ([04](04-cost-model.md#measured-on-the-sample)). Run `analyze` on one of your batch output files to see your search share, and `plan` on a records file to see your search count, before you build anything.

**Is it as accurate as letting the model search?**
On the sample, two-stage got 44 of 46 facts right and model-driven web search 42 of 46, on the same model. Twenty-three records can't settle accuracy. Measure it on a few hundred of your own labeled records ([08](08-evaluate-quality.md)).

**Why Web IQ rather than calling the built-in web search tool once per search?**
The built-in tool runs inside a model call, so every search also pays for model tokens, and the model can reword or add searches. Web IQ is called directly, with no model in the loop, and costs $7 per 1,000 calls against $14 per 1,000 Bing requests at list prices ([Web IQ pricing](https://learn.microsoft.com/en-us/web-iq/resources/pricing), [Grounding with Bing pricing](https://www.microsoft.com/en-us/bing/apis)).

**Do I need the Foundry toolbox?**
No. It keeps the Web IQ key out of your code and lets the job use Microsoft Entra ID. `--via rest` calls the Web IQ REST API directly with a key or an app-only Entra token ([02](02-the-search-then-batch-pattern.md#where-stage-1-runs)).

**Can I cache search results across jobs to save more?**
No. The Web IQ terms don't allow a database or persistent cache of content ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#can-i-cache-or-store-web-iq-content)). This sample keeps evidence only for the job's duration and deletes it at the end; have your legal team confirm that fits your use ([06](06-security-privacy-and-terms.md#web-iq-terms-storing-content)).

**How fresh is the evidence?**
Minutes to hours old when the batch runs, because Batch targets a 24-hour turnaround ([Batch](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/batch)). Use the pattern for facts that change slowly.

**What if Web IQ is down?**
Web IQ has no SLA ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#does-web-iq-include-an-sla-rto-or-rpo-commitment)). A failed search is retried, then run again in up to three rounds. The records that use a search that still fails are held: they aren't submitted, and `held-records.jsonl` lists them to run again as a new job ([07](07-deploy-and-run.md#held-records)).

**Is my data protected the same way as Azure OpenAI data?**
No. Web IQ is outside the Data Protection Addendum and the EU Data Boundary. So is Grounding with Bing, used by the built-in web search tool ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-web-iq-covered-by-the-azure-data-protection-addendum-or-eu-data-boundary), [Web search](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/web-search)). Only send fields you're allowed to send; see [06](06-security-privacy-and-terms.md).

**Can I run it inside a private network?**
The batch side can use your Azure networking. Web IQ has only a public endpoint, with no Private Link or IP allowlisting, so the job host needs egress to `api.microsoft.ai` ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#does-web-iq-support-private-link-private-endpoints-vnet-integration-or-ip-allowlisting)).

**How many searches per second can I run?**
15 per Web IQ AppID by default, shared by every key and client ID of that AppID; you can request more ([Web IQ FAQ](https://learn.microsoft.com/en-us/web-iq/resources/faq#is-there-a-rate-limit)). The sample defaults to 5 (`--qps`).

**Does it work with Data Zone Batch?**
The code doesn't care about the deployment type. Point `--deployment` at a Data Zone Batch deployment. To price it, add an entry to [settings.toml](../shared/settings.toml) that names the Data Zone meters, for example `[pricing.models."gpt-5.4-mini-datazone".global_batch]` with the `5.4 mini Batch Inp Dz 1M Tokens` meters (the code reads the batch tier under that name), give it an output-token assumption under `[forecast.output_tokens_per_line]`, and pass `--price-model gpt-5.4-mini-datazone`. The Data Zone meters are listed about 10% higher. Not tested here.

**Can I use Flex processing instead of Batch?**
Yes: use [flex_job.py](../flex-web-search-and-web-iq/flex_job.py) with `--design bounded-web-search` (option 1's lines) or `--design two-stage` (option 2's), point `--deployment` at a Global Standard deployment of a Flex-capable model such as gpt-6-sol, and pass `--price-model gpt-6-sol`. Each line becomes a synchronous request with `service_tier: "flex"`; the lines and the results stay the same. Flex is preview with no SLA, shares the deployment's quota with Standard requests, and supported fewer models when we checked (not gpt-5.4). It wasn't measured on the 23-record sample; two 5-record live checks completed every line ([Flex with web search or Web IQ](../flex-web-search-and-web-iq/README.md)).

**Which models were tested?**
gpt-5.4-mini and gpt-5.4, on Global Batch, on 31 October 2026; gpt-6-sol on Flex, in 5-record checks on 1 November 2026. [settings.toml](../shared/settings.toml) also names the gpt-5.4-nano, gpt-5 and gpt-5-mini meters, so you can forecast with them; those models weren't tested with this sample.

**What does `insufficient_evidence` mean, and what should I do with it?**
The evidence didn't address the fact. That's the right answer for the sample's fictional records. For real records, send them to a second pass that lets the model search, or to a person.

**Can I see the evidence the model used?**
Only during the job. `--keep-working-files` keeps `evidence.jsonl` and `batch-input.jsonl` for debugging. Delete them afterwards: they hold Web IQ content. The kept results include each decision's citation URLs.

**Why isn't the evidence published in this repository?**
It's Web IQ content, which the terms don't allow us to store or redistribute. The published results hold only decisions, citation URLs, usage and costs.

**How do I report a problem?**
Open a GitHub issue. Don't include keys, endpoints or customer data. Report security vulnerabilities as described in [SECURITY.md](../SECURITY.md).
