# oss-0924-m-05 — the 2,139 un-embedded rows: who they belong to, and whether it is still happening

**This document changes no behaviour.** It resolves a number that was measured
correctly and framed as though it were a customer-impact estimate, and it
answers the two questions the number could not. The measurement is re-runnable:
`benchmark/oss_m05_unembedded_row_provenance.py`, read-only, against any
database you can reach.

**The headline, in one sentence a non-technical reader can act on:**

> **None of the un-embedded rows in either local database belong to a customer.
> Every one of them is benchmark data or integration-test exhaust, so nothing a
> user wrote has lost semantic recall — but the mechanism that stranded them is
> live, losing about 1% of fast-mode writes as recently as the freshest data we
> have, and the job that is supposed to repair them is switched off by default.**

The original row's number (2,139) is accurate. Its implied framing — that a
population of memories has been silently un-retrievable since January — does
not survive the provenance join. The *defect* does.

---

## 1. The split

Measured on the `memclaw` database, the corpus the row was taken from. <!-- legacy-name-floor: names the local database these figures were measured from -->

| class | rows | tenants | share | how it was established |
|---|---:|---:|---:|---|
| benchmark — row carries `metadata.benchmark` | 1,545 | 67 | 72.2% | explicit marker, value `longmemeval` |
| benchmark — **row carries nothing; its fan-out PARENT carries the marker** | 261 | 37 | 12.2% | join through `metadata.parent_memory_id` |
| residual — no benchmark provenance by either route | 333 | 67 | 15.6% | remainder |
| **plausibly real traffic** | **0** | **0** | **0%** | see §2 |

**Class B is the whole reason this document exists.** Those 261 rows carry no
benchmark marker of their own, because fan-out children are built with fresh
metadata (`{parent_memory_id, source, retrieval_hint}` plus governance signals)
and do not inherit the parent's. A provenance filter that asks the row — the
obvious thing to write — classifies all 261 as real user content. It would have
reported a real-traffic residual of **594** instead of 333: a **78%
over-count**, produced by a query that looks correct and returns a number that
is wrong in the direction that makes the row look worse.

This is the same trap `benchmark/pm_c03_derived_row_population.py` documents for
derived rows, firing a second time on a different population. It is worth
stating plainly that it is not a coincidence: **any** count over a store that
contains fan-out children will under-attribute synthetic data unless it joins
through the parent.

## 2. Why the residual 333 is not real traffic either

Tenant names are free text chosen by whoever wrote the row, so
`tenant_id LIKE 'lme-%'` is a guess, not a marker. The check that does not
depend on naming is the **tenant registry**: `enterprise.tenants` is where a
tenant lands when it is created through signup/provisioning. A tenant holding
memories while absent from that registry was invented by a harness pointing at
the store directly, and no customer credential can point at it.

| | rows | tenants |
|---|---:|---:|
| residual rows in **unregistered** tenants | 332 | 66 |
| residual rows in **registered** tenants | 1 | 1 |

And the registry does not rescue the remaining one. **All 18 registered tenants
on this box carry an `@example.com` email address** — they are wet-test accounts,
every one. The single row belongs to `dev-9ff0ca`, agent `a28-wet`, and its
content is a test canary.

The unregistered 332 identify themselves without any inference at all. Their
content is, verbatim:

```
body <uuid>              REPLACED content <hex>     rewritten row <hex>
b7 canary <uuid>         a shared observation <hex>
```

Their tenants are `t-tie-*`, `t-scope-*`, `t-build-*`, `t-bulk409-*`, `b7-*`,
`default`. Their agents are `a-tie-*`, `b7-tester`, `agent-one`, `agent-two`.
Twenty-eight of them carry timestamps that are **exactly midnight UTC** on
2026-01-01, -02, -03, -04, 2026-04-01, -05-01, -06-01 — hand-set fixture dates,
not write times. That is the whole of the "silent since January" population: it
is a test fixture with a backdated `created_at`.

So the row's caveat was right to demand this split, and the answer is stronger
than the caveat anticipated. It is not that the real-traffic residual is
*small*. **On these corpora it is empty.**

## 3. The live database says the same thing, and it is the one that matters

The row was measured on `memclaw`, which is at **migration 041** and whose <!-- legacy-name-floor: names the local database the row was measured from -->
newest row is 2026-09-08. The other local database, `caura`, is at **migration
044** and holds rows through 2026-09-22 — three migrations and a fortnight
ahead. `memclaw` is a retired snapshot; `caura` is the live one, and any <!-- legacy-name-floor: names the retired local database -->
"is it still happening" question has to be asked of `caura`.

`caura` holds **265** live un-embedded rows. All 265 are in a single tenant,
`default`, and their content is `REPLACED content <hex>`. Real traffic: **zero**,
again.

One thing is strictly worse there, and it is not this row's defect. In `caura`,
**0 of the 265 have a `search_vector`** — that database has `search_vector NULL`
on all 43,197 rows (oss-0923-m-02). In `memclaw`, **all 2,139 have one**. <!-- legacy-name-floor: names the local database with intact full-text vectors -->
So the two relevance guards §5 discusses actually do their job on the corpus the
row was measured from: those rows are still reachable by keyword. On the live
database they are reachable by nothing — but the cause of that is the missing
full-text vector, not the missing embedding.

## 4. Ongoing or historical: **ongoing, and the quiet period is an artefact — twice**

"Nothing younger than seven days" invites the reading that the failure stopped.
Two separate observations make it look that way, and neither survives contact
with its denominator.

**First artefact — the store stopped.** `memclaw`'s newest un-embedded row is <!-- legacy-name-floor: names the retired local database -->
2026-09-06 and its newest row *of any kind* is 2026-09-08. The seven-day gap is
not seven days of clean writes; it is a database nobody has written to since.
And the last day that carried real volume is the worst day in the entire series:

| day | rows | fast writes | un-embedded | % of fast |
|---|---:|---:|---:|---:|
| 2026-08-27 | 16,134 | 13,134 | 35 | 0.3% |
| 2026-09-02 | 1,312 | 120 | 35 | 29.2% |
| **2026-09-06** | **10,243** | **7,806** | **1,823** | **23.4%** |
| 2026-09-08 | 39 | 39 | 0 | 0.0% |

All 1,806 benchmark rows landed on **one day**, 2026-09-06 — a single
LongMemEval run in fast mode, 84% of the whole finding. The trailing zero is 39
rows. The last thing this database observed was the failure getting an order of
magnitude worse, and then it stopped being observed.

**Second artefact — the traffic stopped.** The live `caura` database has a tail
of clean days, which is the more persuasive "it's fixed" signal and is equally
empty:

| day | rows | fast writes | un-embedded | % of fast |
|---|---:|---:|---:|---:|
| 2026-09-08 | 5,309 | 3,523 | 36 | **1.0%** |
| 2026-09-09 | 12,001 | 7,917 | 82 | **1.0%** |
| 2026-09-10 | 9,080 | 6,003 | 62 | **1.0%** |
| 2026-09-11 | 2,336 | 1,544 | 16 | **1.0%** |
| 2026-09-12 | 2,638 | 579 | 6 | **1.0%** |
| 2026-09-14 | 8,976 | 5,939 | 61 | **1.0%** |
| 2026-09-15 | 105 | 87 | 0 | 0.0% |
| 2026-09-16 | 54 | 35 | 0 | 0.0% |
| 2026-09-17 | 419 | 0 | 0 | — |
| 2026-09-22 | 275 | 2 | 0 | 0.0% |

Six consecutive days at a **dead-flat 1.0% of fast writes**. That is not a
glitch; it is a rate. Then four clean days — carrying **124 fast writes between
them**. At 1.0%, 124 fast writes are expected to produce **one** failure. Zero
is the unremarkable outcome. The tail proves that nobody ran a fast-mode load
after 2026-09-14; it says nothing whatever about whether the defect is live.

Nor has anything changed that would have fixed it: `embed_backfill_enabled` is
still `False` on `main` today, and the deferred-embed path that produced the 1%
is the one described in §5. **Verdict: unresolved, presumed live at ~1% of
fast-mode writes.**

## 5. Why they failed, and what is supposed to repair them

`write_mode=fast` defers the embedding and returns immediately — the row is
persisted with `embedding IS NULL` and, on migration 041, `embedding_pending:
true` in metadata (ax-0917-h-06 covers the read-after-write consequence). About
1% of those deferred embeds never land.

Worse, on the **live** database that marker is gone: `caura` sets
`embedding_pending` on **0 of 41,533** live rows. All 263 of its un-embedded
fast-mode rows carry **no pending flag at all**. Whatever the reason, the public
signal an agent is documented to read — "this row's vector is coming" — is
absent exactly where it would be true, so nothing downstream can tell a
deferred row from a permanently stranded one.

**A backfill exists.** `run_embed_backfill_tick` in
`core-operations/src/core_operations/tasks.py:204` fires
`POST /api/v1/admin/lifecycle/fanout/embed-backfill` daily at 04:00 UTC, and it
is described in its own docstring as *"the only periodic self-healing path for
memories that never got an embedding scheduled"*.

**It is off by default, and it is not deployed locally.**

- `embed_backfill_enabled: bool = False`
  (`core-operations/src/core_operations/config.py:137`). The comment gives the
  reason and it is a good one: the Pub/Sub topic
  `caura.lifecycle.embed-backfill-requested` is Terraform-provisioned, so until
  infra lands a fire would publish into a topic nothing consumes. (The route's
  own comment spells that topic with the pre-rebrand prefix, so the two sources
  do not agree on its name — worth settling before anyone provisions it.)
- Firing it early fails **silently**, which is why "has it run?" is not
  answerable from a log. `lifecycle.py:70-84` spells it out: `PubSubEventBus.
  publish` does not block on the publish future, so a missing topic surfaces
  only on a background thread, and the trigger returns **200 with an
  `audit_id` whose row sits at `pending` forever**.
- `core-operations` appears in **neither** `docker-compose.yml` nor
  `docker-compose.dev.yml`. On a local stack the sweep is not disabled, it is
  absent.
- `app.py:167` registers the task *conditionally* on that flag, so a deployment
  that never set it has no tick at all.
- The admin endpoint that reports the backlog,
  `GET /api/v1/admin/lifecycle/embedding-coverage`, says in its own docstring
  that before it existed the count lived only inside the VPC and *"in practice
  nobody measured it, including while turning the sweep on"*. Whether the sweep
  is on in any deployed environment is therefore a question this repository
  cannot answer — §7 is where to go and ask it.

**This has already caused an incident.** `memory_service.py:557` and `:3356`
both carry the same warning in a comment: *"a deployment that never enabled it
is how ~430 memories were stranded in the 2026-07-27 incident this module
already carries a postmortem for."* Both call sites schedule an explicit
re-embed precisely because they do not trust the sweep to exist. The two paths
that know about this problem work around it; nothing fixes it.

So: **there is a backfill, it has never run, and the code that knows it has
never run routes around it rather than turning it on.**

## 6. The guards, and the sentence in each that is wrong

The two relevance guards the row names are written as though un-embedded is
transient. A third comment, on the same column, is not — and it is the one to
copy.

| where | what it says | true for this population? |
|---|---|---|
| `core-api/src/core_api/search_trim.py` — `trim_reserving_fts_matches` | "a matching row whose embedding backfill **has not landed yet**" | no — no backfill was ever scheduled for them |
| `core-storage-api/.../postgres_service.py:2856` (CAURA-594 `row_filters`) | "could fill top_k slots … **during a large backfill window**" | no — there is no window; it is the steady state |
| `core-storage-api/.../postgres_service.py:3161` (CAURA-679) | "the CAURA-594 deferred-embed window **and any case where the embed worker fails permanently**" | **yes** — this is the only place in the tree that says it |

The behaviour of all three is correct and should not change. Only the framing
is wrong, and the correction is CAURA-679's own wording: the full-text path is
not a grace period, it is the **permanent and only** retrieval path for a row
whose embed never landed. On `memclaw` it is holding. <!-- legacy-name-floor: names the local database where the full-text fallback is intact -->

## 7. Recommendation

**Split the row.** The two halves have different severities and only one of them
is a bug.

| half | finding | severity |
|---|---|---|
| the count | 2,139 rows, 0 of them real traffic; "silent since January" is a backdated test fixture | **docs** — correct the two guard comments per §6, do not quote 2,139 again |
| the mechanism | ~1.0% of fast-mode writes lose their deferred embed, flat across six days on the live database; the repair job is off by default and the tree already records it stranding ~430 memories once (2026-07-27) | **keep at medium** — retitle around the loss rate, not the row count |

The actionable next step is **not** in these corpora, which cannot size customer
impact because they contain no customers. It is one query against staging:
whether `EMBED_BACKFILL_ENABLED` is set in the deployed core-operations config,
and what `/api/v1/admin/lifecycle/embedding-coverage` reports there. If the flag
is false in staging, §5's incident is one fast-mode load away from repeating,
and that — not 2,139 — is the sentence worth escalating.

---

## Re-running this

```bash
python3 benchmark/oss_m05_unembedded_row_provenance.py                    # local docker stack, `caura`
python3 benchmark/oss_m05_unembedded_row_provenance.py --db memclaw       # legacy-name-floor: pasteable command; the database bears this name
python3 benchmark/oss_m05_unembedded_row_provenance.py "postgresql://…"   # anywhere else
```

Read-only; it issues `SELECT` only. Sections 3a–3c need `enterprise.tenants`
and print a SKIPPED notice on an OSS-only deployment — sections 1, 2, 4 and 5,
including the ongoing-or-historical verdict, need no registry and still run.
Row content is printed only for unregistered tenants.
