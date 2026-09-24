# pm-0918-c-01 — the search-path bisect

**Status:** complete (read-only, code + git only — no prod access used)
**Scope:** the branch the row deferred: *"if the enrichment hypothesis fails, bisect
`84c4b62d`, `c13b7203`, `f236f34a`."*
**Companion:** `docs/fts-title-weighting/c01-title-rank-movement-findings.md` (#1704),
which measured the title-into-FTS mechanism and left the sign unmeasured.

---

## Summary

Two findings, and the second is the one that matters.

**1. All three named commits are per-tenant knobs whose default is 0, and at 0 each
one is inert on the ranking path.** I verified this in the diffs, not from the PR
prose. None of them can move a score for a tenant that did not opt in. One of the
three (`f236f34a`) has an environment-variable override, which is the only way any
of them reaches a default tenant, and that is a one-line thing to check.

**2. The window is wrong.** The bisect was framed around the 10 Sep → 14 Sep deploy
gap. In that gap, `core-api`'s entire search-step surface received **zero** commits
(the last is `84c4b62d` on 10 Sep 17:27; the next is 15 Sep), and the two storage-side
commits are the default-0 knobs above. A bisect over that window is predicted to come
back empty, and that prediction is itself worth something: it is evidence the code did
not change under the benchmark in the window everyone has been staring at.

**The default-on changes that actually alter what `/search` returns all landed on
9 Sep — inside the record run, not after it.** Three of them, none in the row's list:

| Commit | Landed | Default-on? | What it changes |
|---|---|---|---|
| `1171c130` → `0a658863` → `2a88514d` (A70) | 9 Sep 14:20–15:21 | **yes** | the deferred write path starts creating fan-out **child rows**; before this it discarded the facts |
| `b17771f5` (D16) | 9 Sep 17:53 | **yes** | successor injection budgeted — a response that could carry *all* successors now carries at most one per stale row |
| `1d86102e` | 9 Sep 09:49 | **yes** | `MAX_SEARCH_TOP_K` 20 → 200 |

The third of those is not a suspect; it is a **clock**. See below — it dates the
record run.

**On the framing the row insisted on:** neither of the two leading candidates is a
regression on its face. A70 adds atomic facts the deferred path was silently dropping
(a bug fix). D16 removes result bloat that A34 never intended (a bug fix). Both change
what the benchmark sees, and both could move the number in **either** direction. The
question to ask about each is "did the store/response change", not "did it break".

---

## Part 1 — the three named commits, one at a time

### `84c4b62d` — ANN-pool shadow mode (10 Sep 17:27)

**(a) What it changed in the retrieval path.** Three things, and only the third is
unconditional:

- A new core-api knob `ann_pool_shadow`. When it is `1` **and** `ann_pool_size > 0`
  and the run is not diagnostic, core-api serves the legacy full-scan result and
  re-runs the identical payload with the pool in a background task, logging an
  overlap line. The caller's result is the legacy one — the primary wire payload is
  built with `ann_pool_size` forced to 0
  (`execute_scored_search.py`, the `use_shadow` block).
- D12 arm provenance: inside `if use_ann_pool:`, the pool arms are tagged and the
  `UNION` becomes `UNION ALL + GROUP BY string_agg(DISTINCT arm)`.
- **Unconditional:** the outer select gains one column,
  `null().label("pool_arms")` on the default path, plumbed through
  `post_filter_results.py` and `execute_scored_search.py`. A typed NULL. It is not
  read by any scoring expression.

There is also a drive-by: the function-local `from sqlalchemy import Date, cast,
literal` inside the `date_range_boost` block was lifted to module scope (it shadowed
those names across the whole of `search_memories`, which is why the `_dr_cast` /
`_DrDate` aliases exist). The names now resolve to the same `sqlalchemy` objects.
No semantic change.

**(b) Could it move ranking for a store like PersonaMem's?** Only with
`ann_pool_size > 0` on that tenant, which is a separate opt-in. With it set, yes,
substantially — a bounded candidate pool is exactly a ranking change. With it unset,
no: every pool expression is behind `if use_ann_pool:`.

**(c) Flag / default.** `ANN_POOL_SHADOW = 0` and `ANN_POOL_SIZE = 0` in
`core-api/src/core_api/constants.py`. Neither is environment-overridable. Both are
tenant-level (`search.default_profile`), not agent-tunable.

**(d) Dates.** Landed 10 Sep 17:27, after that day's release (`816be36e`, 15:28), so
it first shipped in `8a41ca27` (11 Sep 02:09). It is inside the 10→14 Sep window. The
date does **not** exonerate it; the default does.

**Verdict: exonerated on mechanism, not on date.** The only residual risk is the
tenant having `ann_pool_size > 0`, which is checkable.

---

### `c13b7203` — `freshness_reference` / As-Of Recall (11 Sep 02:02)

**(a) What it changed.** A tenant knob `freshness_reference`. At `1` **and** with a
request-level `valid_at`, two expressions retarget:

- the freshness anchor goes from `greatest(created_at, ts_valid_start)` to
  `coalesce(ts_valid_start, created_at)`, and the clock from `now()` to `valid_at`;
- `temporal_boost`'s window compares the same anchor to `valid_at - temporal_window`
  instead of `created_at` to `now() - temporal_window`;
- `ts_valid_end < ref_ts` replaces `ts_valid_end < now()` in the freshness floor case.

At the default the code takes the `else` branches and reconstructs the *same*
expression tree (`ref_ts = func.now()`, `anchor = greatest(...)`).

**One unconditional change,** which is why this commit needed a closer look than the
other two: `valid_at` is now normalised before use —

```python
if valid_at is not None and valid_at.tzinfo is None:
    valid_at = valid_at.replace(tzinfo=UTC)
valid_at_ts = literal(valid_at, type_=DateTime(timezone=True)) if valid_at is not None else None
```

and the `currency_factor` comparison switched from `ts_valid_end < valid_at` to
`ts_valid_end < valid_at_ts`. That fires for **any** request carrying `valid_at`,
knob or no knob, and `caura_recall` does expose a `valid_at` parameter.

I could not find a behavioural delta in it. `column < python_datetime` already bound
through the column's `DateTime(timezone=True)`, and asyncpg already treats a naive
datetime on a `timestamptz` bind as UTC — so the explicit type and the `replace()`
restate what the driver was doing. **I did not prove this against the real driver**,
and I am flagging it rather than asserting it: it is the one unconditional line in
the three commits that touches a scoring input.

**(b) Mechanism if it did fire.** A backfilled corpus — which the PersonaMem store
is, bulk-written in one sitting — is precisely the shape this knob was built for, and
at `1` it would move freshness for every row. That is a large ranking change. But it
requires the tenant flip.

**(c) Flag / default.** `FRESHNESS_REFERENCE = 0`, hard-coded, not
environment-overridable, tenant-level.

**(d) Dates.** 11 Sep 02:02, released `8a41ca27` 02:09. Inside the window.

**Verdict: exonerated at default.** Residual: confirm the tenant has no
`freshness_reference` in its profile, and confirm whether the harness sends
`valid_at`.

---

### `f236f34a` — A41 boost-gate (14 Sep 09:44)

**(a) What it changed.** `recall_boost_source` selects which counter feeds
`recall_boost`: `0` = `recall_count` (bumped on every return by `TrackRecalls`),
`1` = `metadata._system.recall_used_count` (bumped only by an evolve outcome report).
The boost's cap, window, scale and saturation curve are identical under both. At `0`
the extra columns are not even projected — the `recall_used_count` / `recall_used_at`
columns are appended to `ingredient_cols` only under `if _recall_boost_source == 1:`,
explicitly so the default statement stays byte-identical, and the compiled-text
ratchets in `test_fts_score_single_render` / `test_ann_pool_statement` pin that.

The commit also adds an `evolve_apply_weights(mark_used=True)` UPDATE that writes
`metadata._system.recall_used_count`. That runs on the evolve path, writes only
`metadata`, and cannot disturb search: the FTS trigger is
`BEFORE INSERT OR UPDATE OF content, title`, so a metadata-only UPDATE does not
rebuild `search_vector`.

**(b) Mechanism if it did fire.** At `1`, every row that no agent has confirmed via
evolve gets `recall_boost = 1.0` exactly. On a benchmark store, where nothing reports
outcomes, that means **the recall boost switches off entirely** — a real, global
ranking change. Worth knowing, because it is the one of the three whose "on" state
has a large and predictable effect on a benchmark tenant specifically.

**(c) Flag / default — and the exception.** `search.default_profile`, tenant-level,
default 0. But unlike the other two it reads the environment:

```python
RECALL_BOOST_SOURCE = 1 if read_int_env("CAURA_RECALL_BOOST_SOURCE", 0, minimum=0) == 1 else 0
```

`CAURA_RECALL_BOOST_SOURCE=1` in the benchmark environment's core-api would turn this
on for **every** tenant with no profile change. This is the only environment-level
route any of the three commits has, and it is a one-line check.

**(d) Dates — and this one the date does help with.** It landed 09:44 on 14 Sep;
the next release cut was `31b57172` at 10:40 the same morning. **If the 14 Sep re-run
started before ~10:40 local, this commit was not in the deployed build at all** and is
ruled out on date alone for that run. It cannot be ruled out for the 18 Sep run. I do
not have the run's clock time, so I am stating the condition rather than the
conclusion.

**Verdict: exonerated at default; check the env var; likely ruled out by date for the
14 Sep run specifically.**

---

## Part 2 — going wider

Every commit touching the retrieval path between the record run and the 14 Sep re-run,
from `git log` over `pipeline/steps/search/`, `services/recall_service.py`,
`services/memory_service.py`, `core-storage-api/.../postgres_service.py`,
`common/constants.py` and `core-api/.../constants.py`.

### The ones that are default-on and change what comes back

#### A70 — atomic-fact fan-out reaches the deferred path (9 Sep 14:20–15:21) — **top candidate**

Three commits, released together in `c4a31b73` (9 Sep 15:56):

- `1171c130` **step 1**: the async enrichment worker had
  `_ENRICHMENT_UNROUTED_FIELDS = frozenset({"atomic_facts"})` — it was not merely
  skipping the fan-out, it was **discarding the LLM's facts**. This routes them into
  the row's metadata.
- `0a658863` **step 2a**: lifts `fan_out_atomic_facts` out of the synchronous write
  path so something else can call it.
- `2a88514d` **step 2b**: `handle_memory_enriched` in `core-api/consumer.py` reads the
  persisted facts and calls that function.

What a child is (`memory_service.py::fan_out_atomic_facts`): a **full `memories` row**
— `status: "active"`, its own content, its own embedding, `weight` and `visibility`
and `ts_valid_start` inherited from the parent, `metadata.source =
"atomic_fact_fanout"`, `metadata.parent_memory_id = <parent>`, **and its own
`created_at`**. It is an ordinary searchable row competing for top-K slots like any
other.

Why this is the top candidate:

1. **It is default-on** (the per-tenant switch `atomic_fact_fanout_enabled` did not
   exist until `4ab590f9`, 22 Sep, and defaults to `True`).
2. **It changes the store, not just the code** — which is the only class of cause that
   survives the observation the row already has, namely that the 14 and 18 Sep runs
   agree with each other at 96% overlap while both disagree with the record.
3. **The store's write mode is exactly the one that was broken.** The `amb` store was
   bulk-written on 8 Sep; bulk writes take the deferred path; before 9 Sep that path
   produced **zero** children. Whatever fraction of the 3,458 chunks were still
   draining through enrichment when this deployed got children; the ones enriched
   before it did not.
4. **It is measured to fire on this corpus.** The 28% fan-out rate in the 18 Sep
   correspondence is a measurement on this content shape. 2,000-character document
   chunks are what the enrichment prompt splits; the original A75 proof gate was
   closed without testing that shape.
5. **It directly explains the retrieval-overlap number.** ~28% of 3,458 parents
   spawning children is on the order of a thousand new, short, specific rows inserted
   into a top-50 competition. 71% context overlap is the shape of a candidate set that
   gained a large cohort of new rows, not of a scoring formula that shifted.

**The precondition this rests on, stated explicitly.** Which enrichment path a write
takes is decided by `settings.inline_enrichment`, i.e. `deployment_mode == "inline"`
— **not** by `write_mode`. In inline mode (the OSS default, a single-process stack)
enrichment runs in-process via `_enrich_memory_background`, which has called
`fan_out_atomic_facts` all along; A70 changed nothing there. In deferred mode (what
`core-api/tasks.py` calls "the SaaS shape") the write publishes an enrich request and
the worker handles it — and that is the path that discarded the facts until 9 Sep.

So this finding applies **only if the deployment serving the `amb` store runs in
deferred mode.** It almost certainly does — it has a `core-worker` — but it is a
precondition, not an assumption, and it is checkable in one line
(`DEPLOYMENT_MODE` / `deployment_mode` in that environment's core-api config).

This also resolves what looks at first like a contradiction in
`docs/atomic-fact-fanout/pm-c03-include-derived-blast-radius.md`, which reports 1,486
fan-out children on the local `memclaw` database with `created_at` of **2026-09-06**,
three days before A70 — from parents marked `write_mode=fast`. That is not a
counter-example: a local stack runs inline, `write_mode` does not select the
enrichment path, and the inline path always fanned out. The two observations are
consistent.

Direction is genuinely open. Children are shorter and more atomic — which is either
better recall (a precise fact now has its own row and its own embedding) or worse
(the answer's context fills with fragments instead of the chunk that held the answer).
The 18 Sep note's `include_derived` proposal — "a caller asking for 50 results means
50 memories, not 36 plus 14 fragments" — is the pessimistic reading of the same fact.

**And it hands back the test the row wrote off as unrunnable.** #1704 stopped because
`memories` has no `updated_at`, so "when did the title land" cannot be asked. But the
fan-out child is created by `handle_memory_enriched`, which fires on the event the
worker publishes *immediately after* the PATCH that writes the title
(`core-worker/consumer.py`: `update_memory_enrichment(...)` → `publish_memory_enriched(...)`).
So for every parent that produced at least one fact, **the child's `created_at` is a
timestamp for the parent's enrichment, accurate to one Pub/Sub hop plus one embedding
batch call.** It is a stored column on a row that exists today, on ~28% of the corpus.
See the test in Part 4.

#### D16 — successor injection budgeted (`b17771f5`, 9 Sep 17:53) — **second candidate**

Before this commit, `LoadAndSerialize` appended **every** row returned by
`find_successors` — which is a bare `supersedes_id IN (...)` with no per-predecessor
cap. The commit's own note: *"observed live: `top_k=5` answered with 53 items."*
After it, at most one successor per stale row, the newest, so a response holds at most
`2 * top_k` items. Injected rows are now labelled `injected: true`, and the set is
deliberately **not** re-trimmed to `top_k`.

Default-on, no knob. It changes the returned set for any query whose results contain a
superseded row — and PersonaMem is a persona-consistency benchmark, so evolving
preferences and therefore supersession are the substance of the corpus, not an edge
case.

Direction, again, cuts both ways, and here the pessimistic reading is the likelier
one: for a generative QA benchmark the answering model reads the returned rows, and a
response that used to carry up to an order of magnitude more rows — including the
correct successors — plausibly answered more questions. If so, part of the 85.4% was
bought by a bloat that A34 never intended and D16 correctly removed. That is the same
"the record was never reproducible" conclusion as the title hypothesis, arriving by a
completely different route, and unlike the title hypothesis it is **replayable
locally** without seeding anything new.

#### `1d86102e` — `MAX_SEARCH_TOP_K` 20 → 200 (9 Sep 09:49) — **not a suspect, a clock**

Before this commit, `top_k` above 20 was unreachable on every public surface:
`core_api/schemas.py` had `le=MAX_SEARCH_TOP_K` (a 422 on REST `/search`) and
`mcp_server.py` had `capped_top_k = min(top_k, MAX_SEARCH_TOP_K)` with a warning on
`caura_recall`. Verified at `1d86102e^`.

The commit's own rationale names this benchmark: *"20 was the only slot count on the
AMB leaderboard set by the server rather than the caller … it forced the harness into
8k-character chunks to reach comparable context."*

The record run is `caura-bulk-2k-top50`. **A run at `top_k=50` could not have executed
against any build older than this commit** (released `a2039672` 10:13 / `2409a7d2`
10:31 on 9 Sep). So the record run's queries began on or after the morning of 9 Sep —
which places A70 (14:20–15:21) and D16 (17:53) **inside** the record run, not before
it. If the record run's queries are timestamped, splitting them at 15:56 and at 17:53
is a free A/B that was already run without anyone meaning to.

This dating rests on the run name meaning `top_k=50`. If it means something else, the
constraint drops and the two candidates above simply land before the run instead of
during it — still the right window, less leverage.

### The ones that are ruled out

| Commit | Landed | Why ruled out |
|---|---|---|
| `79d653c3` — stop shipping embedding + tsvector per scored-search row | 14 Sep 09:21 | Serialization only: `orm_to_dict(r.Memory, MEMORY_LIST_FIELDS)` instead of `MEMORY_FIELDS`. I checked every consumer — `rerank_results.py` reads `r.similarity` and `vec_sim` (separate projected columns), and the only `data["embedding"]` on the search path is the **query** embedding set by `parallel_embed_entity_boost.py`. Nothing read the row vector. Ranking-neutral. |
| `fef5b3de` — HNSW PR1, fenced ingredients CTE | 9 Sep 17:38 | A 494-line rewrite of the scoring statement, but a projection refactor: cosine rendered 6×→1×, `ts_rank_cd` 10×→1×. Same values, same select list, outer `ORDER BY score DESC, created_at DESC` unchanged so ties stay deterministic. **Caveat worth stating:** "byte-compatible" is pinned by compiled-text and `EXPLAIN` tests, not by an output-equivalence test over real rows. Low risk, not zero. |
| `382c0607` — HNSW PR2, ANN candidate pool | 10 Sep 12:02 | `ANN_POOL_SIZE = 0`; every pool expression behind `if use_ann_pool:`. Same residual as `84c4b62d`. |
| `5783f8f3` — strict fleet scoping (C27) | 9 Sep 08:22 | Opt-in per the commit title; check the tenant profile if paranoid. |
| `629daba9`, `0bf240bf`, `769d9def` — ENTITY_LOOKUP short-circuit and graph-expansion fallbacks | 9 Sep | These alter the ENTITY_LOOKUP and graph paths. The 18 Sep diagnostics put **569 of 589** queries on `retrieval_strategy=keyword_search`. Whatever they do, they do it to ≤3% of this workload. |
| `339f3beb` — document top_k cap | 9 Sep 10:21 | Pure refactor; `MAX_DOC_SEARCH_TOP_K = 50` was already a bare literal 50. No value changed. |
| `5ac51116` — bulkhead on entity-lookup fall-through | 9 Sep 19:07 | Concurrency, not ranking. |
| `9936f7a7` — CAURA-722 diagnostic columns | 8 Sep 13:38 | Diagnostic reporting. |
| `_adaptive_fts_weight`, `FTS_WEIGHT_BOOSTED` | — | `git log -S` shows both unchanged since the initial public release (26 Apr). The observed `fts_weight=0.6` is `FTS_WEIGHT_BOOSTED`, i.e. the adaptive router's own value for short specific queries — so it does **not** by itself prove the tenant carries a custom profile. |
| `core-api/src/core_api/search_trim.py` | — | No commits in the window at all. Its current `resolve_include_derived` came in with `1c98ce18` on 23 Sep, long after. |

---

## Part 3 — what the dates rule out, stated plainly

1. **The 10 Sep → 14 Sep window contains no default-on ranking change.** `core-api`'s
   search-step directory received nothing between `84c4b62d` (10 Sep 17:27) and 15 Sep.
   The two storage-side commits in the window are default-0 knobs. If the benchmark
   changed between those two deploys and the tenant's profile did not change, the cause
   is not in this repository's search path.
2. **`f236f34a` is ruled out for the 14 Sep run if that run started before ~10:40**,
   because the release that could carry it was cut at 10:40 that morning. It is not
   ruled out for the 18 Sep run.
3. **`84c4b62d` and `c13b7203` are not ruled out by date** — both were deployable
   before 14 Sep. They are ruled out by their defaults instead, which is the stronger
   argument of the two.
4. **The record run cannot predate 9 Sep ~10:30**, because `top_k=50` was a 422 before
   then. This is the constraint that moves A70 and D16 from "before the record" to
   "during the record".

---

## Part 4 — the cheapest test

One candidate stands out, and its test is a read-only query that needs no new writes,
no LLM calls and no seeding.

**Fan-out children carry `created_at`, `metadata.source` and `metadata.parent_memory_id`.**

```sql
-- 1. When did the children appear? One row per hour.
SELECT date_trunc('hour', created_at) AS hour, count(*)
  FROM memories
 WHERE tenant_id = :amb_tenant
   AND metadata->>'source' = 'atomic_fact_fanout'
 GROUP BY 1
 ORDER BY 1;

-- 2. How many are there, against the parent corpus?
SELECT count(*) FILTER (WHERE metadata->>'source' = 'atomic_fact_fanout') AS children,
       count(*) FILTER (WHERE metadata->>'source' IS DISTINCT FROM 'atomic_fact_fanout') AS parents
  FROM memories
 WHERE tenant_id = :amb_tenant;

-- 3. The enrichment-timing question #1704 could not ask, answered on the ~28%
--    of parents that produced a fact. child.created_at is the parent's enrichment
--    time to within one Pub/Sub hop and one embedding batch.
SELECT date_trunc('hour', c.created_at) AS parent_enriched_hour, count(DISTINCT p.id)
  FROM memories c
  JOIN memories p ON p.id = (c.metadata->>'parent_memory_id')::uuid
 WHERE c.tenant_id = :amb_tenant
   AND c.metadata->>'source' = 'atomic_fact_fanout'
 GROUP BY 1
 ORDER BY 1;
```

**How to read it.** The A70 consumer was released at 15:56 on 9 Sep. Children created
after that timestamp are rows the record run did not have (or had for only part of its
queries). If query 1 shows a mass of children landing on 9–10 Sep or later, the store
gained a large cohort of competing rows between the record and the re-runs — and that
is a data change, with the code exonerated, arriving at the same conclusion as the
title hypothesis but through a column that actually exists.

If query 1 shows the children all predate the record run, A70 is dead and D16 becomes
the primary candidate.

**Three one-line checks to run alongside it,** each of which can retire a suspect:

- `CAURA_RECALL_BOOST_SOURCE` in the benchmark environment's core-api — if `1`,
  `f236f34a` is live for every tenant and the recall boost is effectively off on a
  store nobody reports outcomes against.
- The `amb` tenant's `search.default_profile` — specifically `ann_pool_size`,
  `freshness_reference`, `recall_boost_source`, `score_formula`, `candidate_pool_size`.
  All three named commits are inert unless one of these is set.
- Whether the harness sends `valid_at` on `caura_recall`. It is the only request field
  that reaches an unconditional change in `c13b7203`.

**If A70 survives and you want the sign, not just the movement,** there is a replay
that needs no seed: re-run the 18 Sep query set against the same store with
`include_derived=false` (shipped `1c98ce18`, 23 Sep). That isolates the children's
contribution on the live corpus. A score that climbs back toward 85.4 convicts the
fan-out; a score that does not exonerates it. Same idea for D16, locally: replay with
the injection budget lifted and see whether the extra successors were carrying answers.

---

## What I could not determine

- **The clock times of the three runs.** Every date conclusion above that depends on
  hour-level ordering — `f236f34a` versus the 14 Sep deploy, A70 and D16 versus the
  record run's queries — is stated as a condition for that reason.
- **Which release was actually deployed** to the benchmark environment at each run.
  I used the `chore: release main` cut times as the earliest possible deploy, which is
  an upper bound on how early a commit could have been live, not evidence it was.
- **The `amb` tenant's search profile and the environment's variables.** No prod
  access; these are the three one-line checks above.
- **Whether the harness sends `valid_at`.** The harness is not in this repository.
- **Whether `c13b7203`'s `valid_at` normalisation is byte-identical at the driver.**
  I reasoned it through SQLAlchemy's bind typing and asyncpg's naive-datetime handling
  and found no delta, but I did not execute it.
- **The sign of anything.** As with #1704, everything here is "what changed", never
  "what got worse". A70 and D16 are both bug fixes. If either explains the delta, the
  honest conclusion is that 85.4% was measured on a store and a response shape that no
  longer exist — not that something regressed.

## One incidental defect

`core-worker/src/core_worker/consumer.py` still logs, at WARNING, on every multi-fact
enrichment:

> `child-memory fan-out is not yet implemented on the async path — secondary facts will NOT appear as child memories YET`

That has been false since `2a88514d` (9 Sep) wired the consumer up. The string was
deliberately greppable so the exposure stayed countable for the A75 proof gate; it now
miscounts in the opposite direction and would mislead anyone grepping for fan-out
behaviour during exactly this investigation. Not fixed here — flagging it.

---

## Appendix — reconciling with pm-0918-c-03's +2.4pp

> **Caution for whoever reads this next.** The reconciliation below was nearly
> written as a finding on the strength of two figures matching. They are different
> measurements that happen to share a value. On this row specifically, treat a
> coincidence of numbers as a prompt to check provenance, not as evidence — it is
> the third time a matching figure has almost become a conclusion here.

c-03 reports that excluding derived rows moved a PersonaMem run **79.8% → 82.2%
(+2.4pp)**, on a run it names `caura-bulk-2k-top50-sess2`. The tempting chain is:
85.4 was a near-unpolluted store, the later runs measure a ~28%-derived store, and
filtering recovers +2.4 of the 3.2pp gap. Three things have to be said before anyone
writes that down.

**1. There are two different 82.2s and they are probably not the same measurement.**
The row records the 18 Sep re-run at **82.2% unfiltered**. c-03 records **82.2%
filtered, from a 79.8% baseline**, on `sess2`. If both are right, `sess2`'s unfiltered
score (79.8) is not the 18 Sep run's unfiltered score (82.2), and the two results
cannot be chained — chaining them would predict 84.6, which nobody measured. Resolve
which run 79.8 belongs to before quoting a combined number.

**2. Filtering at query time is not the same as a store that never had children, and
every difference runs the same way — filtering under-recovers.**

- *Refill.* c-03 makes the point that a **server-side** exclusion placed before
  `PostFilterResults`' trim fills `top_k` exactly (storage overfetches
  `top_k * SEARCH_OVERFETCH_FACTOR`, factor 2). But that exclusion shipped as
  `1c98ce18` on 23 Sep; the +2.4pp was measured client-side, which is the shape the
  18 Sep correspondence complains about — "asking for 50 and getting
  50-minus-whatever-you-dropped". ~24 rows removed from 85 and not backfilled is a
  smaller context than a store that never had them. **+2.4pp is a floor.**
- *Recall-boost hysteresis.* `TrackRecalls` bumps `recall_count` on every returned
  row, children included, and `recall_boost` feeds back into the score. Children
  returned since 9 Sep have accrued it; the parents they displaced have not.
  Query-time filtering does not undo an accrued counter. Small after A26 dampened it
  (cap 1.1, 14-day window) but signed toward the children — and it is exactly the
  loop A41 was built to break.
- *D16.* The record run also enjoyed unbudgeted successor injection until 9 Sep 17:53.
  No amount of derived-row filtering recovers that component.

**3. One asymmetry that does *not* exist, which is why reconciliation is plausible at
all.** PostgreSQL's `ts_rank_cd` scores a row from its own `tsvector` and the query;
it carries no collection-wide IDF term. Cosine similarity is likewise per-row. So
inserting ~1,000 children did **not** change any parent's score. The pollution is
purely slot competition — and slot competition is the one kind of damage that a
filter with proper refill can undo exactly.

**Conclusion.** The numbers do not reconcile as arithmetic, and they were never going
to: 79.8 → 82.2 is a within-run delta on one run, 85.4 is a different run against a
different store state. The residual 3.2pp is better described as *not measured by that
experiment* than as *unexplained*. All three asymmetries above predict the filtered
run should land below the never-polluted run, which is the direction observed — so the
corpus-change reading is **consistent with** c-03, not **confirmed by** it.
