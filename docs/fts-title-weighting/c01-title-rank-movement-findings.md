# pm-0918-c-01 — does a title landing after the write move FTS ranking?

**Verdict: the mechanism is SOUND. Whether it explains PersonaMem's 85.4% is
still open, and this corpus cannot close it.**

Three separate claims sit inside that row and they do not have the same answer:

| claim | verdict |
|---|---|
| Migration 034 puts `title` in the tsvector at content's weight | **true**, quoted below |
| A title-only `UPDATE` rebuilds that row's `search_vector` | **true**, demonstrated against Postgres |
| The rebuild is non-uniform across rows | **true**, 188 distinct ratios among 411 moved rows |
| Non-uniform ⇒ neighbours reorder | **true but not automatic** — see the counterexample |
| Enrichment is deferred, so titles land after the rows exist | **true**, 27,794 local rows did exactly that |
| Therefore the 8–10 Sep run scored a store that was still moving | **untested** — no local population reproduces that trajectory |

Everything below is read-only against the local development Postgres. No prod
access was used or attempted, no LLM call was made, nothing was seeded, and
nothing in the shared databases was modified.

## 1. The mechanism, in code

`034_memories_search_vector_title_weighting` replaces the trigger function
installed by migration 001. Both halves of the row's claim are in the same
`_set_trigger` call:

```python
def _vector(prefix: str = "") -> str:
    return f"to_tsvector('english', coalesce({prefix}title, '') || ' ' || coalesce({prefix}content, ''))"

def upgrade() -> None:
    _set_trigger(_vector("NEW."), "content, title")
```

which renders as

```sql
CREATE OR REPLACE FUNCTION memories_search_vector_update() RETURNS trigger AS $$
BEGIN
    NEW.search_vector := to_tsvector('english', coalesce(NEW.title, '') || ' ' || coalesce(NEW.content, ''));
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER memories_search_vector_trigger
BEFORE INSERT OR UPDATE OF content, title ON memories
FOR EACH ROW EXECUTE FUNCTION memories_search_vector_update();
```

Both are live in the local database (`pg_get_triggerdef` returns the
`UPDATE OF content, title` form verbatim; the schema is at alembic `041`).

*Same weight* is accurate and worth stating precisely: there is no `setweight`.
The two fields are concatenated before tokenisation, so every lexeme keeps
weight `D` and a **content-only** match scores exactly what it scored before 034.
The migration's own docstring says this is deliberate, so `FTS_RANK_SCALE = 6.0`
did not have to be re-derived. The consequence for this investigation is the
useful half: a row whose title shares no term with the query does not move at
all. Only rows whose title echoes the query move — and `ts_rank_cd`'s boost for
a repeated term is not a constant.

The title is **not** in the embedding — re-embeds hash and embed raw content —
so this is a keyword-path effect only. Downstream, `_saturate_rank` maps the
scaled rank through `1 - 1/(1 + x)`, which is strictly increasing, so it
preserves whatever order `ts_rank_cd` produced; it does not damp the reordering,
it only rescales it before the hybrid blend.

### Deferred enrichment really does write the title onto an existing row

`MemoryService._apply_enrichment` (core-api) builds a patch on a row that has
already been inserted:

```python
if enrichment.title:
    patch["title"] = enrichment.title
```

`content` is not in that patch. That is precisely the `UPDATE OF ... title`
case the migration widened the trigger for, and on `write_mode=fast` — the
default — it happens on a background worker seconds to minutes after the row
was written and became searchable.

## 2. The trigger fires, and not by a constant

Run `benchmark/c01_title_fts_rank_movement.py`; it opens with a probe that
borrows the real trigger function onto a temp table inside a rolled-back
transaction. Two rows, `content` never touched, `ts_rank_cd` against two queries:

| state | row 1, "terraform provisioning" | row 2, "telemetry timescaledb" |
|---|---:|---:|
| no title | 0.033333 | 0.025000 |
| title echoing the query | **0.091667** (×2.75) | **0.083333** (×3.33) |
| title not echoing it | 0.033333 (×1.00) | — |

Three different factors — 1.00, 2.75, 3.33 — from the same event. That is the
non-uniformity the row's argument needs, and it is real.

## 3. What it does to real rows

`benchmark/c01_title_fts_rank_movement.py` scores every row of a tenant twice,
building both vectors itself rather than reading the stored one:

```
before  to_tsvector('english', coalesce(content, ''))
after   to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
```

scored with the production expression, `ts_rank_cd(v, plainto_tsquery('english', q))`
(`postgres_service.py`, `_keyword_rank`). Queries are drawn from the corpus
itself, two ways: from row **content** (a user asks about what a memory says,
knowing nothing of titles) and from row **titles** (the friendliest case for the
hypothesis). Both are reported. Reading only the second would repeat the mistake
that sank pm-0918-c-04.

**Tenant `dev-9ff0ca` — 8,850 live rows, 100% titled, LLM titles that echo their
own content.** 40 two-term queries per set, candidate pools p50 = 70 / max 1,386:

| | content-derived | title-derived |
|---|---:|---:|
| rows matching before → after | 6,042 → 6,233 | 1,667 → 2,019 |
| newly eligible on title alone | 191 | 352 |
| rows matching both ways, rank **unchanged** | 5,631 (93.2%) | 1,399 (83.9%) |
| ratio p95 / max, over all matched rows | 1.42 / 32.5 | 2.06 / 73.5 |
| distinct ratios among the movers | 188 of 411 | 247 of 268 |
| **top-K agreement** (K = min(50, pool)) | **0.970** | **0.921** |
| top-10 agreement | 0.921 | 0.779 |
| **Kendall tau-b**, size-weighted | **0.943** | 0.883 |
| mean / max rank displacement, top-K rows | 1.94 / 32 | 5.64 / 46 |
| queries whose **top-1 result changed** | **9 of 33** | 12 of 24 |

Ordering statistics are over queries with a pool of at least 10; below that a
single swap reads as tau = −1 and means nothing. Overlap is reported against the
effective K, not against 50 — an earlier cut of this measurement reported
"16.3 / 50" on pools averaging 17 rows, which looks like catastrophic churn and
is in fact near-total agreement. No row ever lost a match: adding text to a
tsvector cannot remove one, and the script asserts it.

**Read that as: the effect is real, ordinary, and small per query.** ~93% of
scored rows do not move at all. Of a top-50, one to two members turn over. But
the head of the list is where a benchmark is scored, and the top-1 answer
changed on a sixth to a quarter of content-derived queries and about half of
title-derived ones.

### How much of that is the query sample?

A second, disjoint query set (`--seed c01-seedB`, same tenant and settings)
reproduces the ordering statistics and **not** the top-1 rate:

| | seed `c01` | seed `c01-seedB` |
|---|---:|---:|
| content-derived, top-K agreement | 0.970 | 0.954 |
| content-derived, top-10 agreement | 0.921 | 0.923 |
| content-derived, Kendall tau-b | 0.943 | 0.930 |
| content-derived, top-1 changed | 9 / 33 (27%) | 4 / 26 (15%) |
| title-derived, Kendall tau-b | 0.883 | 0.928 |
| title-derived, top-10 agreement | 0.779 | 0.790 |
| title-derived, top-1 changed | 12 / 24 (50%) | 10 / 21 (48%) |

The aggregate agreement measures are stable to ~0.02 and the tau ordering
between the two query families is not (0.883/0.928 straddles 0.930/0.943), so
treat "title-derived reorders more than content-derived" as unresolved at this
sample size. The top-1 rate on content-derived queries is a count over ~30
queries and moves accordingly — 15–27% is the honest range, not 27%.

## 4. Non-uniform does not automatically mean reordered

The clearest result in the run is a negative one. On tenant `default`, the
title-derived set moved **every** matched row — 592 of 592, ratios 2.17 to 3.67,
five distinct values — and reordered **nothing**: top-K agreement 1.000,
tau 1.000, top-1 unchanged on all 6 queries. The rows moved together, past
nobody.

So "the ratio varies" is necessary for a reorder and is not sufficient. The row
asserts the step from one to the other; the step holds on the fully-enriched
tenant and fails on this one. Any future argument of this shape has to show the
interleaving, not just the spread.

## 5. The effect scales with title coverage — which is the interesting part

| tenant | live rows | titled | content-derived tau (both seeds) | top-1 changed |
|---|---:|---:|---:|---:|
| `default` | 15,929 | 1,044 (6.6%) | **1.000** | 0 / 22 |
| `dev-9ff0ca` | 8,850 | 8,850 (100%) | 0.943 / 0.930 | 9 / 33, 4 / 26 |

At 6.6% coverage the content-derived set produced **zero** movement of any kind:
no newly-eligible rows, no rank change, no displacement. At 100% coverage it
produces measurable head churn.

That is a dose-response curve in title coverage, and it is exactly the shape the
hypothesis predicts. A store bulk-written on the deferred path starts at 0%
coverage and climbs to 100% as the worker drains, so its keyword ranking drifts
continuously across that window with no deploy, no config change and no code
change to blame. The row's framing — "the data changed underneath the benchmark"
— is mechanically available.

**It does not follow that it did.** Two points on a curve, from two tenants that
differ in more than coverage, is not the curve.

## 6. Titles do land after the row exists — 27,794 times here

`memories` has **no `updated_at` column** (`\d memories`: `created_at`,
`ts_valid_start/end`, `last_recalled_at`, `deleted_at`, `last_dedup_checked_at`
— nothing that records when a title was written). Neither does the enrichment
patch write an audit entry: only 82 `audit_log` rows record a title change.

The `create` audit does record the title **as it was at insert**:

```json
{"title": null, "memory_type": "fact", "content_length": 35, "write_latency_ms": 1213}
```

Joining that to the row's title today answers "did the title arrive later?" for
every row that has a create-audit entry:

| | rows |
|---|---:|
| memories paired with a create-audit entry | 59,559 |
| untitled at insert | 49,020 |
| **untitled at insert, titled now** | **27,794** |
| titled at insert (synchronous enrichment) | 10,539 |

46.7% of the paired corpus got its title after it was already a searchable row.
The deferred-enrichment premise is not in doubt.

## 7. What this corpus cannot answer, stated plainly

**It contains no PersonaMem data.** No tenant matches `%amb%` or `%persona%`;
the 3,458-chunk store the row is about is not here. Newest local row is
2026-09-08, so the corpus also predates most of the 8–18 Sep window.

**Neither measured population is the population in question.** The row's
scenario is *bulk-written, deferred, eventually fully enriched*. `dev-9ff0ca` is
fully titled but was titled **at insert** — only 64 of its 8,850 rows got a title
later — so it is the *destination* state without the transition. `default` has
genuinely deferred titles (all 1,044 of them) but stalled at 6.6% coverage, so it
is an *early* state that never arrived. Nothing here traverses the path.

**So the magnitudes above bound the mechanism; they do not estimate the
PersonaMem delta.** tau = 0.943 on one tenant's query mix is not a prediction
about a different corpus, a different question set, and `fts_weight = 0.6` rather
than the 0.3 default — a keyword-heavy blend gives this effect more of the score
than the runs measured here would.

**And the sign of the effect is unknown.** Every number here measures *how much*
ordering changes, not *whether the changed ordering is better*. Titles were added
in 034 because content-only FTS could not find rows whose distinguishing words
lived in the title; 191 and 352 newly-eligible rows in the two sets are that fix
working. A store that finished enriching could plausibly score **higher**, not
lower. Nothing in this investigation says which.

## 8. The row's decisive test is not runnable as written

The row asks prod for "the gap between each row's `created_at` and when its
title was written". **That column does not exist.** Not locally, and not on any
environment running this alembic chain: no migration in
`core-storage-api/.../versions/` ever adds `updated_at` to `memories` (grep
`updated_at` across all of them — every hit is another table). The test as
specified cannot be run even with the prod access it is blocked on.

The substitute, which needs only `audit_log` read on the amb tenant:

```sql
WITH c AS (
    SELECT resource_id, created_at AS insert_ts, detail->>'title' AS title_at_create
    FROM audit_log
    WHERE resource_type = 'memory' AND action = 'create' AND resource_id IS NOT NULL
      AND tenant_id = :amb_tenant
)
SELECT count(*)                                                        AS chunks,
       count(*) FILTER (WHERE c.title_at_create IS NULL)                AS untitled_at_insert,
       count(*) FILTER (WHERE c.title_at_create IS NULL AND m.title IS NOT NULL) AS titled_later,
       min(c.insert_ts), max(c.insert_ts)
FROM c JOIN memories m ON m.id = c.resource_id;
```

This gives the row's number (1) exactly and answers the *direction* of (2) —
which titles landed after their row existed — but still **not the timestamp** of
the title write, so it cannot say whether they landed before or after 10 Sep.
That question needs either worker logs from the window or a new column; the
database as it stands does not hold the answer, and no amount of prod access
changes that.

## 9. What would actually settle it

In order of cost:

1. **Run the query in §8 against the amb tenant.** If a large share of the 3,458
   chunks were untitled at insert, the store was still filling in when the record
   was set. Read-only, one query, no access this project has.
2. **Replay the benchmark's queries against the amb store twice** — once scoring
   `to_tsvector(content)` and once the live `search_vector` — with the script
   here pointed at that tenant. That converts "ranking moved" into "ranking moved
   *on these questions*", which is the only version that bears on 85.4%.
3. **Reproduce the transition locally**, which is the row's own no-prod-access
   fallback: bulk-write a corpus, score before the worker drains, score after.
   That needs LLM enrichment calls and a real seed, both currently held, so it
   was not done here.

Until (1) or (2), the honest statement is: **the mechanism exists, fires on the
default write path, and reorders the head of keyword results by a small but
non-zero amount that grows with title coverage. It is a live candidate for the
85.4% not being reproducible. It is not yet the explanation, and the search-path
bisect the row lists as its fallback should not be cancelled on the strength of
this document.**

## Reproducing

```
python benchmark/c01_title_fts_rank_movement.py \
    --dsn postgresql://memclaw:changeme@localhost:5432/memclaw \
    --tenant dev-9ff0ca --terms 2 --queries 40 --seed c01
```

Read-only apart from the probe's `ON COMMIT DROP` temp table, which is rolled
back. `--terms 3` narrows the generated queries and shrinks candidate pools below
the point where top-50 statistics mean anything; 2 is the setting used above.
Each report prints a digest of its query set so a re-run can be checked against
these numbers.
