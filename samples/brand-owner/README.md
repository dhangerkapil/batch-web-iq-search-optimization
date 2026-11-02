# Sample: brand ownership

Twenty-three records, each checking two public facts ([records.jsonl](records.jsonl)):

- **brand_owner**: does `claimed_owner` own `brand` in `market`?
- **owner_hq**: is `claimed_owner` headquartered in `claimed_owner_hq_country`?

| Field | Meaning |
| --- | --- |
| `id` | Unique record id; becomes the batch `custom_id` |
| `brand` | Brand name as sold |
| `market` | ISO 3166-1 alpha-2 country code where the brand is sold; used as the Web IQ `region` for the brand search |
| `claimed_owner` | The company the record says owns the brand |
| `claimed_owner_hq_country` | ISO country code the record says the owner is headquartered in |

[labels.jsonl](labels.jsonl) holds the expected status of each fact (`verified`, `contradicted` or `insufficient_evidence`), a note explaining it, and the public sources behind it, checked on 31 October 2026. [08 - Evaluate quality](../../docs/08-evaluate-quality.md#what-a-labeled-set-looks-like) explains which hard cases the set covers and why.

Facts change. Recheck the sources before using these labels for a new comparison; the notes say what each label rests on.

The records name real brands and companies only to check publicly reported facts about them. "Glimmerbrook" and "Contoso Beverages Ltd" are fictional: the right answer for R22 and R23 is `insufficient_evidence`.
