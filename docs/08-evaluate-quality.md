# 08 - Evaluate quality

Cost is easy to measure; accuracy isn't. Before you switch designs on real records, measure both on the same labeled set. This page shows how the sample does it and how to build your own set.

## What a labeled set looks like

One line per record, with the expected status of every fact and the public sources behind it ([samples/brand-owner/labels.jsonl](../samples/brand-owner/labels.jsonl)):

```json
{"id": "R14", "brand_owner": "contradicted", "owner_hq": "verified", "all_verified": false,
 "note": "Ben & Jerry's moved to The Magnum Ice Cream Company when Unilever's ice cream business was demerged (completed December 2025).",
 "sources": ["https://www.foodingredientsfirst.com/news/unilever-magnum-ice-cream-demerger-352039.html", "..."]}
```

The sample's 23 labels cover:

| Case | Records | Why it's in the set |
| --- | --- | --- |
| Straightforward, both facts true | 13 | The common case; any design should get these right |
| Claimed owner is a competitor | R05 (Minute Maid), R15 (Gillette) | The model must contradict, not just fail to confirm |
| Ownership changed recently | R04 (Tropicana, majority sold 2021-2022), R14 (Ben & Jerry's, demerged 2025) | Stale pages and model knowledge both point the wrong way |
| Ownership differs by market | R13 (Persil in Germany is Henkel's) | Region matters |
| Wrong headquarters claim | R09 (Nestlé in France, never true), R12 (Unilever in the Netherlands, out of date since 2020) | One fact true, the other false |
| A brand name that contains a former owner | R10 (Nestlé Pure Life in the US) | Tests the lean plan's skip rule |
| Entities that don't exist | R22, R23 (fictional) | The right answer is `insufficient_evidence`, not a guess |

## Run the evaluation

```bash
python evaluation/evaluate.py runs/web-search-mini runs/lean-mini runs/full-mini \
    --labels samples/brand-owner/labels.jsonl --out runs/evaluation.md
```

For each run, `evaluate` reports ([evaluate.py](../evaluation/evaluate.py)):

| Measure | Meaning |
| --- | --- |
| Facts right | Statuses that match the label, out of records × facts |
| Records right | Records with every fact right |
| Invalid status values | Statuses outside `verified`, `contradicted`, `insufficient_evidence`. Possible only without a strict schema |
| Missing or unparseable | Records with no usable answer |
| Wrong answers by run | Each miss, with the status returned and the status expected |

It also prints cost per 1,000 records, search share and tokens from each run's `run.json`, so cost and quality sit side by side. The sample's table is in [results/2026-10-31/evaluation.md](../results/2026-10-31/evaluation.md).

## Read the misses, not just the score

The score tells you whether to look; the misses tell you what to fix. On the sample ([09](09-results-on-the-sample.md#where-each-design-went-wrong)):

| Miss | Cause | Fix to try |
| --- | --- | --- |
| R10, lean plan: Pure Life "verified" | The lean rule skipped the brand search and told the model to assume ownership | Drop the rule, or keep it only where the data owners accept the risk |
| R10, full plan: Pure Life "verified" | Evidence mixed the primary source with secondary pages describing a licence | A template that targets the market (`<brand> <market> owner`), or a rule to prefer primary sources |
| R22: fictional owner "contradicted" | The model treated "Contoso" pages as evidence about "Contoso Beverages Ltd". R23, with the same headquarters evidence, was answered correctly, and gpt-5.4 got both right | A name rule: a different full name is a different company |
| Web search, unbounded: one fact's status leaking into the other | No written per-fact rules | Per-fact rules and a strict schema, as in the two-stage prompt |

Make one change at a time and re-run the whole labeled set. Fixing R22 by tuning to R22 alone is how prompts get overfit.

## Build your own labeled set

- **Size.** A few hundred records gives a usable read. 23 records show mechanics, not accuracy: one record is about 4 points of record accuracy.
- **Coverage.** Over-sample the hard cases above, in the proportions you expect in real data, plus a few you hope never happen.
- **Sources.** Store the URL behind every label. Labels go stale as facts change; re-check them before each comparison.
- **Independence.** Don't label from the same search results the pipeline sees. Use primary sources (company filings, press releases) where you can. R10's label rests on the seller's own press release, because secondary pages disagree.
- **Version the labels with the results**, so a change in score can be traced to the data, the prompt or the model.

## Keep checking after launch

- **Spot-check decisions every run**, especially `contradicted` ones, which usually drive action.
- **Track the `insufficient_evidence` rate.** A jump usually means a search template stopped working or the source pages changed.
- **Re-run the labeled set** when you change the model, prompt, templates, evidence size or plan.
