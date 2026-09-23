# pm-0918-c-03 — `include_derived`: what the decision actually costs

**This document does not change search behaviour and does not choose a shape.**
It measures the blast radius so the choice is cheap to take and cheap to
reverse. The measurement is re-runnable:
`benchmark/pm_c03_derived_row_population.py`.

The single most useful finding is the smallest one: **options (a) and (c) are
the same implementation and one boolean.** (c) — "exclude by default, inclusion
opt-in" — *requires* the request flag from (a), because opting in needs
something to opt in with. The code is identical. The decision in front of
Arkady is not which of three designs to build; it is what one default should be,
and that is a one-line change either way, in both directions, at any time.

---

## 1. What exactly marks a derived row

Two writers, and only two, put `parent_memory_id` into child metadata. Both are
in `core-api/src/core_api/services/memory_service.py`:

| marker | writer | what the row IS | excluding it |
|---|---|---|---|
| `source="atomic_fact_fanout"` | `fan_out_atomic_facts` (line ~3288) | a single claim extracted **from** a parent that keeps its full text and stays retrievable | loses redundancy only |
| `source="auto_chunk"` | the `>2000`-char chunker (lines ~425, ~1331) | the **only sub-document vector the store has** | loses retrieval on every long document |

**These are not the same population and nothing in the codebase currently
distinguishes them.** The auto-chunk parent is written with
`"content": data.content` — the whole document — but it carries **one**
embedding over all of it. That is the entire reason chunking exists. Dropping
auto-chunk children by default would not remove duplicates; it would remove the
only vectors that can match a specific passage of a long document, and the
regression would appear as "long documents stopped being findable", days later,
with nothing pointing here.

So the filter predicate is a **conjunction**, and both halves are load-bearing:

```
metadata.parent_memory_id IS NOT NULL  AND  metadata.source = 'atomic_fact_fanout'
```

- **`parent_memory_id` alone is wrong** — it catches auto-chunk children.
- **`source` alone is wrong, and unsafely so.** `source` is *deliberately*
  absent from `PLATFORM_ONLY_KEYS`
  (`core-api/src/core_api/services/system_metadata.py`, which says so in a
  comment: ingest also writes it, in caller-adjacent item metadata). A caller
  can therefore set `metadata.source = "atomic_fact_fanout"` on its own write.
  `parent_memory_id` **is** reserved and is stripped from caller input, so
  requiring both halves makes the predicate unforgeable.
- **`source_uri IS NULL` is incidental, not load-bearing.** Fan-out children
  simply never pass a `source_uri` (the payload has no such key), while
  auto-chunk children inherit the parent's. On the development corpus 19,622 of
  55,621 **ordinary** rows also have `source_uri = NULL`. A filter using it
  would delete a third of a normal store.

**Is `parent_memory_id` always set?** On both writers, yes — unconditionally, in
a dict literal, before the insert. Two caveats worth knowing:

- It is written to **top-level** `metadata`, not through `set_system_value`, so
  it does not appear under `_system`. `extract_system_metadata` merges the two
  on read, which is why the CEO sees it at `system_metadata.parent_memory_id`.
  A SQL-side filter must read the top-level key (the measurement script checks
  both, so it cannot silently under-count if a writer ever changes).
- The link is a JSON key, never a column. `memory_find_children_by_parent_id`
  (`core-storage-api/.../postgres_service.py:3837`) says so explicitly: *"a
  column would be better and is not what production rows carry"*. Any filter
  pays a JSON extraction, not an indexed column read.

**A third shape exists and a naive filter would catch it.** Rows carrying
`parent_memory_id` with neither known `source` — 24 of them on the local
`caura_storage` database. They are test fixtures
(`core-storage-api/tests/test_h10_children_by_parent_id.py` inserts exactly this
shape), not a production writer, but the measurement script counts them as a
named `ORPHAN` category so that the day something starts producing them, the
filter's behaviour on them is a decision rather than an accident.

**Two derived-adjacent populations a `parent_memory_id` filter does NOT catch,
correctly:** ingest rows (linked to a parent *Document* by `run_id`, never by
`parent_memory_id`) and crystallizer / forge rows (`source="forge"`, no parent
link). Neither is a fan-out child and neither should be excluded.

---

## 2. The population, and why the local corpus cannot price this

**State the population first.** Three local databases, read-only:

| database | live rows | scanned | newest row | fan-out children | auto-chunk children |
|---|---:|---:|---|---:|---:|
| `caura` | 41,533 | 41,400 | 2026-09-22 | **0** | **0** |
| `memclaw` | 57,354 | 57,107 | 2026-09-08 | 1,486 (2.60% of scanned) | 0 |
| `caura_storage` | 2,456 | 126 | 2026-09-22 | 0 | 16 (test fixtures) |

"Scanned" is rows whose `metadata` is a JSON object; the rest are `NULL` or
scalar and are reported separately here rather than folded in, because on
`caura_storage` they are 95% of the table. They cannot hide a derived row — a
row with no metadata object has no `parent_memory_id` — but quoting 126 as that
database's size would be wrong.

**`caura` — the only corpus written after A70 shipped (2026-09-09) — contains
zero derived rows of either kind.** It cannot answer this question at all.

`memclaw` has the only fan-out population, and it is not representative:

- all 1,486 children come from **703 parents, every one of them
  `benchmark=true` and `write_mode=fast`** — LongMemEval conversation data;
- all of them were written on a **single day, 2026-09-06**, three days *before*
  A70 shipped;
- the tenants are per-question LongMemEval isolates (`lme-N-<hash>`), median
  size ~33 rows.

**A provenance trap that already caused one bad number, and would cause another
here.** Fan-out children do **not** inherit the parent's `benchmark` marker —
`child_meta` is built fresh as `{parent_memory_id, source, retrieval_hint}` plus
governance signals. Query the child's own metadata for provenance and all 1,486
synthetic rows report as real user content. The script joins through the parent
instead. This is the same failure mode that made pm-0918-c-04's first table
99.4% synthetic without saying so.

### The one number that does transfer

Within the 195 tenants that have any fan-out at all, derived rows are **22.4% of
the tenant's entire store on average** (individual tenants 18–57%). That
brackets the CEO's ~28%-of-returned-rows observation from the write side, and it
supports a reading that matters:

> The 28% is most likely **corpus composition**, not fan-out children
> out-ranking their parents.

But the same table carries the caveat that stops this from settling anything.
**187 of those 195 tenants hold fewer than 50 non-derived rows** (mean: 26). On
a store that small a `top_k=50` returns essentially the whole tenant, so the
derived share of *returns* is just the derived share of the *store*, and a
server-side filter cannot promote better rows — there are none left to promote;
it only returns a shorter, cleaner list.

**PersonaMem's store is not that shape**, and this is the fact that makes the
server-side filter worth building: the CEO's measurement reports ~24 derived of
**85 requested** rows, on a run named `caura-bulk-2k-top50-sess2`. Getting 85
rows back means the candidate pool was not exhausted — there *were* more rows to
promote. So filtering there genuinely promotes real memories, and the 79.8% →
82.2% (+2.4pp) result is a ranking gain, not just a tidier payload.

**What this corpus therefore cannot tell you:** how much of a *typical* store is
derived, or how much of a typical top-50 they occupy. Nothing here should be
quoted as a general rate. The number that would settle it is a direct count on
the PersonaMem store, which nobody has run.

### The top_k objection is already solved by the existing overfetch

The row's strongest argument for a server-side fix is that client-side filtering
distorts `top_k` — ask for 50, get 36. That argument is right about
client-side filtering, and it is worth knowing that a server-side filter costs
nothing to get right: storage already returns `top_k * SEARCH_OVERFETCH_FACTOR`
candidates (factor = 2, `core-api/src/core_api/constants.py:832`) and
`PostFilterResults` trims to `final_top_k` afterwards. A derived-row exclusion
placed in that step, **before** the trim, fills `top_k` exactly for any derived
rate below 50% of the candidate pool. At the measured ~28%, 100 candidates
become ~72 survivors and all 50 slots fill.

That placement also bounds the blast radius to the search pipeline. Write-path
dedup and contradiction candidate selection go through `_find_semantic_duplicate`
(a direct embedding query, not the search pipeline) and are untouched.

---

## 3. Who consumes derived rows today

Every reader of `search_memories`, and what excluding fan-out children by
default does to each:

| consumer | call site | effect of excluding by default |
|---|---|---|
| `POST /search` | `routes/memories.py:2388` | **the decision** — depends on the caller |
| `POST /recall` | `routes/memories.py:2705` → `summarize_memories` | **helps.** Children are summarised into a brief; short redundant fragments spend brief tokens restating the parent. |
| MCP `caura_recall` | `mcp_server.py:1349` | **helps**, same reason. |
| Plugin auto-recall | `plugin/src/context-engine.ts:1017`, `top_k=5` | **helps most.** Five slots, rendered as `- [type] content` lines. A fan-out child is a one-line restatement of a parent it is competing with; at `top_k=5` one duplicate costs 20% of the context block. |
| Plugin bootstrap smoke test | `plugin/src/context-engine.ts:632`, `top_k=1` | **neutral-to-better.** Asserts score ≥ 0.7 on a row it just wrote; it would now match the parent rather than possibly a child. |
| Plugin heartbeat probe | `plugin/src/heartbeat.ts:653`, `top_k=1` | **neutral.** Only cares that the call returns. |
| Python SDK `.search()` / `.recall()` | `clients/python/src/caura_client/client.py:106,124` | **pass-through.** No filtering of its own; inherits whatever the server does. |
| TypeScript SDK `.search()` / `.recall()` | `clients/typescript/src/index.ts:170,191` | pass-through, same. |
| Dashboard | `frontend/app/(dashboard)/demo/page.tsx:279` | **neutral.** Only the demo page calls `/search`; the memory browser lists via `GET /memories`, which this does not touch. |
| **caura-daemon (on-prem broker)** | `cloud.Client.Search`, serving MCP `memory_search` | **the one that can break.** See below. |
| PersonaMem adapter | external; already filters client-side via `CAURA_SOURCE_ONLY` / `CAURA_SHOW_TITLE` | **helps** — it is doing this by hand today, which is the evidence the need is not benchmark-specific. Those env vars are **not in this repo**; that claim is from the tracker row and is not verified here. |

**Nothing in the tree reads a fan-out child on purpose.** There is no consumer
of `source="atomic_fact_fanout"` outside the writer, the governance cascade
(`memory_find_children_by_parent_id`, which finds children by parent id and does
not go through search) and the tests. No ranking, lineage or insights path
depends on children appearing in search results.

**Two inconsistencies a default-off would create**, neither fatal, both worth
deciding deliberately:

1. `GET /memories` would still list rows that `POST /search` can no longer
   find. Two endpoints, two row sets, no error.
2. Excluded rows never get a `recall_count` bump, so `recall_boost` can never
   engage on them. Turning the flag back on later returns rows that have been
   frozen at zero reinforcement for however long it was off.

---

## 4. Contract surface: `/search` is frozen, and no gate catches this

**`POST /api/v1/search` is in the frozen v1 broker subset** —
`core-api/scripts/gen_broker_openapi.py`, `BROKER_OPERATIONS`, called by
`cloud.Client.Search` in caura-ai/caura-daemon. It is also listed as a stable
REST surface in `docs/public-api-stability.md`.

**The answer to the question asked: this is a SEMANTIC change with no schema
movement, and the gate passes it silently.** Concretely, for each option:

| change | what oasdiff sees | verdict |
|---|---|---|
| add optional `include_derived: bool = true` | a new **optional** request property | non-breaking — passes |
| flip the default to `false` | **nothing at all** — a default value on an optional request field is not a breaking-change signal | passes **silently** |
| honour a write-time `metadata.amb_bench` | nothing — `metadata` is a free-form object | passes silently |

This is exactly the ax-0917-m-19 shape. The only things standing between a
default flip and an on-prem broker quietly returning different rows are the
repo's own conventions: a `BREAKING CHANGE:` commit trailer and the
`kind/breaking` PR label, which `docs/public-api-stability.md` requires and
which reviewers are told to block on. Those are human gates, not CI gates.

The skew direction has one piece of good news and one trap:

- **Good:** a client sending `include_derived=false` to a server that does not
  know the field gets it echoed back in `SearchResponse.warnings` — the
  `extra="allow"` + `_unknown_param_warnings` machinery from ax-0917-h-05. The
  flag fails *loudly* on a stale server, not silently.
- **Trap:** that machinery is itself days old. A server predating it silently
  discards the key and returns derived rows anyway, with the caller believing it
  filtered.

---

## 5. Options, and the recommendation

| # | shape | solves the stated problem | breaks | caught by a gate | cost to reverse |
|---|---|---|---|---|---|
| **a** | request flag `include_derived`, **default `true`** | yes, fully — the caller gets exactly `top_k` real rows | nothing | additive; oasdiff passes it correctly | n/a |
| **b** | honour a write-time opt-out (`metadata.amb_bench`) | partly | nothing | invisible to every gate | requires rewriting rows |
| **c** | same flag, **default `false`** | yes, and for callers who never ask | any reader relying on derived rows — in practice the on-prem broker | **no** — silent semantic change on a frozen endpoint | one line, but a second contract event |

**Reject (b) outright.** It is the only option with no compensating benefit: it
is benchmark-shaped (`amb_bench` is a specific harness's key), it keys read-time
policy on a *forgeable, caller-owned* metadata field, and it puts a retrieval
decision into write-time data where changing it means rewriting every affected
row. The per-tenant write-side switch A70 already shipped
(`enrichment.atomic_fact_fanout_enabled`, default ON) covers everything (b) was
reaching for, today, with no API change.

**Recommendation: build (a) — the flag, defaulting to `true` — plus a per-tenant
`search.include_derived` default, in one PR. Flip PersonaMem's tenant to
`false` the same day. Revisit the global default at the next minor.**

The reasoning is that this is the choice that makes the decision reversible
rather than the choice that settles it:

- It gives the CEO exactly what was asked for, immediately, and the +2.4pp on
  the PersonaMem store without waiting for a contract event.
- `search.*` is an existing per-tenant boolean namespace
  (`organization_settings.py:657-661`: `strict_fleet_scoping`, `recall_boost`,
  `graph_retrieval`, `entity_retrieval`). `search.include_derived` is one entry
  in a shape that already exists, and it gives any store the default-off
  behaviour of (c) without imposing it on every store — the same argument that
  justified A70's per-tenant switch.
- The global default then becomes a one-line change, taken later, with a second
  store's worth of evidence and a `BREAKING CHANGE:` trailer, rather than taken
  now on one store's 2.4pp.

**I have real sympathy for (c) and the argument for it is stronger than
"breaking" makes it sound**: default-on fan-out is **14 days old** (A70 shipped
2026-09-09), so almost nobody can be depending on it yet, and the window for
changing it without ceremony is now rather than in six months. If Arkady wants
(c), it is a defensible call — take it now, not later — and the conditions are:
the predicate is the conjunction in §1 (auto-chunk children stay), the PR
carries the `BREAKING CHANGE:` trailer and `kind/breaking` label, and a test
pins the default so a future refactor cannot flip it back unnoticed.

### What I would have to be wrong about for this to flip

Two facts, either one of which changes the answer:

1. **That derived rows help no caller.** The evidence for this is an absence —
   nothing in the tree reads them on purpose — and an absence is not a survey of
   what on-prem broker deployments do. **One question to caura-daemon's owner
   settles it.** If the answer is "nothing depends on them", (c) becomes the
   right call today and the case for staging it through a tenant setting
   largely evaporates.
2. **That +2.4pp generalises.** It is one store, one run, one seed. If a second
   store reproduces it, (c) is worth the contract event on its own. If a second
   store shows no gain, then even (a) is only an ergonomics fix and the default
   should certainly not move.

A third, smaller one: if the PersonaMem store turns out to have **fewer
non-derived rows than its `top_k`** — the shape 187 of 195 local tenants have —
then filtering server-side returns a shorter list rather than a better one, and
the whole gain is payload size rather than accuracy. The "85 rows returned"
figure argues against that, but it has not been checked directly, and
`benchmark/pm_c03_derived_row_population.py` run against that store answers it
in one command.

---

## Re-running

```bash
python3 benchmark/pm_c03_derived_row_population.py                     # local docker stack, `caura`
python3 benchmark/pm_c03_derived_row_population.py --db memclaw        # the corpus with the only fan-out
python3 benchmark/pm_c03_derived_row_population.py "postgresql://…"    # any store
```

Four results: store composition (including the `ORPHAN` safety net), parent
provenance and write mode, per-tenant derived share, and top_k headroom. Read
§2 before quoting any of them.
