| Run | Design | Plan | Model | $ per 1,000 records | Search share of cost | Web search requests / record | Web IQ calls / record | Input tokens / record (cached) | Output tokens / record | Facts right | Records right | Invalid status values | Missing or unparseable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| web-search-gpt54mini | web-search | - | gpt-5.4-mini | 43.12 | 93% | 2.87 | 0.0 | 8712.8 (2905.0) | 291.7 | 42/46 | 19/23 | 0 | 0 |
| bounded-web-search-gpt54mini | bounded-web-search | - | gpt-5.4-mini | 40.33 | 88% | 2.52 | 0.0 | 10230.0 (0.0) | 530.0 | 42/46 | 19/23 | 0 | 0 |
| two-stage-full-gpt54mini | two-stage | full | gpt-5.4-mini | 10.49 | 84% | 0.0 | 1.261 | 3731.0 (1213.2) | 300.7 | 44/46 | 21/23 | 0 | 0 |
| two-stage-lean-gpt54mini | two-stage | lean | gpt-5.4-mini | 9.78 | 81% | 0.0 | 1.13 | 3534.1 (233.7) | 277.5 | 44/46 | 21/23 | 0 | 0 |
| web-search-gpt54 | web-search | - | gpt-5.4 | 44.73 | 79% | 2.52 | 0.0 | 8750.5 (3383.7) | 303.7 | 44/46 | 21/23 | 0 | 0 |
| two-stage-lean-gpt54 | two-stage | lean | gpt-5.4 | 14.08 | 56% | 0.0 | 1.13 | 3534.1 (690.1) | 335.9 | 45/46 | 22/23 | 0 | 0 |

Wrong answers by run:

- **web-search-gpt54mini**: R05 owner_hq: contradicted (expected verified); R09 brand_owner: contradicted (expected verified); R10 brand_owner: verified (expected contradicted); R15 owner_hq: contradicted (expected verified)
- **bounded-web-search-gpt54mini**: R11 owner_hq: insufficient_evidence (expected verified); R13 owner_hq: insufficient_evidence (expected verified); R14 brand_owner: verified (expected contradicted); R15 brand_owner: verified (expected contradicted)
- **two-stage-full-gpt54mini**: R10 brand_owner: verified (expected contradicted); R22 owner_hq: contradicted (expected insufficient_evidence)
- **two-stage-lean-gpt54mini**: R10 brand_owner: verified (expected contradicted); R22 owner_hq: contradicted (expected insufficient_evidence)
- **web-search-gpt54**: R10 brand_owner: verified (expected contradicted); R14 brand_owner: verified (expected contradicted)
- **two-stage-lean-gpt54**: R10 brand_owner: verified (expected contradicted)
