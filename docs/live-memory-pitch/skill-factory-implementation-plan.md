# Skill Factory — Implementation Plan v1.0

**Status:** approved for implementation (decisions ratified 2026-05-10).
**Owner resident:** Forge.
**Tenant of record (design memories):** `arkash24-4d270c` / `caura-dev-fleet` (re-log post-finalization).
**DO NOT START** any coding until Ran's explicit green light on this plan.

> **One paragraph.** Forge — a new lake-side resident — passively mines outcome-tagged session clusters from MemClaw's memory stream, distills them into proposed skills (with content body, frontmatter, provenance, scan, hashes), drops them into the existing `skills` collection with `status: staged`, surfaces them in a HITL Skills Inbox, and on approval installs to Claude Code / OpenClaw plugin / direct-MCP via the existing distribution layer. Same `skills` collection holds Forge-generated *and* manually-authored skills, distinguished by a `source` discriminator. Agents can also write skills directly via `memclaw_doc` (no new MCP tool). OpenClaw's Skill Workshop conventions (hash-binding, quarantine, rollback, caps, frontmatter strip-on-apply) are adopted; OpenClaw's *origin signal* (single-conversation) is not — Forge's fleet-collective mining stays the moat.

---

## 1. Goals · Non-goals

### Goals (MVP)

1. A tenant connects MemClaw and runs agents normally. Forge accumulates skill candidates without anyone calling `memclaw_evolve` or any new tool.
2. One click in the Skills Inbox approves a candidate; one more installs it to Claude Code / OpenClaw / Direct-MCP.
3. Installed skills produce outcomes that flow back; v2 update proposals are auto-generated when behavior drifts.
4. Same `skills` collection holds both Forge-generated and human-authored skills; `source` + `status` fields keep them distinguishable.
5. Skills are full-content (PROPOSAL.md body + frontmatter), not pointer-only.
6. Safety primitives shipped in MVP: security scan, hash-binding, rollback metadata, hard caps.

### Non-goals (explicit, for v1)

- Branching procedures with per-branch success rates. (Flat MVP; reconsider in Phase 4.)
- Cross-tenant / cross-fleet skill marketplace. (Enterprise, post-MVP.)
- Auto-promote tier (skip HITL on gold candidates). (Phase 5+.)
- Custom Concierge residents. (Phase 5+.)
- Migration of eToro's 1,402 existing pointer-only skills. (Out of scope; Ran will work with eToro to adjust their import to full-content shape.)
- A separate `memclaw_skill_workshop` MCP tool. (Rejected; `memclaw_doc` is the agent surface.)

---

## 2. Architecture overview

See `08-skill-factory-architecture.svg` for the big-block flow.

**One-line shape:** producers → outcome inference → cluster + fingerprint → Forge distill → Sentinel scan → `skills` collection (lifecycle) → HITL Skills Inbox → harness install → outcome loopback.

Components:

| # | Component | Status | Lives in |
|---|---|---|---|
| 1 | Memory store (lake) | existing | `caura-memclaw` `common/` + storage-api |
| 2 | `memclaw_doc` MCP tool | existing | `caura-memclaw` `core-api/src/core_api/routes/documents.py` |
| 3 | `skills` collection | existing | DB table `documents` with `collection='skills'` |
| 4 | Plugin reconciliation → `plugin/skills/<slug>/SKILL.md` | existing | `core-api/routes/plugin.py` |
| 5 | Direct-MCP skill adapter → `static/skills/memclaw/SKILL.md` | existing | served from `static/` |
| 6 | `contradiction_detector` | existing | `core-api/services/contradiction_detector.py` |
| 7 | `evolve_service` (outcome+rule) | existing | `core-api/services/evolve_service.py` |
| 8 | `insights_service` clustering primitives | existing | `core-api/services/insights_service.py` |
| 9 | `lifecycle_publishers` substrate | existing | `common/events/lifecycle_publishers.py` |
| 10 | **Outcome inference layer** (6 signals) | NEW | `core-api/services/outcome_inference/` |
| 11 | **Session-trace builder** | NEW | `core-api/services/session_trace.py` |
| 12 | **Cluster fingerprint** | NEW | `core-api/services/forge/fingerprint.py` |
| 13 | **Forge resident (distillation)** | NEW | `core-api/services/forge/forge_service.py` |
| 14 | **Sentinel scanner** | NEW | `core-api/services/forge/sentinel_scan.py` |
| 15 | **Skill lifecycle + hash-binding** | NEW | `core-api/services/skill_lifecycle.py` |
| 16 | **Skills Inbox UI** | NEW | `frontend/.../skills-inbox/` |
| 17 | **Claude Code install path** | NEW | `core-api/services/forge/harness_install.py` |
| 18 | **Rollback metadata writer** | NEW | shares lifecycle service |
| 19 | **v2 diff card proposer** | NEW (Phase 4) | extends Forge |
| 20 | **OpenClaw PROPOSAL.md emitter** | NEW (Phase 5) | `core-api/services/forge/openclaw_bridge.py` |

---

## 3. Skill schema (v1 — canonical `skills` collection doc shape)

Stored in `documents.data` (jsonb), `collection='skills'`, `doc_id` namespaced by source:
- Forge-generated: `forge/<slug>` (e.g. `forge/deploy-eu-west-dns`)
- Agent-direct via `memclaw_doc`: `agent/<slug>` (or plain `<slug>` for backwards compat)
- Hand-authored / bulk-imported: plain `<slug>` (existing convention)

```jsonc
{
  // Identity (always present)
  "name":         "Deploy to eu-west · use fallback DNS at step 7",
  "slug":         "deploy-eu-west-dns",
  "version":      "v1",                          // semver-ish; bumped per applied revision
  "kind":         "create",                      // create | update
  "source":       "forge",                       // forge | agent | manual | imported
  "status":       "staged",                      // see lifecycle §5

  // Trigger surface (Claude Code / OpenClaw read these)
  "description":  "Use fallback DNS resolver when eu-west deploy step 7 hangs.",
                                                 // ≤ 160 bytes (default; configurable)
  "summary":      "Detects a hung step 7 on eu-west deploys and switches to the fallback DNS resolver before retrying.",
                                                 // longer; used for hover/inspect
  "domain":       "devops",                      // matches eToro convention
  "tags":         ["deploy", "eu-west", "dns", "incident-mitigation"],

  // Body
  "content":      "## When to use\n…\n## Steps\n1. …",  // full markdown (PROPOSAL.md body)
  "content_hash": "sha256:…",                    // hash of `content` for stale detection

  // Frontmatter-strip metadata (cleared on apply)
  "frontmatter": {
    "date":     "2026-05-12T14:01:00Z"           // when proposal was first written
  },

  // Provenance (machine + human)
  "cites":        ["mem-8821", "mem-8932", "mem-9011", "mem-9134"],  // memory IDs
  "goal":         "Deploy to eu-west without hanging on step 7.",     // ≤ 280 bytes
  "evidence":     "Across 4 sessions / 3 agents in Apr 18–28, step 7 hung in eu-west; fallback DNS resolved every time. 100% success when applied.",  // human prose, ≤ 600 bytes
  "cluster_fingerprint": "fp:v1:7a4f…",          // canonical id (see §8)

  // Origin (who/where this came from — borrowed from OpenClaw)
  "origin": {
    "agent_id":    "forge",                      // for source=forge: the resident
    "session_key": null,                         // forge has no single session
    "run_id":      "forge-run-20260501-0600",    // the Forge run that produced it
    "message_id":  null
  },

  // Support files (Phase 3+; allowed only under fixed prefixes)
  "support_files": [
    {
      "path":      "scripts/check-dns.sh",       // assets/ examples/ references/ scripts/ templates/
      "size_bytes": 412,
      "hash":      "sha256:…"
    }
  ],

  // Hash-binding (for `kind: update` — links to the target it modifies)
  "target": {
    "slug":              "deploy-eu-west-dns",
    "previous_version":  "v1",
    "target_content_hash": "sha256:…"            // hash of live skill at proposal time
                                                 // → if live skill changes, status flips to `stale`
  },

  // Sentinel scan state
  "scan": {
    "state":      "clean",                       // pending | clean | failed | quarantined
    "scanned_at": "2026-05-01T06:01:33Z",
    "critical":   0,
    "warn":       0,
    "info":       1,
    "findings":   []
  },

  // Lifecycle stamps
  "created_at":     "2026-05-01T06:01:30Z",
  "updated_at":     "2026-05-01T06:01:35Z",
  "applied_at":     null,
  "rejected_at":    null,
  "quarantined_at": null,
  "stale_at":       null,
  "deprecated_at":  null,
  "status_reason":  null,                        // free text on terminal transitions

  // Telemetry (filled by outcome-loop in Phase 4)
  "telemetry": {
    "fires_total":          0,
    "fires_success":        0,
    "fires_failure":        0,
    "last_fired_at":        null,
    "utilization_30d":      null
  }
}
```

### Backwards-compatibility with the existing `skills` collection

- Existing manual docs (`caura-shared-tasks`, `onprem-deploy-runbook`) get `source: "manual"` and `status: "active"` retroactively (one-shot migration script in Phase 0). They don't have `content_hash` or `cites` — those stay null and the lifecycle treats them as "frozen" until edited.
- The eToro 1,402 pointer-only skills get `source: "imported"` and `status: "active"`. Ran will coordinate with eToro to migrate them to full-content shape on their own timeline; our code tolerates both shapes via a `data.content` IS NULL guard.
- The plugin reconciliation path (`core_api/routes/plugin.py`) becomes lifecycle-aware: it only emits skills with `status: "active"`.

---

## 4. Direct authorship via `memclaw_doc` (no new tool)

Agents who want to draft a skill *synchronously* use the existing `memclaw_doc` MCP with `op=write`, `collection=skills`, `data=<schema above>`. **Required adjustments to `memclaw_doc` for the skills collection (Phase 0):**

1. **Schema validator hook.** When `collection='skills'` and write_mode='strong', validate the doc against the schema (required: `name`, `slug`, `description`, `domain`, `kind`, `source`). Reject with 422 if any required field missing.
2. **`description` cap enforcement.** Default 160 bytes; tenant-configurable via `org_settings.skills.description_max_bytes`. Reject with 422 on over-cap.
3. **`source` defaulting.** If unset on write, default `source: "agent"` when caller is a regular agent; `source: "manual"` only when caller has admin role.
4. **`status` defaulting.** If unset, default `status: "staged"` (goes to inbox). Direct `status: "active"` writes require admin role.
5. **Auto-fill content_hash, origin.agent_id, created_at, updated_at.** Inferred from the request, not trusted from the body.
6. **Auto-trigger Sentinel scan** on every skills-collection write (sync; reject if `critical > 0`).
7. **`kind: update` hash-binding.** When `kind: "update"`, require `target.target_content_hash` and reject if it doesn't match the live skill's current hash.

**This means: an agent writing via `memclaw_doc` goes through the SAME lifecycle as a Forge-generated candidate.** Same Inbox card, same approval gate, same install path. Forge is just the autonomous producer alongside human-curated ones.

---

## 5. Lifecycle states + transitions

```
        write
          │
          ▼
   ┌─────────────┐    auto-gates       ┌──────────┐
   │  candidate  │───── pass ─────────▶│  staged  │
   │  (silent)   │                     │ (inbox)  │
   └─────────────┘                     └────┬─────┘
                                            │
                       Approve              ▼
                       ─────────▶  ┌─────────────────┐
                                   │     active      │
                                   │   (installed)   │
                                   └────┬────────────┘
                                        │
                       v2 mined or      ▼
                       drift detected
                                   ┌─────────────────┐
                                   │   deprecated    │
                                   └─────────────────┘

   side states (from staged or active):
     rejected     — operator said no; cluster fingerprint poison-flagged
     quarantined  — Sentinel scan flagged critical; held for review
     stale        — target live skill changed (hash mismatch) — must re-revise
```

**Transition matrix:**

| From → | candidate | staged | active | rejected | quarantined | stale | deprecated |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| (new write) | ✓ (Forge) | ✓ (`memclaw_doc`, admin) | ✓ (admin only) | — | — | — | — |
| candidate | — | auto-gate pass | — | — | scan critical | — | — |
| staged | — | — | HITL Approve | HITL Reject | scan rerun fail | live target changed | — |
| active | — | — | — | — | — | — | superseded / utilization drop |
| rejected | — | — | — | — | — | — | — |
| quarantined | — | manual recheck | manual override | manual delete | — | — | — |
| stale | — | revise+re-bind | — | — | — | — | — |

**Auto-gates** (candidate → staged):

| Gate | Threshold | Source |
|---|---|---|
| Volume | ≥ 10 successful executions (≥3 for early demo via `org_settings.forge.min_cluster_size`) | session-trace store |
| Diversity | ≥ 3 distinct agents (anti-poison) | session-trace store |
| Convergence | step-order variance below threshold across last K traces | distill pre-pass |
| Freshness | latest trace within 14 days (configurable) | timestamps |
| Coverage | matches ≥ 1 task pattern in last 30d of fleet recall queries | recall log |
| Scan | Sentinel reports `critical == 0` | scanner |

Patterns failing any gate stay `candidate` and keep accruing signal silently.

---

## 6. Outcome inference — the 6 signals

No `memclaw_evolve` requirement. Each signal is mined from data MemClaw already writes.

| # | Signal | Source (existing code) | Indicates |
|---|---|---|---|
| 1 | Contradiction event | `contradiction_detector.detect_contradictions_async` (Path A + Path C) | Recalled memory contradicted soon after = recalled answer was wrong |
| 2 | Supersession event | memory `supersedes_id` / `superseded_by` columns | Original was wrong / stale |
| 3 | Repeat-recall on same query | recall log + query similarity | First answer didn't land |
| 4 | Session terminal memory | content classifier on last memory in session (`shipped`/`fixed` vs `blocked`/`abandoned`) | Success or failure label |
| 5 | Cross-agent reuse depth | recall counter on memory × agent | Load-bearing memory; promotes to skill candidate |
| 6 | External hooks | git commits / PR merges / CI pass-fail tied to `run_id` | Ground-truth outcome (where available) |

**Optional soft-force**: `memclaw_recall` returns a `recall_id`; subsequent writes auto-attach it. No new tool, no new agent requirement. Lifts inference precision from ~75% (typical for passive) to ~95% (with attached recall_id) per our planning estimates — **internal eval harness in Phase 1 will measure the real number.**

---

## 7. Forge — the resident spec

A lifecycle operation in `lifecycle_publishers.py` (`publish_forge_distill_request`) + a worker:

```yaml
name:       forge
trigger:    cron (default every 6h; configurable)
            event:  on_evolve_outcome (debounced 15min — runs more often if outcomes are flowing)
scope:      tenant + fleet (one Forge worker per fleet)
trust_tier: 2 (cannot write scope_org or scope_agent skills; only scope_team)
budget:     llm_tokens_per_run = 50_000 (configurable)
            max_writes_per_run = 20
audit:      writes audit row per run (run_id, found_clusters, staged_count, rejected_count)
write_as:   author=resident:forge → tagged on every doc it writes
```

**Each Forge run:**

1. Pull outcome-labeled session-traces from the last `freshness_window_days` (default 14).
2. Cluster traces by goal + entity overlap (see §8 fingerprint).
3. For each cluster passing the volume + diversity + freshness gates: LLM-distill into a skill candidate doc (the schema in §3, status=`candidate`).
4. Compute cluster fingerprint; if a candidate with the same fingerprint already exists, *update its draft* (kind=update, bind to its hash) instead of minting a new candidate.
5. Trigger Sentinel scan.
6. Trigger convergence + coverage check.
7. If all gates pass → flip to `staged` (now visible in inbox).
8. Write audit row.

---

## 8. Cluster identity / fingerprint (Phase-1 prereq, the gating open question)

**Problem**: without canonical cluster identity, every Forge run mints near-duplicate candidates → inbox flood.

**Proposed design**: `fp:v1:<hash>` where `<hash> = sha256(canonical_form)` and `canonical_form` is the deterministic projection of the cluster:

```
canonical_form := sorted_set(
  goal_phrase_canonical,
  domain,
  top_5_entities_by_centrality_canonical,
  step_skeleton_canonical
)
```

- **`goal_phrase_canonical`**: LLM-extract a 6-word "what is this cluster about" phrase; lowercase + lemmatize.
- **`top_5_entities_by_centrality_canonical`**: entity IDs (already resolved by existing entity-resolution layer), sorted ascending.
- **`step_skeleton_canonical`**: ordered list of step intents (verbs + objects), each ≤ 4 words, lowercased + lemmatized.

**Stability test (must pass in Phase 1 eval)**: if Forge re-runs against the same lake state, the same clusters must produce the same fingerprint with ≥ 99% rate. If new traces arrive that should *extend* an existing cluster, the fingerprint MUST stay stable — only the membership grows.

**Drift handling**: if a cluster mutates enough that the fingerprint *would* change (e.g., new dominant entity), the old fingerprint stays on the existing candidate (`status: stale`) and a *new* candidate is minted with the new fingerprint. Operator sees both in the inbox; can Reject one.

**Anti-poison**: rejected fingerprints are written to `forge_rejected_fingerprints` table; Forge won't re-propose the same fingerprint for `rejection_cooloff_days` (default 30, configurable).

---

## 9. Sentinel scanner — pre-screen integration (pulled into MVP)

Wraps OpenClaw-style scan: every skill doc body is scanned before it can transition out of `candidate`.

| Check | Action on hit |
|---|---|
| Prompt-injection markers (e.g., `Ignore previous instructions`, `system: …`, hijack patterns) | Quarantine + critical+1 |
| Shell-injection in `support_files/scripts/*` | Quarantine + critical+1 |
| URL exfiltration patterns (e.g., suspicious data POSTs in scripts) | Quarantine + warn+1 |
| Path violations in `support_files` (absolute, traversal, hidden, executable, non-UTF8) | Reject the write (422) |
| PII in `content` or `evidence` | Warn+1; redact-on-display flag set |
| Memory-id stuffing (more than 20 unique cited memories) | Warn+1; capped at 20 on render |
| Body size > `maxSkillBytes` (default 40 000) | Reject the write (422) |
| Description size > `descriptionMaxBytes` (default 160) | Reject the write (422) |

Scan reruns on `apply` (catching cases where the lake state changed between `staged` and apply).

---

## 10. HITL Skills Inbox

Filtered view of `skills` collection where `status='staged'` AND `source IN ('forge', 'agent')`.

**Card displays:**
- LLM-named title + `description` (the trigger sentence)
- `domain` + `tags` chips
- `content` rendered as readable markdown (collapsible)
- **Provenance**: `evidence` paragraph + `goal` sentence + cited memory IDs (clickable)
- **Replay strip**: top-3 session traces (collapsed by default; expand for full memory chain)
- **Sentinel scan summary**: green/yellow/red badge + finding count
- **Hash-binding state**: if `kind='update'`, badge showing target version + stale-or-current
- **Cluster fingerprint** (advanced): copyable, expandable diagnostic

**Actions (one click each):**
- ✅ Approve — `staged → active`; triggers install pipeline
- ✏️ Edit — opens markdown editor on `content`/`description`/`summary`; saves a revision; scan reruns
- ❌ Reject — `staged → rejected`; cluster fingerprint poison-flagged
- 🛑 Quarantine — `staged → quarantined`; for security review
- ⏸️ Defer — leaves in inbox; Forge can revise on next run

**Cross-fleet (`scope_org`) skills:** require dual approval (two distinct admin users; Phase 5).

---

## 11. Harness install — the `active` transition

On `staged → active`:

| Target | Path | Notes |
|---|---|---|
| OpenClaw plugin | already-existing reconciliation in `core-api/routes/plugin.py` | Just becomes lifecycle-aware: filter by `status='active'`. |
| Direct-MCP (Claude Code / Codex via MCP) | `static/skills/memclaw/<slug>/SKILL.md` | New emitter. Strips `status`/`version`/`date` from frontmatter on apply (OpenClaw convention). |
| Claude Code direct install | `~/.claude/skills/<slug>/SKILL.md` | New CLI/MCP tool (`memclaw skill install <slug>`) writes locally with rollback metadata. |
| OpenClaw workspace PROPOSAL.md emission | `<workspace>/skills/<slug>/PROPOSAL.md` | Phase 5. Forge skills land as proposals in the OpenClaw Skill Workshop — review + apply happens in OpenClaw's UI. |

**Rollback metadata** (borrowed from OpenClaw) written before any apply mutates a target file:

```jsonc
{
  "schema":             "memclaw.skill-factory.rollback.v1",
  "skill_slug":         "deploy-eu-west-dns",
  "written_at":         "2026-05-12T15:00:00Z",
  "target_path":        "~/.claude/skills/deploy-eu-west-dns/SKILL.md",
  "action":             "create" | "update",
  "previous_content_hash": "sha256:…" | null,
  "previous_content":   "<full bytes>" | null,
  "support_files":      [ /* same shape */ ]
}
```

Stored as a separate doc in collection `skills_rollback` (new collection, simple), one entry per apply. One-click revert in the UI restores from this.

**Frontmatter strip-on-apply** (OpenClaw convention):

```yaml
# PROPOSAL.md (staged)         # SKILL.md (active)
---                             ---
name: …                         name: …
description: …                  description: …
status: proposal      ──strip──▶
version: v1           ──strip──▶
date: 2026-05-12      ──strip──▶
---                             ---
```

---

## 12. Caps + configurability

All defaults; tenant-overridable via `org_settings.skills_factory.*`:

| Setting | Default | Where it applies |
|---|---|---|
| `description_max_bytes` | 160 | Write validation (`memclaw_doc` skills writes + Forge distill) |
| `body_max_bytes` | 40 000 | Same |
| `inbox_max_pending` | 50 | Auto-defer oldest beyond cap |
| `forge.cron_interval_hours` | 6 | Forge schedule |
| `forge.min_cluster_size` | 3 (3 for demo / 10 for prod default) | Volume gate |
| `forge.min_distinct_agents` | 3 | Diversity gate |
| `forge.freshness_window_days` | 14 | Cluster member window |
| `forge.llm_tokens_per_run` | 50 000 | Budget |
| `forge.max_writes_per_run` | 20 | Per-run write cap |
| `sentinel.fail_on_critical` | true | Hard-quarantine on critical |
| `rejection_cooloff_days` | 30 | Cluster fingerprint poison lifetime |
| `openclaw_bridge.enabled` | false | Phase 5 emitter on/off |

---

## 13. OpenClaw bridge (Phase 5, complexity-gated)

Decision: ship only if it stays ≤ 1 engineer-week. Drop if scope creeps.

Spec:
- Reuses Forge candidate at `status: staged`.
- New emitter writes `<workspace>/skills/<slug>/PROPOSAL.md` + manifest entry compatible with OpenClaw's `SKILL_WORKSHOP_MANIFEST_SCHEMA`.
- Frontmatter follows OpenClaw's `name/description/status: proposal/version/date`.
- Support files emitted under `assets/examples/references/scripts/templates/` only.
- **Bidirectional outcome:** if user applies in OpenClaw, OpenClaw's manifest update fires a webhook to MemClaw → flip MemClaw status `staged → active`. (Webhook is optional; skip on v0; user can manually mark applied.)
- Gated by `openclaw_bridge.enabled = true`.

This is the "killer integration": **Forge mines your fleet, drops PROPOSAL.md straight into your OpenClaw workspace, the user reviews in OpenClaw's Control UI, applies.** They get an upstream; we get a downstream. Same plumbing benefits both.

---

## 14. OSS vs Enterprise split

| Capability | OSS | Enterprise |
|---|:-:|:-:|
| Resident framework substrate (`publish_forge_distill_request`) | ✓ | ✓ |
| Forge worker + cluster fingerprint + distill | ✓ | ✓ |
| Sentinel scanner | ✓ | ✓ |
| Skills Inbox dashboard + HITL actions | ✓ | ✓ |
| Install to Claude Code / OpenClaw / Direct-MCP | ✓ | ✓ |
| Outcome inference + v2 update proposals | ✓ | ✓ |
| Hash-binding, rollback, stale state | ✓ | ✓ |
| Configurable caps (per-tenant settings) | ✓ | ✓ |
| **Cross-fleet skill sharing** | — | ✓ |
| **Cross-tenant skill marketplace (anonymized)** | — | ✓ |
| **Auto-promote tier (skip HITL on gold)** | — | ✓ |
| **`scope_org` dual-approval governance** | — | ✓ |
| **Federated antibodies (Sentinel cross-tenant signal)** | — | ✓ |
| **Hosted Forge / platform-paid tokens** | — | ✓ |
| **OpenClaw PROPOSAL.md emitter** | ✓ | ✓ |

LLM token cost in OSS = on the user's own provider keys (existing MemClaw pattern). `forge.llm_tokens_per_run` knob is conservative by default.

---

## 15. Phased delivery

### Phase 0 — Foundations (everything else blocks on this)

**Scope:**
- Migration: add `source` and `status` (+ all new schema fields) to all existing `skills` docs in the DB. Existing manual docs → `source: manual, status: active`. Bulk-imported pointer-only docs → `source: imported, status: active`.
- Extend `memclaw_doc` skills-collection writes with the 7 adjustments in §4.
- Build `org_settings.skills_factory.*` config plumbing.
- Add `publish_forge_distill_request` to `lifecycle_publishers.py`; wire a no-op handler (just logs).
- New collection `skills_rollback` (for rollback metadata).
- New table `forge_rejected_fingerprints` (for poison memory).
- New table `session_traces` (for outcome-labeled traces; populated in Phase 1).

**Prereqs:** none (after rebases).
**Deliverable:** an admin can write a `source:agent, status:staged` skill via `memclaw_doc`, it lands in the Inbox-shaped query, but no UI yet.
**Acceptance:**
- `pytest tests/test_skill_schema_v1.py` passes (new file)
- existing eToro 1,402 docs still readable via `memclaw_doc` op=read after migration
- `memclaw_doc` `op=write` against `skills` with `description` > 160 bytes returns 422
- `memclaw_doc` `op=write` against `skills` without `name`/`slug`/`description`/`domain` returns 422
**Estimated agents:** 1-2 backend engineers · ~3-4 days

### Phase 1 — Outcome inference + Forge dry-run (parallel internal track)

**Scope:**
- Implement the 6 signal extractors → write `session_traces` rows.
- Session-trace builder: group memories by `run_id` (already exists on the memory write signature) + agent + time-window. Label each trace with success/failure inferred from signals.
- Cluster fingerprint (§8) — including stability test.
- Forge service: pull traces → cluster → fingerprint → distill (LLM call) → write `candidate` docs.
- **Eval harness**: hand-label 3 fleets' worth of session traces; measure (a) fingerprint stability ≥ 99%, (b) cluster precision ≥ 70%, (c) cluster recall ≥ 60%.
- Forge runs only as a manual CLI invocation; candidates never escape `candidate` state in this phase.

**Prereqs:** Phase 0.
**Deliverable:** `memclawctl forge dry-run --tenant=… --fleet=…` produces N candidate docs in DB. Internal eval reports precision/recall.
**Acceptance:**
- Stability test passes ≥ 99%
- Precision ≥ 70% on labeled set; recall ≥ 60%
- All candidates have `status=candidate, source=forge, cluster_fingerprint=fp:v1:…`
- No candidate is `staged` or visible in any user surface
**Estimated agents:** 2 backend + 1 ML/eval · ~2-3 weeks (longest phase; cluster fingerprint is hard)

### Phase 2 — HITL Skills Inbox + Sentinel scanner

**Scope:**
- Sentinel scanner (§9), wired in front of `staged` transition.
- Auto-gates evaluator: candidate → staged when all 6 gates pass.
- Skills Inbox UI (frontend): filtered list view + card detail view + 5 actions (Approve/Edit/Reject/Quarantine/Defer).
- Edit inline = markdown editor on `content`, `description`, `summary` + scan re-trigger.
- Reject flow writes to `forge_rejected_fingerprints`.

**Prereqs:** Phase 1 producing candidates; Phase 0 schema fields.
**Deliverable:** an operator can land on `/skills-inbox`, see staged Forge candidates, and approve one. Approve flips to `active` but doesn't install yet (Phase 3 wires the install path; until then, plugin reconciliation alone serves it).
**Acceptance:**
- Approve → `status: active` and the doc surfaces in `op=search` / plugin reconciliation
- Reject → fingerprint written to poison table; Forge re-run does NOT propose the same fingerprint
- Scan critical → auto-quarantine; visible in a separate filter
- Edit + save → new `content_hash`, scan rerun, stays `staged`
**Estimated agents:** 1 backend + 2 frontend · ~2 weeks

### Phase 3 — Harness install (Claude Code path + rollback)

**Scope:**
- New emitter: `static/skills/memclaw/<slug>/SKILL.md` — strip frontmatter on apply.
- New CLI/MCP: `memclaw skill install <slug> --target claude-code` writes `~/.claude/skills/<slug>/SKILL.md`.
- Rollback metadata writer + new `skills_rollback` collection writes.
- Path-safe support file emission (`assets/examples/references/scripts/templates`).
- One-click revert in the UI.

**Prereqs:** Phase 2 (Approve must land in `active`).
**Deliverable:** approving in the Inbox → Claude Code user sees a new skill they can invoke immediately.
**Acceptance:**
- Approving the canonical worked-example skill installs to local Claude Code dir
- Frontmatter strip works (no `status`/`version`/`date` in installed SKILL.md)
- Support file path violation rejected (`../`, absolute, hidden, executable, non-UTF8)
- Rollback metadata exists; one-click revert restores prior state byte-for-byte
**Estimated agents:** 1 backend + 1 frontend · ~1-2 weeks

### Phase 4 — Outcome loop closes (v2 + hash-binding + telemetry)

**Scope:**
- Skill fire telemetry: every harness invocation reports back (Claude Code skill triggers + OpenClaw plugin fires) via existing audit hooks → updates `telemetry.fires_*` fields.
- Utilization computation (rolling 30d).
- Forge `kind: update` proposals: when (a) telemetry drifts past threshold, OR (b) Forge re-mining finds a divergent cluster → produce a v2 candidate bound to v1's hash.
- Stale detection: if a target skill changes between `staged` and apply → flip `staged → stale`; require re-revise.
- Deprecation flow: utilization drop > threshold → Forge proposes deprecate diff card.

**Prereqs:** Phase 3 installed skills firing.
**Deliverable:** 30 days post-install, a v2 diff card or deprecate proposal lands in the inbox automatically.
**Acceptance:**
- Telemetry rows present per skill after first 100 fires
- Mock-drift scenario produces a v2 card
- Manual edit of installed skill between staged + apply → stale flag fires
**Estimated agents:** 2 backend · ~2 weeks

### Phase 5 — Polish + integrations

**Scope:**
- Auto-promote tier with 7-day rollback window.
- `scope_org` dual-approval governance.
- Concierge primitive (custom resident definition surface).
- **OpenClaw PROPOSAL.md emitter** (§13).
- Tenant-level caps configurability surface (settings UI).

**Prereqs:** Phase 4.
**Acceptance:** OpenClaw bridge demo: Forge candidate lands as PROPOSAL.md in an OpenClaw workspace; user applies in OpenClaw's Control UI; webhook (or manual) flips MemClaw to `active`.
**Estimated agents:** 2 backend + 1 frontend · ~2-3 weeks

---

## 16. Tasks (issuable today, just for Phase 0)

| ID | Task | Owner | Estimate |
|---|---|---|---|
| SF-001 | Migration script: backfill `source`, `status` on all existing `skills` docs (per §3 backwards-compat rules) | backend | 1d |
| SF-002 | Extend `routes/documents.py` skills writes: 7 adjustments in §4 (schema validator + caps + defaulting + scan trigger + hash-binding for `kind=update`) | backend | 2d |
| SF-003 | New table: `forge_rejected_fingerprints` (Alembic migration) | backend | 0.5d |
| SF-004 | New table: `session_traces` (Alembic migration) | backend | 0.5d |
| SF-005 | New collection-as-table affordance: `skills_rollback` (just docs; reuses `documents`) | backend | 0.25d |
| SF-006 | Org settings plumbing: `org_settings.skills_factory.*` JSON read/write + defaults | backend | 1d |
| SF-007 | Lifecycle publisher stub: `publish_forge_distill_request` + no-op handler | backend | 0.25d |
| SF-008 | Unit tests: `test_skill_schema_v1.py` covering acceptance criteria | backend | 1d |
| SF-009 | Documentation: this file + `8-skill-factory-architecture.svg` (DONE here) | docs | 0d |
| SF-010 | Re-log this plan to MemClaw under tenant `arkash24-4d270c` / fleet `caura-dev-fleet` | tooling | 0.25d |

Phase 1 tasks issued after Phase 0 lands.

---

## 17. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Garbage-in skills (noisy outcome inference → bad candidates → trust collapse) | High gating thresholds at launch; surface confidence prominently; never auto-promote in v1; eval harness in Phase 1 must pass before public exposure. |
| Inbox flood (without fingerprint stability → near-duplicates) | Fingerprint design + stability test is Phase-1 prereq; rejected fingerprints are poison-flagged for 30d. |
| Silent harness failure (installed skill triggers wrongly) | Every install ships with telemetry hook + one-click revert via rollback metadata. |
| Prompt-injection via mined memories | Sentinel scanner in MVP (Phase 2); HITL editor surfaces raw quotes. |
| PII bleed into skill bodies | Skills inherit PII redaction from parent memories; scanner re-checks at apply time. |
| Cold-start demo problem | Synthetic-fleet seed + "warmth meter" UI ("Forge is observing — 7/10 outcomes needed for first candidate"). |
| eToro pointer-only skills surprise our schema | `data.content IS NULL` guard everywhere; migration tags them `source: imported`. |
| OpenClaw releases a competing fleet-mining feature | Forge's outcome inference layer + cluster fingerprint are the moat; document and gather feedback fast. |

---

## 18. Open questions remaining (gated per phase)

| # | Question | Resolved by | Phase |
|---|---|---|---|
| OQ-1 | Verify which jsonb field drives skills-collection embedding (`description` vs `summary`) | Reading `core_api/services/doc_indexing.py` | Phase 0 |
| OQ-2 | `doc_id` namespacing collision risk between `forge/<slug>` and eToro bulk imports | Phase 0 migration check | Phase 0 |
| OQ-3 | Cluster fingerprint formula — stability test must hit ≥ 99% before Phase 1 ships | Eval harness | Phase 1 |
| OQ-4 | Forge cron interval default — 6h vs 12h vs on-event-debounced | Eval harness signal | Phase 1 |
| OQ-5 | Edit-inline UX: full markdown WYSIWYG or raw-markdown only | Design call | Phase 2 |
| OQ-6 | Claude Code description-quality benchmark (does the LLM-generated description trigger the skill correctly?) | Dedicated trigger-accuracy harness | Phase 3 |
| OQ-7 | OpenClaw webhook protocol — pull-based polling vs push-based webhook | OpenClaw discussion | Phase 5 |

---

## 19. Validation checklist (re-run before finalizing)

- [x] **Decision: Option B (full content)** — `content` field in schema (§3); flat-procedure MVP (§1 non-goal); content shown in inbox card (§10).
- [x] **Decision: no extra MCP tool** — `memclaw_doc` adjustments in §4 + §15 Phase 0 task SF-002.
- [x] **Decision: OpenClaw bridge if not too complex** — §13 explicitly gated at ≤ 1 engineer-week; default `openclaw_bridge.enabled=false` (§12).
- [x] **Decision: 160-byte description default but configurable** — §3 schema + §12 caps table + §15 SF-002 acceptance criterion.
- [x] **Reuse existing primitives** — components table §2 lists all 9 existing pieces with file paths.
- [x] **Same `skills` collection + `source` discriminator** — §3, §11 plugin path filters by status.
- [x] **Session-bounded clusters, ≥3 sessions / ≥2 agents** — §5 auto-gates table, §12 config defaults.
- [x] **Cluster fingerprint design proposed** — §8 with stability acceptance criterion in Phase 1.
- [x] **Outcome inference passive (no `memclaw_evolve` required)** — §6 with 6 signals + their existing code sources.
- [x] **Sentinel scanner pulled into MVP** — §9 + Phase 2 deliverable.
- [x] **Hash-binding + stale state from OpenClaw** — §3 `target.target_content_hash`, §5 lifecycle, Phase 4 stale detection.
- [x] **Rollback metadata from OpenClaw** — §11 schema + Phase 3 deliverable.
- [x] **Frontmatter strip-on-apply convention** — §11 diagram + Phase 3 acceptance.
- [x] **Caps configurable per tenant** — §12 complete defaults table.
- [x] **OSS vs Enterprise split preserved** — §14 unchanged from prior plan.
- [x] **Risks updated** — §17 includes new ones (eToro shape surprise, OpenClaw competition).
- [x] **No new MCP tool surface introduced** — entire plan reviewed.
- [x] **Plan can be implemented by an agents team in parallel where possible** — phase dependencies are clear; Phase 0 sequential; Phases 1 & 2 can begin in parallel as long as Phase 1 doesn't surface candidates to UI until Phase 2 ships.

---

## 20. File locations (quick reference)

```
caura-memclaw/
├── alembic/versions/
│   ├── XXXX__add_skill_factory_fields.py            # schema migration (Phase 0)
│   ├── XXXX__add_session_traces.py                  # Phase 0
│   └── XXXX__add_forge_rejected_fingerprints.py     # Phase 0
├── common/events/
│   └── lifecycle_publishers.py                       # ADD: publish_forge_distill_request (Phase 0)
├── core-api/src/core_api/
│   ├── routes/
│   │   └── documents.py                              # EDIT: skills writes adjustments (Phase 0)
│   ├── services/
│   │   ├── outcome_inference/                        # NEW dir (Phase 1)
│   │   │   ├── __init__.py
│   │   │   ├── contradictions.py
│   │   │   ├── supersessions.py
│   │   │   ├── repeat_recall.py
│   │   │   ├── terminal_memory.py
│   │   │   ├── cross_agent_reuse.py
│   │   │   └── external_hooks.py
│   │   ├── session_trace.py                          # NEW (Phase 1)
│   │   ├── forge/                                    # NEW dir
│   │   │   ├── __init__.py
│   │   │   ├── forge_service.py                      # Phase 1
│   │   │   ├── fingerprint.py                        # Phase 1
│   │   │   ├── distill_prompt.py                     # Phase 1
│   │   │   ├── sentinel_scan.py                      # Phase 2
│   │   │   ├── harness_install.py                    # Phase 3
│   │   │   └── openclaw_bridge.py                    # Phase 5
│   │   ├── skill_lifecycle.py                        # NEW (Phase 0 stubs, Phase 2 logic)
│   │   └── organization_settings.py                  # EDIT: add skills_factory.* (Phase 0)
│   └── routes/
│       └── plugin.py                                 # EDIT: filter by status=active (Phase 2)
├── frontend/
│   └── (skills-inbox view + cards)                   # Phase 2
├── static/skills/memclaw/                            # EDIT: per-slug emit (Phase 3)
└── tests/
    ├── test_skill_schema_v1.py                       # Phase 0
    ├── test_session_trace_builder.py                 # Phase 1
    ├── test_cluster_fingerprint_stability.py         # Phase 1 (acceptance)
    ├── test_forge_dry_run.py                         # Phase 1
    ├── test_sentinel_scanner.py                      # Phase 2
    ├── test_skill_lifecycle_transitions.py           # Phase 2
    ├── test_harness_install_rollback.py              # Phase 3
    └── test_outcome_loop_v2_proposals.py             # Phase 4
```

---

## Approvals

- [ ] **Ran** — green light to begin Phase 0 (SF-001 … SF-010)
- [ ] Re-log to MemClaw under correct tenant (SF-010 — runs after green light)
