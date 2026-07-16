# Interviewer Phase 1 — Open-Question Resolutions

**Branch:** `interviewer-phase1` · **Date:** 2026-07-16
**Parent specs:** MemClaw doc store `design_docs/interviewer-feature-plan`, `design_docs/interviewer-phase1-spec`

Task #1 of the Phase-1 prep: the four questions that gated the build, resolved against source.

---

## Q4 — Is there an existing OpenClaw gateway trail the plugin can read? → **NO. Build the on-disk buffer.**

Grounding:
- `plugin/src/openclaw-sdk-bridge.ts` exposes **no** transcript / session-persistence surface (grep: `transcript|history|sessionFile|persist|.jsonl|readSession` → zero hits).
- `plugin/src/context-engine.ts:128` — today's buffer is an **in-memory LRU** capped at `SESSION_BUFFER_CAP`; old events are dropped even without a crash. Only user messages are persisted (as episodes, write-capped per session).
- `static/docs/integration-guide.md` — no session-persistence mention.

**Decision:** Fork 1 stands as spec'd — the plugin writes its own append-only `interview-buffer.jsonl` under the plugin state dir (`~/.openclaw`, per `paths.ts`). If OpenClaw later exposes a persisted transcript, only the buffer *writer* is swapped; the contract is untouched.

---

## Q2 — Watermark granularity: per-node or per-session? → **Per-node. Session id travels as event metadata.**

Grounding (`plugin/src/context-engine.internal.ts:25`):
- `getSessionKey` prefers the per-call `sessionId/sessionKey` OpenClaw ≥2026.5.4 passes, **but** falls back to a factory default (`"default:main-<installId>:default"`), and legacy-compat paths mint **synthetic per-instance ids** (`legacycompat-<ts>-<rand>`, `context-engine.ts:520`).

Session boundaries are therefore **not guaranteed stable** — per-session watermarks would fragment across synthetic ids and defaults.

**Decision:** Phase 1 keys the watermark **per node** (`wm_<sha1(node_id)>`, server-authoritative), and every buffered event carries its `sessionKey` in the C2 `session_id` field so the interview prompt can group by session. Revisit per-session cursors in Phase 2, where disk trails have real per-session files.

---

## Q3 — Is redaction sufficient pre-broker? → **YES, with one addition: mask events at `/interview/submit` ingestion, *before* the interview LLM.**

Grounding (`core-api/src/core_api/pipeline/steps/write/governance_scan_content.py`):
- `GovernanceScanContent` is a **deterministic PII/secret gate at the ingestion boundary** — regex / Luhn / IBAN / entropy detectors — running in **both** write modes, positioned **before** `ComputeContentHash`, so hash/dedup/embedding/stored-row all see the redacted form. Per-tenant action: mask-in-place / drop (422) / flag. Every action audit-logged. LLM free-form PII signal applied separately (`GovernanceDecision` strong-mode / post-write remediation fast-mode).

The nuance the spec missed: that gate protects **persistence**. The interview worker runs the LLM **before** `/memories/bulk`, so raw window content would reach the platform LLM pre-governance.

**Decision:** the `/interview/submit` handler runs the **same shared library** (`common.governance.scan/mask`) on `events[].content` immediately on receipt — before the interview prompt sees anything. Bulk-write governance then runs again on the Report (defense in depth, zero new code — it's always-on). Plugin-side regex stays a lightweight optional wire-privacy improvement, not a correctness requirement. Phase-1 redaction posture: **adequate for the opt-in tenant; full pre-wire redaction remains Path A's (broker) value.**

---

## Q1 — Window size vs. LLM context → **Cap per submit + worker-side map-reduce; the watermark loop is the catch-up mechanism.**

Grounding: `core-api/constants.py:144` (`CHUNKING_THRESHOLD_CHARS = 2000`) establishes the chunking precedent; platform enrichment model is small-context-tier (`gpt-5.4-nano` class).

**Decision — three stacked caps, no giant payloads, no lost data:**
1. **Submit cap (plugin):** at most `INTERVIEW_MAX_EVENTS_PER_SUBMIT` (initial: 500) / ~2 MB per `/interview/submit`. The plugin submits `[since_seq, min(head, since_seq+cap)]`.
2. **Catch-up via the cursor (free):** the watermark only advances to `cursor_to`. If the buffer head is beyond it, the next scheduler tick issues a new `interview_request` from the new cursor — the crash-safe loop doubles as the backlog-drain loop. No special backlog mode.
3. **Worker map-reduce (server):** events chunked into token-budgeted windows (initial: ~24k tokens/chunk); map = per-chunk mini-report (same fixed JSON schema); reduce = merge mini-reports into the final Report (dedup episodes, union decisions/outcomes). Single-chunk windows skip the reduce.

Initial numbers are config, not constants — tune on the opt-in tenant.

---

## Net effect on the build tasks

| Task | Delta |
|---|---|
| #2 buffer | confirmed required; add `sessionKey` per event; add submit cap |
| #3 handler | submit `[since_seq, head∧cap]`; no backlog special-casing |
| #4 worker | **add**: `common.governance` mask on receipt; map-reduce with 24k-token chunks |
| #5 scheduler/watermark | per-node key confirmed; cadence tick naturally drains backlog |
| #6 e2e | add backlog-drain case (buffer > cap → multiple ticks converge) |
