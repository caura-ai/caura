# pm-0918-c-04 — does the atomic-fact fan-out fire on document-shaped writes?

**Answer: no, and the rate falls as content gets longer.** The hypothesis in the
tracker row — that 2,000-character document chunks are the shape the fan-out
fires on — is not supported by the corpus. A content-length gate, the row's
leading proposal, would be the wrong fix.

## Why this measurement exists

A70 shipped the fan-out on 2026-09-09 (#1424, #1428, #1430) partly on the
strength of a measurement that it almost never fires: seeding three STALE T2
haystacks produced zero fanned rows, because the enrichment prompt
(`common/enrichment/_prompts.py`, field 9) says `atomic_facts` is "OPTIONAL —
null in almost all cases" and "DO NOT fan out single-topic content".

**That measurement was taken on conversational content.** The proof gate that
would have covered the rest — reg-a75, the 100-session replay — was closed
without being run, because its harness lived in an unreachable private repo. So
three flags shipped with no evidence, and the first evidence was an external
benchmark regression.

This is that measurement, run on data rather than closed again.

## Method

Local `memclaw` database: **61,515 memories across 2,617 tenants**. <!-- legacy-name-floor: names the database these numbers came from; the measurement is not checkable without it --> Fan-out
children are identified by `metadata->>'source' = 'atomic_fact_fanout'` and
carry `metadata->>'parent_memory_id'`.

> The tracker row names `split_of` as the marker. That is stale — nothing in the
> current code writes it, so a query using it returns zero and reads as "never
> fires". Worth knowing, because that is the same shape as the original
> null result.

Parents are joined back from their children, bucketed by `length(content)`.

## Result

| content length | parents | fanned out | **rate** | children made | children per fanned parent |
|---|---:|---:|---:|---:|---:|
| <500 | 39,828 | 528 | **1.33%** | 1,014 | 1.92 |
| 500–999 | 2,051 | 70 | **3.41%** | 173 | 2.47 |
| 1,000–1,999 | 4,671 | 35 | 0.75% | 74 | 2.11 |
| **2,000–2,999** | 10,554 | 60 | **0.57%** | 207 | 3.45 |
| ≥3,000 | 2,925 | 10 | **0.34%** | 18 | 1.80 |

The band the row blames is the **second-lowest**, at less than half the rate of
short conversational content and a sixth of the 500–999 peak. The corpus is not
short of document-shaped content either — 13,479 parents sit at ≥2,000
characters, and 70 of them fanned out.

Overall: **703 of 60,029 parents fanned out (1.2%)**, producing **1,486 of
61,515 rows (2.4%)**.

## What this means

**1. There is no length threshold worth having.** Any ceiling low enough to
exclude document chunks also cuts the 500–999 band, where the fan-out fires
three times more often and is presumably doing the job it was built for.

**2. The 28% figure is about retrieval, not writing.** Children are 2.4% of
rows but were measured at ~28% of returned rows on the PersonaMem store. Those
two numbers are consistent only if children out-rank their parents — which is
exactly what they are built to do: short, self-contained, single-claim, one
embedding per claim. **The lever is therefore the read path, which is
pm-0918-c-03 (`include_derived`), not the write path.** This row should not be
closed by making writes rarer.

**3. One in-code justification rests on the rarity claim and survives.**
`fan_out_atomic_facts` deliberately bypasses the per-tenant storage bulkhead
(CAURA-602), justified in a comment by "the fan-out is rare enough". At 1.2% of
parents that holds. It is worth re-checking if the rate ever moves.

## What shipped instead

A per-tenant switch, `enrichment.atomic_fact_fanout_enabled`, default **ON**
(today's behaviour), read at the one chokepoint both the synchronous and worker
paths share.

A switch rather than a threshold because the measurement does not support a
threshold, and default-ON because turning it off for everyone would change what
every tenant's store contains in order to fix a problem measured on one store.
A tenant seeing fan-out children crowd its results turns them off for itself,
without a deploy and without inheriting a guessed number.

Switching it off is strictly cheaper — no child is embedded — so it reduces LLM
and embedding spend rather than adding any.

## Limits of this measurement

The fan-out decision is made by the enrichment LLM, so the rate depends on the
model and prompt in force. This corpus reflects the configuration that wrote it;
a store enriched by a different model could fan out at a different rate. What
the corpus does establish is that **length is not the discriminator**, which is
the specific claim the row rested on.

The PersonaMem `ambs` store itself was not available here. Its 28% remains the
only direct measurement of the regressed case, and it measures returned rows,
not writes.
