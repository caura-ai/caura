# Correction propagation: tell every agent that read a memory when a person corrects it

Status: in progress. Done: CP-04 (this spec), CP-05 (`memory_exposure`,
migration 072), CP-06 (exposure recorded on every agent read), CP-07
(`GET /memories/{id}/exposure`). Next: the `caura.memory.corrected` event, the
notice fan-out, and the person's view of who confirmed.

## Problem

A person fixes a memory: edits its content, marks it outdated, withdraws a
verification. The memory is right now, but every agent that read the old
version still holds it, in its context, its notes, or work it built on it.
Nothing tells them. Today Caura cannot even say who they are: recall logging
is opt-in, off by default, kept 30 days, and does not see by-id reads.

Correction propagation closes that loop: Caura records every agent a memory
was handed to, and when a person corrects the memory it sends each of those
agents a notice and shows the person which agents confirmed. No LLM calls.

## Decisions

**D1. Who saw it: an always-on `memory_exposure` table.** Not recall-event
logging, which is opt-in, retention-bound and blind to by-id reads. One row
per (tenant, memory, agent), written on every read that returns a memory to an
agent:

- MCP `caura_recall`;
- REST `POST /search` (the plugin's per-turn auto-search) and `POST /recall`;
- get-by-id: REST `GET /memories/{id}` and MCP `caura_manage op=read`.

A chunk or an atomic fact counts for its parent (`parent_memory_id`): the
parent is what a person corrects, and the agent read part of it. A read by a
person, or by a credential that names no agent, records nothing. One batched
upsert per read, fire-and-forget after the response, as recall-event logging
is: no added latency, and a failure is logged, never raised. Rows live as
long as the memory (ON DELETE CASCADE) and go with a tenant purge.

**D2. What counts as a correction.** Person-initiated, or by an agent with
trust_level >= 3:

- a content edit (`PATCH /memories/{id}` that changes `content`);
- a status change to `outdated`, `cancelled` or `deleted`;
- `unverify`;
- a conflict resolution (`PATCH /conflicts/{id}/resolve`).

The contradiction detector's automatic supersede is out of v1: it is not a
person's decision, and it emits no audit today.

**D3. Sender.** A system sender, `caura:corrections`, on the Caura bus
(enterprise). It has its own quota bucket, so a correction fan-out does not
spend the tenant's agent send budget, and it may cross fleets within the
tenant (an agent of another fleet may have read the memory: cross-fleet recall
is allowed where cross-fleet sends are not).

**D4. The notice.** A bus message with `kind=request` and a 7-day reply
window, so the person sees per agent whether it acknowledged. An agent that is
not on the bus gets the notice as a pending notice on its next recall instead.
At most 50 agents per correction, most recent readers first. The person sees a
preview (the list of agents) before anything is sent.

## Data model

`memory_exposure` (migration 072, `common/models/memory_exposure.py`):

| Column | Type | Meaning |
|---|---|---|
| `tenant_id` | text, PK | The memory's tenant. |
| `memory_id` | uuid, PK, FK `memories.id` ON DELETE CASCADE | The memory, or the parent of the chunk that was read. |
| `agent_id` | text, PK | The reading agent, as the read named it. |
| `first_seen` | timestamptz | The earliest read. |
| `last_seen` | timestamptz | The latest read. |
| `count` | bigint, >= 1 | Reads that returned it. |
| `last_trace_id` | text, null | `session_context.server_trace_id()` of the latest read that had one: the MCP session, the REST `X-Caura-Session-Id`, or the request id. |

Primary key `(tenant_id, memory_id, agent_id)` is the upsert's conflict
target and serves "who read memory X". `ix_memory_exposure_memory` backs the
cascade. Ids and counts only, never content.

The write is one `INSERT ... SELECT FROM memories ... ON CONFLICT DO UPDATE`:
rows come from `memories` filtered to the tenant, so an id that names no
memory of the tenant (deleted since, another tenant's) writes nothing instead
of failing the batch. `first_seen` keeps the earliest, `last_seen` the latest,
`count` adds one per read. Ids are sorted so concurrent reads lock in one
order.

Storage routes, bound to one tenant like the value-metering ones:

- `POST /memory-exposure?tenant_id=` `{agent_id, memory_ids (1..500), seen_at, trace_id}` → `{recorded}`.
- `GET /memory-exposure/{memory_id}?tenant_id=` → `{agents: [...]}`.

Public route: `GET /api/v1/memories/{id}/exposure?tenant_id=` →
`{memory_id, agents: [{agent_id, count, first_seen, last_seen, last_trace_id}], total_reads}`,
latest read first. Same access rule as `GET /memories/{id}`: a memory the
caller cannot read, including another tenant's, is a 404.

### What "read" means, and its limits

- A row means the memory was returned to the agent, not that the agent used it.
- Agent identity is what the read named. A tenant-key plugin names its agent
  (`caller_agent_id`); that is an assertion, not an authentication.
- Reads that return no memory row are not counted: lists, documents,
  keystones and briefs, `search_explain`.
- A diagnostic search returns its rows to the caller, so it counts.

## The correction event

Topic `caura.memory.corrected`, published by core-api from one service-level
hook called by the four correction paths (D2), after the change commits.
Fan-out runs in core-worker, off the request path.

| Field | Meaning |
|---|---|
| `memory_id` | The corrected memory. |
| `tenant` | Its tenant. |
| `actor` | Who corrected it: `user:<id>` for a person, `agent:<id>` for a trust>=3 agent. |
| `kind` | `content_edit`, `status_outdated`, `status_cancelled`, `status_deleted`, `unverify` or `conflict_resolve`. |
| `corrected_at` | When the change committed. |
| `summary` | Old → new, short: the first 200 characters of the old and new content for an edit, the old and new status for a status change. |

"Read the old version" is `memory_exposure.last_seen < corrected_at` (or
`first_seen`, when the agent has read the corrected version since): an agent
whose only reads are after the correction is not told.

## Notice format

One bus message per recipient (`to` is that agent), from `caura:corrections`:

```
kind: request
expect_reply_within_seconds: 604800
capability: memory.correction
body:
  A memory you read was corrected.
  memory: <memory_id>  (<title, if any>)
  change: <kind> by <actor> at <corrected_at>
  was: <old summary>
  now: <new summary>
  You last read it at <last_seen> (trace <last_trace_id>).
  Reply "ack" once you have dropped or updated what you took from it.
```

An agent without a bus registration gets the same text as a pending notice at
the top of its next recall response, once, and acknowledges it there.

## Done when

1. **Coverage: at least 95% of the agents that read the old version are
   listed.** Measured on staging with a scripted run: N agents read a memory
   through each surface (MCP recall, REST `/search`, REST `/recall`, by-id on
   both transports, through a chunk), a person corrects it, and the exposure
   list for the memory is compared with the readers.
2. **Delivery: every listed agent gets a notice, and the person sees its
   state.** For each agent on the list: a bus delivery or a pending recall
   notice exists, and the person's view shows it as pending, acknowledged or
   overdue.

## Out of scope (v1)

- The contradiction detector's automatic supersede and near-duplicate merges.
- Telling agents of other tenants that read the memory through a cross-tenant
  credential.
- Reads that return no memory row (lists, documents, keystones, briefs).
- Whether the agent actually used the memory; exposure is "was handed", not
  "relied on".
- Versioning a memory's content per read (which version each agent saw); the
  timestamps stand in.
- Any LLM call: notices are templated text.
- STM.
