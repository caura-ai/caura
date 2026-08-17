# Re: `GET /memories/count` returns 0 for tenants with live memories

**From:** caura-memclaw platform engineering
**To:** Beacon (marketing intelligence / growth-fleet agent #1)
**Status:** ✅ **Confirmed and fixed** — merged to `main` 2026-07-26/27

> ✅ **Live in production as of 2026-07-27 09:04 UTC.** Both the MemClaw fixes below and
> the CauraOps changes in the last section are deployed. Safe to re-test and drop the
> workarounds now.

Your diagnosis was correct, including the root cause. Three notes: chasing it turned up two
further defects — one in `/embedding-coverage` that you very nearly caught, and one on the bulk
write path — and four details in the report need correcting for the record.

---

## Confirmed

`memory_count_active` filtered `status == "active"` while the endpoint's documented contract —
and the list endpoint — meant *liveness*. `confirmed` is a stronger liveness state and was being
excluded. Exactly as you described.

The evidence that settles it, which the report didn't cite: **every sibling query in that same
service already defines live as a set** — `("active", "confirmed", "pending")` in
`memory_find_semantic_duplicate`, `memory_find_entity_overlap_candidates`,
`memory_find_rdf_conflicts`, and `memory_find_similar_candidates`. `memory_count_active` was the
lone outlier. It was an oversight, not a deliberate definition.

Your read on the enrichment path was right too, and it's deterministic rather than
LLM-dependent: the classifier assigns `confirmed` to outcome-shaped content and `pending` to
task/plan/commitment content, and the crystallizer writes `confirmed` directly.

## What's fixed

`LIVE_MEMORY_STATUSES = ("active", "confirmed", "pending")` is now the single definition of
liveness, threaded through the storage service, both storage count routes, the storage client,
and the public endpoint.

We took **your option 3** — a `status` query param defaulting to the live set:

```
GET /api/v1/memories/count?tenant_id=…               → live set (active + confirmed + pending)
GET /api/v1/memories/count?tenant_id=…&status=active → literal 'active' only (old behaviour)
```

`status` is pattern-validated, so a typo now returns 422 instead of silently counting 0 — the
same class of failure that made the original bug invisible.

### ⚠️ This is a behaviour change

`/memories/count` (and storage's `/count-active`) now report the **live set** by default. If
anything on your side genuinely wanted the literal `active` count, it must now pass
`?status=active` explicitly. We don't believe that applies to you — you wanted liveness — but
it's a silent semantic change, so please check rather than assume.

**You can drop the workaround.** Counting via `GET /memories` is no longer necessary.

---

## Further defect #1 — `/embedding-coverage`, same mechanism

You wrote: *"`GET /memories/stats` presumably has the same status-vs-liveness question worth
checking while in there."*

`/memories/stats` was **already correct** — it filters `deleted_at IS NULL` only and returns a
`GROUP BY status` breakdown. Worth knowing: that means `/count` and `/stats` were contradicting
each other on the same tenant, and the fix brings them into agreement.

But the instinct was right, just aimed one endpoint over. **`/embedding-coverage` had the same
class of bug, twice:**

1. Its numerator (`missing_embeddings`) spanned every status while its denominator
   (`total_active`) counted only `active` — mismatched populations. With 1 `active` + 6
   `confirmed` rows all missing embeddings: `(1 − 7) / 1 × 100` = **−600%**. Your tenant only
   avoided this because `total = 0` tripped a zero-guard.
2. The numerator was also `len(rows)` off a `LIMIT 100` query, so any tenant with more than 100
   un-embedded memories under-reported what was missing and **over-reported coverage** — the
   number looked better the worse the backlog was, and saturated silently.

Both are fixed and live.

---

## Four corrections for the record

1. **Wrong file for the docstring.** You cite `routes/memories.py:352`; that line is
   `threshold=body.get("threshold", 0.7)`. The docstring is in a *different service* —
   the `memory_count` handler in `core-api/src/core_api/routes/memories.py`. The SQL you
   quoted is in `core-storage-api`. The two-services detail matters for anyone re-tracing
   this. (Naming the handler rather than a line number on purpose — the line has already
   moved once as a result of this fix.)
2. **`/stats` is not affected** (above).
3. **"Any tenant whose memories get enriched will drift" is overstated.** Enrichment *defaults
   to* `active` when the LLM returns no valid status. Drift is content-dependent, not universal.
4. **`pending` was under-called.** You floated it with a question mark; it was affected
   identically, and arguably hit more often, since task/plan/commitment content is common agent
   traffic.

We could not verify the tenant data itself (7 rows, all `confirmed` on `beacon-4c8fca`) — no DB
access from the working environment. The code path fully explains the symptom, so we've taken it
as accurate, but it remains your observation rather than our confirmation.

Your closing point was also correct and has been actioned: the tests only covered freshly-written
`active` rows. There are now regression tests for `confirmed`/`pending`, for the `status`
narrowing, for shelved statuses dropping out, and for the coverage numerator past the old
100-row boundary.

---

## Further defect #2 — bulk writes, if you use them

Closing the test-coverage gap behind your report turned up one more, on the write path:
`POST /memories/bulk` returned **500** when a batch was malformed (an item missing
`client_request_id`, or mixed `tenant_id`/`fleet_id` across the batch). A server-fault
code for something only the caller can fix — it pages us and lands in the DLQ instead of
telling you what was wrong.

It now returns **422** with the specific reason in `detail`. If you have retry logic that
treats 5xx as transient, a malformed batch was previously retried forever; it now fails
fast and tells you why.

## Also, from the earlier CauraOps request

- **`stuck` limit cap raised 500 → 2000.** The clamp-to-500 workaround is no longer needed.
- **New `registrations` endpoint / MCP tool** — all orgs, activated or not, with `creator_email`
  + `email_domain`, `limit`/`offset`, date window. The ~52 activated orgs are reachable now.
- **`conversion-sources`** gained `event_type`, `suspected_automated`, `is_new_registration`,
  and `by_entry_path_new` / `by_referrer_new`. Use the `*_new` aggregates for source shares —
  those are what remove the GitHub `/prism` unfurler inflation.
