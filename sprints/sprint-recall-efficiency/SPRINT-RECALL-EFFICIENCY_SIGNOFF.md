# Sprint Signoff: Recall Efficiency

Branch: `sprint/recall-efficiency-plan` · integrated to `origin/main` via PR
[#536](https://github.com/caura-ai/caura-memclaw/pull/536) · deployed to
utility-53 at commit `4889251`.

## Execution Summary

- WorkItems: **8** (RE-01…RE-07 + RE-Z01).
- Completed: **7** (RE-01…RE-06 + this signoff). **Partial: 1** (RE-07 — deploy
  + root-cause fix done; backlog backfill deferred, infeasible on standalone).
- Plus one pre-existing repair (core-worker retry test) and a full `origin/main`
  integration (11 commits) with a live-DB migration reconciliation.
- Full suite green throughout: **3867 passed, 3 skipped** on the integrated tree.

## WorkItem Status

| ID | State | Feature commit | Bookkeeping |
|----|-------|----------------|-------------|
| RE-01 | completed | (docker-compose core-worker + inline mode) | b4b0c05 |
| RE-02 | completed | b949e0d | bce1c91 |
| RE-03 | completed | 869c32b | b60fd83 |
| RE-04 | completed | 237e8d9 | c29d668 |
| RE-05 | completed | ef85cd9 | 3a42974 |
| RE-06 | completed | 6dd2015 | f2d35f1 |
| RE-07 | **partial** | live deploy of `4889251` | this doc |
| RE-Z01 | completed | this doc | — |
| — | (pre-existing repair) | 57b1107 (retry test 3→5) | — |
| — | (integration) | 4889251 (merge origin/main) | — |
| — | (docs) | 471cc3a (CLAUDE.md) | — |

## Acceptance Criteria — Verified

- **RE-02** — enrichment-backlog re-enqueue backfill: 7 unit tests; full call
  chain traced cli→backfill→storage_client→router→postgres_service.
- **RE-03** — `verbosity=compact` on recall/list/session_start (default `full`,
  byte-identical); recall cache keyed on verbosity. Cache-collision regression
  test proven to fail on the pre-fix key. Tool surface: 20 tools, token budget
  7488/7500. **Verified live on utility-53:** compact recall 2605 chars vs full
  4338 (~40% smaller).
- **RE-04** — verbosity behavior/regression suite: compact-shape-exact (+negative
  drop assertions), full-default == MemoryOut field set, cache no-cross-
  contamination, ratio-based latency guard (0.971×), compact ≈36% of full.
- **RE-05** — `status='archived' → 0.5` demotion in scored search; #357 carve-out
  byte-identical; ranking test proven to fail without the arm. (Plan's
  `superseded_by` arm dropped — no such column; superseded rows already
  `outdated`.)
- **RE-06** — session_start recency/usage re-rank (`weight × recency_decay ×
  (1+log1p(recall_count))`), excludes `rule` rows, 300-char compact cap; default
  shape additive. 6 behavior tests.
- **RE-07** — Human approval given per step. Prod DB reconciled `030 → 032`
  (added `agents.belonging_type`/`owner_ref` + `agent_activity_digests`,
  procedures intact). Code `4889251` deployed; verified via raw `curl` JSON-RPC:
  `/api/v1/health` ok, tools/list = 20 with `verbosity`, `verbosity=compact`
  tools/call `isError:false`. Root-cause: `USE_LLM_FOR_MEMORY_CREATION=false`
  (`env.dev:8`) flipped to `true` + core-api restarted → a fresh write clears
  `enrichment_pending` ("Background enrichment succeeded"). **Partial:** see
  Backlog.

## Artifacts Produced

- Code: `core-worker/backfill.py` + CLI, `mcp_server.py` (`_project_compact`,
  `_session_rank_score`, verbosity wiring), `postgres_service.py` (archived
  status_penalty arm + scope_filters typing), `memclaw_session_start.py` desc.
- Tests: `test_enrichment_backfill.py`, `test_mcp_verbosity_projection.py`,
  additions to `test_mcp_recall/list/session.py`, `test_integration_search.py`
  (archived demotion), `test_ph5b_insights_storage.py` (entity-seed FK fix),
  `test_skill_schema_v1.py` (migration chain), retry-test repair.
- Migrations: renumbered procedures chain `030/031/032`; alembic head `032`.
- Ops: prod DB backup `memclaw_predeploy_20260707_233859.sql.gz` (4.8M on host);
  `env.dev.predeploy_bak`.

## Failure Analysis

- **git rerere contamination (caught):** the first `origin/main` merge silently
  replayed a stale recorded resolution injecting an own-vs-any `enforce_delete`
  change present on NEITHER branch (a delete-authz path). Detected by comparing
  the merged function to both branches; rejected by re-merging with rerere
  disabled. No delete-authz behavior changed.
- **Live-DB migration collision (resolved):** prod at alembic `030` had
  procedures but not agent_belonging/digest; branch had renumbered procedures.
  Rehearsed the reconciliation on a scratch DB, then applied
  `stamp 027 → upgrade 029 → stamp 032` to prod.
- **Stale test-DB schema:** `create_all` doesn't ALTER existing tables; rebuilt
  the dedicated test DB to pick up origin's new columns.

## Lessons Learned

### What Went Well
- Every hot-path change shipped additive with a ratio latency guard; regression
  tests proven against pre-fix code before claiming done.
- The migration reconciliation was rehearsed on a scratch DB before touching
  prod, and a backup was taken first — zero prod data loss.
- Read-only recon before any irreversible prod action surfaced the migration
  collision *before* it could corrupt the live DB.

### What To Improve
- Disable `rerere` for integration merges of long-diverged branches, or always
  diff auto-"resolved" security-sensitive files against both parents.
- The `/deploy-53` skill's health path (`/health`) is stale → `/api/v1/health`;
  it also assumes core-worker is deployable (it isn't on standalone). Update it.
- The branch carried 89 commits of prior unmerged work; integrate sprints to
  `origin/main` continuously so a single PR isn't 90 commits.

## Backlog (deferred by explicit scope decision)

1. **Enrichment backlog (91 memories `enrichment_pending=true`) not cleared** —
   RE-07 AC6. Needs `core-worker` (absent from prod compose) + a cross-process
   event-bus consumer (not configured in standalone). Flag flip fixes NEW writes
   only.
2. **core-worker not deployed** (RE-07 AC4) — not in the prod compose; standalone
   uses inline enrichment via core-api background tasks.
3. **Enrichment quality:** post-flip titles/summaries are heuristic
   content-echoes (fast-mode `fake_enrich`), not LLM summaries; tags empty.
   Verify the LLM provider/mode is active on prod (do NOT expose OPENAI_API_KEY).
4. **Flip `verbosity` default to `compact`** — breaking Public-API change, needs
   explicit sign-off; clients adopt compact client-side meanwhile.
5. **`content_max_chars` param on recall/list** — plan-optional, deferred.
6. **Recall cache under-keying** beyond verbosity — `fleet_ids`/`status`/
   `memory_type` also absent from the hash (pre-existing; own fix).
7. **Cross-process event-bus (GCP Pub/Sub) wiring** — prerequisite for
   `DEPLOYMENT_MODE=deferred` / core-worker consumption on any host.

## Recommendations for Next Sprint

- Land PR #536, then stand up core-worker + event bus on utility-53 and run the
  RE-02 backfill to clear the 91-memory backlog.
- Confirm LLM enrichment (not heuristic) is active in prod, gated on a valid
  provider key.
- Decide the `verbosity=compact` default flip with Saas-code hooks.
