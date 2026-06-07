# MemClaw Live Memory — Feature Priorities

All candidate features from the Live Memory brainstorm, ranked. Each is a *process the lake performs on itself* (a "resident") rather than a CRUD endpoint.

Unifying tagline: **most memory products are libraries — they get bigger. MemClaw is a metabolism — it gets smarter.**

---

## P0 — Committed

| # | Feature | Resident | One-line | Why it's first |
|---|---|---|---|---|
| 0 | **Resident framework** | (substrate) | Cron-driven, scope-bounded agent identity with audit + token budget. The primitive every other feature inhabits. | Prerequisite for everything below. Invisible to users. |
| 1 | **Skill Factory** | **Forge** | Passively mines outcome-tagged session clusters → distills branching procedures → HITL Skills Inbox → installs to harness (Claude Code first). | Approved. Headline ROI. Demo moment for both OSS and enterprise. |

---

## P1 — Next, after Skill Factory ships

| # | Feature | Resident | One-line | Differentiator |
|---|---|---|---|---|
| 2 | **Fusion** | **Dreamer** | Collides ≥2 memories that individually don't answer a question into a *new claim* nobody on the fleet had. | The 60-second viral demo. RAG vendors literally cannot do this — no fleet to collide. |
| 3 | **Dream Cycles** | **Dreamer** | Periodic offline pass: recombines, fuses, prunes weak links, strengthens load-bearing ones. Same volume in, *denser lake* out. | Reframes the product from database to organism. Ships with Fusion (same resident). |

---

## P2 — Planned

| # | Feature | Resident | One-line | Differentiator |
|---|---|---|---|---|
| 4 | **Hypothesis Engine** | **Oracle** | Erupts unprompted theories from aggregate patterns ("when flag X is on, latency spikes 3h later 73% of the time"). | The lake stops being passive infra and becomes a scientist. |
| 5 | **Memory Antibodies** | **Sentinel** | Detects poisoning / hallucinations / staleness; auto-generates inoculation memories. Each attack hardens the fleet. | Multi-tenant by definition. One tenant's antibody pattern (anonymized) hardens every other tenant's lake. Also pre-screens Skill Factory candidates. |
| 6 | **Cartography** | **Cartographer** | Auto-derived map of who/what knows what, real OKRs vs stated, contested beliefs, blind spots. | CTO-level standalone product. The *real* org chart, drawn by what survived in the lake. |

---

## P3 — Later

| # | Feature | Resident | One-line | Notes |
|---|---|---|---|---|
| 7 | **Counterfactual Replay** | (Dreamer-adjacent) | Inject a memory that exists *now* into a past failed session and replay. Quantifies $ value of each memory. | ROI proof tool. Doubles as the lake's self-evaluation harness. |
| 8 | **Adversarial Self-Play** | **Hunter** | Continuously generates queries the fleet *should* answer; surfaces the gaps. | The lake hunts its own coastline. |
| 9 | **Concierge** | (custom) | Tenant-defined custom resident — prompt + schedule + scope. Community-publishable. | Resident marketplace primitive. Extensibility story. |

---

## OSS vs Enterprise split

**Strategic bet:** adoption beats moat. Killer feature must be in OSS or the OSS story dies (Mem0 / Letta both ship OSS).

| Capability | OSS | Enterprise / Managed |
|---|:-:|:-:|
| Resident framework | ✓ | ✓ |
| Forge + Skills Inbox + HITL approval | ✓ | ✓ |
| Install to Claude Code / OpenClaw / MCP | ✓ | ✓ |
| Outcome inference + v2 proposals | ✓ | ✓ |
| Fusion + Dream Cycles | ✓ | ✓ |
| Oracle / Sentinel / Cartographer / Hunter | ✓ | ✓ |
| **Cross-fleet skill sharing** | — | ✓ |
| **Cross-tenant skill marketplace (anonymized)** | — | ✓ |
| **Auto-promote tier (skip HITL on gold candidates)** | — | ✓ |
| **`scope_org` dual-approval governance** | — | ✓ |
| **Federated antibodies (Sentinel cross-tenant)** | — | ✓ |
| **Hosted Forge / platform-paid tokens** | — | ✓ |

**Principle:** single-fleet magic is OSS · multi-fleet leverage is enterprise.
**LLM cost in OSS:** user's own provider keys (existing pattern). Per-resident token budget knob, conservative default.

---

## Suggested exec deck order

Lake → Library-vs-Metabolism → Residents → Residents-vs-Insights → Outcome-Inference → Fusion → Skill-Factory.

Sketches: `docs/live-memory-pitch/01..07-*.svg`
