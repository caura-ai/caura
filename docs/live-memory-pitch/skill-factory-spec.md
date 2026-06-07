# Skill Factory — Spec (high-level)

**Status:** approved for implementation.
**Owner resident:** Forge.
**One-line:** the fleet writes the playbooks nobody had time to write — humans bless them with one click — they run in the harness — outcomes flow back.

---

## 1. Definition of "shipped"

A tenant connects MemClaw and runs agents normally. Then:

1. Without anyone calling a special tool, **candidate skills accumulate in a Skills Inbox.**
2. **One click approves.** One more **installs** to the harness (Claude Code first).
3. Installed skills produce outcomes that **flow back**; v2 is proposed when branch performance drifts.
4. The tenant writes **zero new agent code**.

**Magic property to preserve:** *the only thing the user ever does is write memories normally and click Approve.*

---

## 2. The pipeline (7 lake-side stages)

| Stage | Verb | Owner | Visibility |
|---|---|---|---|
| 1 · Observe | watch | Forge | silent |
| 2 · Infer outcomes | label | passive signal layer | silent |
| 3 · Cluster | group | Forge | silent |
| 4 · Distill | compile | Forge (LLM) | silent |
| 5 · Gate → HITL | judge | auto-gates → Skills Inbox | **user moment** |
| 6 · Install | emit | harness adapter | one click |
| 7 · Re-mine | refine | Forge (continuous) | silent → diff card |

---

## 3. Outcome inference signals (no `memclaw_evolve` required)

The memory stream **is** the feedback signal. Six free signals mined passively:

1. **Contradiction event** — recalled memory contradicted soon after = failed.
2. **Supersession event** — superseded memories were wrong.
3. **Repeat-recall** on same query = first answer didn't land.
4. **Session terminal memory** — "shipped"/"fixed" vs "blocked"/"abandoning". LLM-classifiable.
5. **Cross-agent reuse depth** — recalled by N agents with healthy terminal states = load-bearing.
6. **External hooks** — git commits, PR merges, CI pass/fail tied to `session_id` when available.

Optional soft-force layer: `memclaw_recall` returns a `recall_id`; subsequent writes auto-attach it. No new tool, no new requirement.

---

## 4. Decisions made

| # | Decision | Choice | Why |
|---|---|---|---|
| 4.1 | What is a "procedure"? | **Session-bounded trace.** Each cluster member = one session. | Sessions have natural start/end and a terminal memory. Cross-session abstraction punted. |
| 4.2 | How big must a cluster be to graduate? | **≥3 sessions across ≥2 distinct agents.** | Single-session ≠ skill, it's a transcript. Anti-poison story only kicks in with multi-trace clustering. |
| 4.3 | Skill data model | **Dual: registry artifact + lake biography.** Memory `type=skill` for governance/audit/provenance/lifecycle; registry for harness emission, fast queries, high-frequency telemetry. | Registry holds the artifact; lake holds its biography. Memory-only fallback acceptable for MVP. |
| 4.4 | Branches in the procedure | **Flat MVP.** Ordered steps + overall success rate. | Step-level cluster alignment is hard. Branches arrive when per-cluster volume justifies them (Phase 4). |
| 4.5 | OSS vs Enterprise | **Single-fleet magic in OSS · multi-fleet leverage in Enterprise.** | Adoption beats moat. Killer feature must be in OSS or the OSS story dies. See `feature-priorities.md`. |

---

## 5. Auto-gates (candidate → staged)

A cluster only graduates when **all five** hold:

| Gate | Threshold |
|---|---|
| Volume | ≥10 successful executions (≥3 acceptable for early demo) |
| Diversity | ≥3 distinct agents (anti-poison) |
| Convergence | branch / step variance below threshold across last K runs |
| Freshness | observed within last 14d |
| Coverage | preconditions match real upcoming task patterns |

Patterns failing any gate stay as `candidate` and keep accruing signal silently.

---

## 6. Lifecycle

```
candidate → draft → staged → active → deprecated
                     ▲          │
                     │          ▼ (via re-mine)
                  HITL       v2 diff card
```

- `staged` is the first user-visible state (Skills Inbox).
- Reject **poison-flags the cluster pattern** so it won't re-surface.
- `scope_org` (cross-fleet) skills require **dual approval**.
- Optional auto-promote tier (mature tenants only): gold candidates (≥50 exec, ≥90% success, ≥5 agents) skip HITL with **7-day rollback window**.

---

## 7. Skills Inbox card (HITL surface)

The card is the entire HITL UX — no separate review console.

**Card displays:**
- LLM-named title + one-line "when to use this"
- Procedure rendered as readable markdown
- **Provenance trail** — every step links to source memories with author + outcome
- **Replay strip** — 2–3 actual session traces (collapsed by default)
- **Branch ledger** — success rate per branch + sample size

**Actions (one click each):** Approve · Edit inline · Split · Merge · Reject · Defer.

---

## 8. Harness installation targets

| Target | Output |
|---|---|
| Claude Code | `~/.claude/skills/<name>/SKILL.md` with tuned `description` frontmatter |
| OpenClaw | plugin slot (existing MemClaw plugin) |
| Cursor / Windsurf | equivalent skill files |
| Generic MCP | `memclaw_skill_<name>` tool |

**Critical:** description quality governs trigger accuracy. Bad descriptions = silent product failure. A `skill-creator`-style benchmark + prompt-tuning loop must exist *before* Phase 3 ships.

---

## 9. Phased delivery

| Phase | Scope | User sees |
|---|---|---|
| **0 · Foundations** | Outcome inference primitives + Resident framework. | nothing |
| **1 · Forge dry-run** | Forge produces candidates internally; eval harness measures precision/recall on hand-labeled fleets. | nothing |
| **2 · Skills Inbox** | Dashboard card UI lands. Approve / Edit / Reject / Defer. | the inbox |
| **3 · Harness install** | One-click install to Claude Code (+ OpenClaw same time). Description-quality benchmark must be passing. | install button |
| **4 · Outcome loop closes** | Installed-skill outcomes feed Forge. v2 diff cards. Versioning, deprecation, rollback. | v2 cards |
| **5 · Polish** | Auto-promote tier (7-day rollback) · `scope_org` dual-approval · Concierge primitive. | settings |

---

## 10. Risks + mitigations

| Risk | Mitigation |
|---|---|
| Garbage-in skills (noisy outcome inference → bad candidates → trust collapse) | Aggressive gating thresholds at launch; surface confidence prominently; **never auto-promote in v1**. |
| Inbox flood (no cluster fingerprint stability → near-duplicate candidates every Forge run) | **Cluster fingerprint design is a Phase-1 prerequisite, not a follow-up.** |
| Silent harness failure (skill triggers wrongly) | Every install ships with a "did this fire correctly?" telemetry hook + one-click revert. |
| Prompt-injection via memories (poisoned content in procedure body) | HITL editor surfaces raw quotes; **Sentinel pre-screens** candidates before they hit the inbox. |
| PII bleed | Skills inherit redaction status of their parent memories. |
| Cold-start demo problem (new fleets don't generate enough signal) | Synthetic-fleet demo mode + a **"warmth meter"** ("Forge is observing — 7 of 10 outcomes needed"). |

---

## 11. Canonical worked example

> A small platform fleet (Sasha, Mira, Kai). Nobody coordinates. Nobody calls `memclaw_evolve`.

| Date | Agent | Session memories |
|---|---|---|
| Apr 18 | Sasha | "Deploy v4 to eu-west — step 7 hung 6 min" → "tried fallback DNS resolver — step 7 completed" → "Shipped ✓" |
| Apr 22 | Mira | "Step 7 timeout again on eu-west" → "switched DNS resolver, retry passed" → "Shipped ✓" |
| Apr 25 | Kai | "Deploy to eu-west failing on step 7" → "retried, same hang" → "switched DNS resolver, passed" → "Shipped ✓" |
| Apr 28 | Sasha | "Deploying eu-west — using fallback DNS preemptively" → "Shipped ✓" |

**May 1, Forge wakes up.** Scans terminal memories. Clusters by topic + entity overlap. Finds: 4 sessions / 3 distinct agents / 4 success / freshness 3d / convergence good — **all 5 gates pass**.

**Distillation produces:**

```yaml
title:        "Deploy to eu-west · use fallback DNS at step 7"
when:         "deploying to eu-west region"
steps:
  1. Run deploy v4 normally up to step 7
  2. If step 7 hangs/timeouts → switch to fallback DNS resolver
  3. Continue deploy
success_rate: 4/4 (100%)
cites:        [mem-8821, mem-8932, mem-9011, mem-9134]
status:       staged
```

**A card lands in the Skills Inbox.** User clicks **Approve**, then **Install to Claude Code**.

**May 4 — Mira in Claude Code:** types *"deploy us to eu-west"* → skill description matches → procedure fires → "Shipped ✓". Outcome flows back. Counter ticks.

**One month later** — eu-west DNS fixed upstream. Step 7 stops hanging. Forge notices utilization dropped 78% → 9%. Pushes a **v2 diff card** recommending deprecate. User clicks Deprecate. Skill cold-archived; biography survives in the lake forever.

**Three things that matter:**
1. Nobody told the fleet anything — the procedure emerged from collision.
2. The user did exactly two clicks.
3. The skill knows where it came from — fully auditable trust.

---

## 12. Open questions (must resolve before code starts)

| # | Question | Why it gates |
|---|---|---|
| 12.1 | **Cluster identity / canonical fingerprint** — when Forge re-runs and finds an "almost the same" cluster, is it an update or a new candidate? | **Phase-1 prerequisite.** Without it the inbox floods with near-duplicates and becomes unusable. *This is the next deep-dive topic.* |
| 12.2 | Skill description prompt-tuning + benchmark | Phase-3 prerequisite. Bad descriptions = silent trigger failures. Need `skill-creator`-style eval loop. |
| 12.3 | Forge token budget defaults & opt-up UX | Unit economics. Answer likely: per-resident budget knob, conservative default, tenant opts up. |
| 12.4 | Empty-fleet UX ("warmth meter") copy + thresholds | Cold-start product feel. |

---

## 13. v0.1 — the first demo

Synthetic fleet seeded into a tenant + 7 simulated days of agent activity → opens Skills Inbox → sees one candidate ("Deploy in eu-west") with 23-memory provenance and 87% success rate → clicks **Approve** + **Install to Claude Code** → opens Claude Code → *"deploy us to eu-west"* → skill fires → outcomes flow back → 7 days later a v2 diff card lands.

Every box in the pipeline lit up, end-to-end. **That's the moment the deck stops being a deck.**
