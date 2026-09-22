# pm-0918-c-04 — does the atomic-fact fan-out fire on document-shaped writes?

**This corpus cannot answer that question, and an earlier version of this
document claimed it could.** The correction is the finding. Read the "What went
wrong the first time" section before using any number here.

## The question

A70 shipped the fan-out on 2026-09-09 (#1424, #1428, #1430) partly on a
measurement that it almost never fires: seeding three STALE T2 haystacks
produced zero fanned rows, because the enrichment prompt
(`common/enrichment/_prompts.py`, field 9) says `atomic_facts` is "OPTIONAL —
null in almost all cases" and "DO NOT fan out single-topic content".

That measurement was taken on conversational content. The row asks whether
2,000-character document chunks — multi-topic by construction — are the shape
it does fire on, which nobody tested. reg-a75, the proof gate that would have
covered it, was closed **without being run** because its harness lived in an
unreachable private repo.

## What the corpus actually contains

Local development database, 61,515 memories, 2,617 tenants, newest row
**2026-09-08**.

| slice | parents | fanned out | parents ≥2,000 chars | fanned at ≥2,000 |
|---|---:|---:|---:|---:|
| **Non-benchmark (real content)** | 20,756 | **0** | 83 | **0** |
| Benchmark (LongMemEval) | 39,026 | 703 | 13,396 | 70 |

Every fan-out child in this database — all 1,486 of them, from 703 parents —
came from **benchmark conversation data written in `write_mode=fast`**. Split by
mode and provenance, no other combination produced a single child:

| write_mode | benchmark? | parents | fanned | rate |
|---|---|---:|---:|---:|
| fast | yes | 28,581 | 703 | 2.46% |
| (unset) | no | 18,816 | 0 | 0% |
| strong | yes | 10,445 | 0 | 0% |
| fast | no | 1,684 | 0 | 0% |
| strong | no | 256 | 0 | 0% |

## Three reasons this cannot settle the row

**1. The real-content slice has no fan-out at all, and almost no long content.**
20,756 non-benchmark parents produced zero children, and only 83 of them reach
2,000 characters. There is no document-shaped population here to measure.

**2. The corpus predates the mechanism the row is about.** The newest row is
2026-09-08; A70 shipped 2026-09-09. Before A70 the *synchronous* path fanned out
and the worker path did not — it computed `atomic_facts` and discarded them. So
a deferred write whose enricher *did* decide to fan out appears in this data as
"did not fan out". `write_mode=fast` defers, and fast is the default.

**3. That bias is not random — it tracks the variable under test.** If longer
content is written more often in bulk (deferred, facts discarded pre-A70), a
falling rate-with-length appears in the data whether or not the enricher
behaves that way. The measurement cannot separate "the enricher fans out less
on long content" from "long content was deferred and its facts thrown away".

## Two code facts that bear on the row directly

Both verified against `main`, and both narrow the question more than the corpus
does.

**`create_memories_bulk` never calls the fan-out.** There are exactly two call
sites for `fan_out_atomic_facts` in the tree — `memory_service.py` (the
synchronous enrichment path) and `consumer.py` (the worker path). The bulk
create path is not one of them. So on a deployment running inline enrichment,
**ingest and document writes have a 0% fan-out rate by construction**, whatever
the enricher decides. Any measurement of "does it fire on document writes" has
to establish which of those two paths the writes in question actually took.

**`CHUNKING_THRESHOLD_CHARS = 2000`** (`core-api/src/core_api/constants.py`).
Content above 2,000 characters triggers auto-chunking, so an ordinary single
write never reaches the fan-out at that length — it arrives as chunks. The
"≥2,000" band in the withdrawn table could therefore only ever have been bulk
and ingest rows, never long single writes. That is a second, independent reason
the band did not mean what it appeared to.

## What went wrong the first time

The first version of this document reported a rate-by-length table over the
whole corpus and concluded the row's premise was disproven — the fan-out rate
*falls* with length, so no length gate is worth having.

That table was real arithmetic over the wrong population. Its ≥2,000-character
band was **99.4% synthetic benchmark conversation** (13,396 of 13,479 rows). It
was measuring the length distribution of LongMemEval, not a property of the
fan-out. It also contained a plainly false sentence — that any ceiling low
enough to exclude document chunks must also cut the 500–999 band, when a
2,000-character ceiling preserves that band entirely.

The irony is the point, and it is why this section exists rather than a quiet
edit: **this row exists because A70 shipped on a measurement taken on an
unrepresentative population, and the first attempt to correct it did the same
thing.** The lesson is not "measure more" but "state the population, and check
whether the thing you are measuring could have happened in it".

## Where that leaves the question

**Untested, not disproven.** Whether the fan-out fires on 2,000-character
document chunks is open. Answering it needs a corpus written after 2026-09-09
through the path that actually serves document writes, with `write_mode` and
provenance recorded — or a direct experiment: enrich a sample of real document
chunks and count how often the enricher returns `atomic_facts`, which measures
the decision rather than inferring it from surviving children.

Also worth knowing: children can be deduplicated on creation, so a parent whose
facts all collided with live rows leaves no linked children and reads as "did
not fan out" here. Any future measurement should count the enricher's decision,
not the rows that survived it.

## What shipped anyway, and why it does not depend on the above

A per-tenant switch, `enrichment.atomic_fact_fanout_enabled`, default **ON**,
read at the one chokepoint the synchronous and worker paths share.

It is justified without the measurement: a tenant whose results are crowded by
fan-out children can turn them off for its own store, immediately, without a
deploy and without imposing a threshold on anyone else. That is exactly what
PersonaMem needs and what nothing in the codebase offered. Default ON because
that is today's behaviour, and changing every tenant's store to address one
store's regression would be the wrong default whichever way this measurement
eventually lands.

A threshold was **not** shipped, and the reason is now "there is no evidence for
where to put one", not "the evidence says no threshold helps".

## The 28% figure

The CEO measured ~28% of returned rows on the PersonaMem store as fan-out
children. That is a **retrieval** proportion; the numbers here are **write**
proportions on a different corpus, and the two cannot be compared — PersonaMem's
own child-to-row ratio was never measured. A plausible reading is that short,
self-contained, single-claim children out-rank their parents, which would make
pm-0918-c-03 (`include_derived`) the lever. That remains a hypothesis this
document does not test.

## Re-running

`benchmark/pm_c04_fanout_rate.py` breaks the result out by `write_mode` and by
benchmark provenance, because a single aggregate number over this corpus is
misleading — that is how the first version went wrong.
