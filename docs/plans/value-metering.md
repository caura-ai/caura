# Value metering: the work memory saves

Status: in progress (Erni + Claude), stacked on `CAURA-000-usage-metering`.
Done: A, B, C (plugin and SDKs), D (Value page; per-operation spend moved to
caura-ops › Model Spend), E. Open: C (Claude Code / Codex hook, after this ships:
it needs `origin` on the broker's frozen bulk contract).

## Why

Usage metering counts what Caura's own model calls cost. That is our cost, not
the customer's value: nobody running agents wants to read about entity
extraction. What a customer wants to know is what the memory did for their
agents:

- **Work reused.** A memory took a session N minutes, T tokens and K turns to
  work out. Another session got it in one recall.
- **Sessions helped.** How many agent sessions started with memory that was
  relevant.
- **Knowledge shared.** What one agent or person learned, used by others.
- **Mistakes prevented.** Contradictions caught, stale facts kept out of
  recall, secrets blocked, risky writes held.
- **Caura's cost.** One line, from usage metering.

## Rules (the numbers must survive "how did you get that?")

1. Count a memory **once per new session** that recalls it. Ten recalls in
   one session are one reuse; a session recalling its own memory is none.
2. "Work reused" is an **upper bound** and is labelled so: a recall does not
   always mean the work would otherwise have been redone in full. A confirmed
   version counts only reuse in sessions that reported success (Evolve).
3. A number we did not observe is not shown. Tokens a model "reports" about
   itself are not observations; tokens a hook, plugin or SDK read from the
   provider's response are.
4. Time saved per task needs a with/without comparison we cannot observe, so
   it is not claimed. Minutes of origin work reused are observed.

## Where each signal comes from

| Signal | MCP (every agent) | Hooks / OpenClaw plugin | SDK / REST |
|---|---|---|---|
| Session identity | Server issues `Mcp-Session-Id` (stateless: a label, no server state) | Broker / plugin session id | `X-Caura-Session-Id` header or `metadata.session_id` (the broker's existing convention) |
| Origin time | Session start / previous write → this write | Hook timestamps | Same, from the session's first call |
| Origin turns | Calls to Caura in the stretch (lower bound) | Real chat turns | Caller-supplied |
| Origin tokens | Not observable | Transcript usage (Claude Code, Codex), `afterTurn` usage (plugin) | Caller's own provider usage |

## Phases

**A. Sessions and origin (OSS core) — now**

- A1. MCP issues a session id on a response to a request that had none, and
  reads it back on later requests. Stateless stays stateless. REST/SDK send
  `X-Caura-Session-Id`. Both land in one ContextVar; a write stamps
  `metadata.session_id` when the caller did not.
- A2. `agent_sessions` (migration 067): tenant, session id, agent, source,
  first seen, last seen, calls. One insert when a session is first seen; last
  seen and calls updated at most once a minute per session (buffered, like
  usage metering).
- A3. Origin on write: `origin_seconds` = this write minus the later of the
  session's start and its previous write; `origin_calls` = calls to Caura in
  that stretch; optional `origin` from a trusted reporter (hook, plugin, SDK:
  tokens in/out, turns, source). Stored in the reserved `_system.origin`.
  The MCP tool schema does not expose `origin`: a model cannot report it.
- A4. Reuse on recall: one `memory_reuse` row per (session, memory) the first
  time a session recalls a memory from another session, carrying the origin
  figures and writer/reader agents. Insert-or-ignore makes rule 1 structural.

**B. Value endpoint (OSS core)**

- `GET /api/v1/value`: sessions, sessions helped, work reused (minutes,
  calls, reported tokens; with how many memories had reported tokens),
  knowledge shared (cross-agent reuse, top teachers and learners), mistakes
  prevented from tables Caura already has (conflicts recorded, superseded
  memories kept out of recall).

**C. Token origin from beside the agent**

- OpenClaw plugin: send `origin` from `afterTurn` usage since the last write.
- Lightweight Claude Code / Codex hook (installed by `caura setup`, no daemon
  needed): sum transcript usage since the session's last write.
- SDK: document `origin`.

**D. Customer UI (enterprise)**

- Insights › Value replaces the per-operation Spend tables, which move to
  caura-ops as our internal cost view. Caura's cost stays as one line.
- Mistakes prevented adds the enterprise-only counts (security findings
  blocked, writes held, sessions on current rules).

**E. Confirmed reuse**

- Link Evolve outcome reports to the recalls in their session.

## Not doing

- Model-reported token counts.
- "Saved versus sending every memory."
- Hours saved per task without a measured comparison.
