# Plan — Recall Efficiency + Worker Deployment (requested by Saas-code, 2026-07-07)

Origin: MemClaw-usage optimization session in Saas-code
(`Saas-code/docs/plans/memclaw-optimization-2026-07-07.md`). Client-side items
were executed there; the items below need MemClaw changes, so **execution
stopped at this plan** per Leon's instruction. Nothing in this repo has been
modified beyond adding this file.

## Measured pain (standalone instance on host `dns`, fleet `Saas-code`)

1. `memclaw_recall` / `memclaw_session_start` / `memclaw_list` serialize each
   result with full `model_dump(mode="json")`
   (`core-api/src/core_api/mcp_server.py`, recall payload assembly:
   `"results": [r.model_dump(mode="json") for r in results]`). A memory record
   is ~25 fields; a 15-result recall measured ≈ 8–9K tokens with ~25% being
   the actual content. `session_start` measured ≈ 6K tokens for 6 memories +
   5 keystones. Every agent turn that recalls pays this.
2. **core-worker is not deployed.** The service exists in the repo
   (`core-worker/`, enrichment consumer + tests) but the production
   `docker-compose.yml` (deployed at `/home/ubuntu/dev/caura-memclaw` on `dns`)
   defines no worker service — `docker ps` shows only core-api,
   core-storage-api, db, redis. Consequence: every `write_mode=fast` memory
   keeps `metadata.enrichment_pending=true` forever — `title`, tags, and type
   enrichment never happen (hundreds of null-title memories in the Saas-code
   fleet), which weakens FTS/hybrid recall precision.
3. `session_start` ranks "top-5 by weight"; most memories sit at the default
   weight 0.5, so selection is near-arbitrary — it surfaced a
   "Fake rule generated for testing" artifact (weight 0.6) as top-fleet
   context.
4. Recall returned `status=conflicted` Brain-migration imports ranked equally
   with active memories (mitigated client-side for now by passing
   `status="active"` and by a one-off triage of all 92 conflicted rows to
   active/archived).

## Proposed changes (value order)

### P1 — Deploy core-worker + drain the enrichment backlog
- Add the `core-worker` service to the production compose (mirror the dev
  compose if defined there; else write the service block: image build from
  `core-worker/`, storage-api + redis env, restart unless-stopped).
- Verify the enrichment consumer picks up NEW writes: write a `fast` memory,
  confirm `title`/tags appear and `enrichment_pending` clears within ~1 min.
- **Backfill**: decide mechanism for the existing backlog (memories where
  `metadata->>'enrichment_pending'='true'`). If the worker only consumes the
  live queue, add a small one-shot re-enqueue script (iterate backlog IDs →
  push enrichment jobs). Acceptance: backlog count → 0 for fleet `Saas-code`.

### P2 — Compact response projection (biggest token lever)
- Add `verbosity: "compact" | "full"` (default **compact**) — or a
  `fields: list[str]` param — to `memclaw_recall`, `memclaw_session_start`,
  `memclaw_list`.
- Compact projection: `id, title, content, memory_type, status, weight,
  similarity, created_at` (drop tenant_id, entity_links, run_id, usage,
  ts_valid_*, subject/predicate/object, null-heavy metadata, recall
  bookkeeping).
- Optional `content_max_chars: int` to truncate content server-side (with a
  `content_truncated: true` marker so agents know to `manage op=read` for the
  full record).
- Expected effect: ~60–70% token cut on every recall for every fleet.
  Backwards compatibility: `verbosity="full"` preserves today's shape; if
  default-compact is too breaking, ship default `full` and let clients opt in
  (Saas-code hooks/docs will adopt it immediately).

### P3 — Rank hygiene by default
- In the recall pipeline, exclude (or apply a strong score penalty to)
  `status IN ('conflicted','outdated','archived')` and rows with
  `superseded_by IS NOT NULL`, unless the caller filters explicitly.
- Keeps superseded/conflicted rows reachable on request but stops them from
  displacing active memories in default recalls.

### P4 — session_start scoring
- Replace raw-weight top-5 with a blend, e.g.
  `score = weight × recency_decay(created_at) × log1p(recall_count)`;
  exclude `memory_type='rule'` artifacts not backed by keystones.
- Apply the P2 compact projection here too, plus a per-memory content cap
  (~300 chars) — this payload is injected into every session start via the
  brain-preflight hook.

## Verify (after implementation)
- `memclaw_recall(query="tenant isolation", fleet_ids=["Saas-code"], top_k=5)`
  response < 1.5K tokens (was ~3K+).
- New fast write shows enriched `title` within 1 min; enrichment_pending
  backlog = 0.
- `session_start` top-5 contains no test artifacts and no null-title records.

## Stop rule
No code changes in this repo until Leon reviews this plan. The Saas-code side
will adopt `verbosity="compact"` in `~/.claude/brain-preflight.py` and
`.agent/rules/BRAIN_USAGE.md` once P2 lands.

## Decision log — 2026-07-07 (sprint review, plan author sign-off)

1. **`verbosity` defaults to `"full"` — ACCEPTED, do not flip.** This is the
   fallback this plan pre-authorized: a default response-shape change on a hot
   path is a breaking Public API change (repo convention 4). The token win is
   captured client-side regardless — Saas-code opts in explicitly the moment
   the param ships; flipping the default later is a one-liner if telemetry
   shows every client opting in.
2. **`content_max_chars` deferred; fixed 300-char cap in `session_start`
   (RE-06) — ACCEPTED.** session_start is the only payload injected
   unconditionally every session, so the fixed cap captures most of the win;
   per-call truncation on recall was optional (full content is the useful part
   of a compact recall result).
3. **Host identity for RE-07 — CONFIRMED:** SSH alias `dns` resolves to
   hostname `utility-53`, IP 192.168.1.53 — same machine. It runs the MemClaw
   production stack (checkout `/home/ubuntu/dev/caura-memclaw`). Note: core-api
   runs from `ghcr.io/caura-ai/caura-memclaw-core-api:latest`, so the worker
   deploy should follow the same registry-image pattern, not a local build.
4. **RE-07 deploy approval — remains Leon's, not granted here.** Production
   deployment stays behind his explicit go, per the original stop instruction.
   Live evidence the fix matters: the session-close memclaw_write from the
   originating Saas-code session itself came back `enrichment_pending: true`
   — the backlog is still growing, so the P1 backfill/drain step is required,
   not just the consumer.
