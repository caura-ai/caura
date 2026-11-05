# Correction propagation: tell every agent that read a memory when a person corrects it

Status: in progress. Done: CP-04 (this spec), CP-05 (`memory_exposure`,
migration 072), CP-06 (exposure recorded on every agent read), CP-07
(`GET /memories/{id}/exposure`), CP-10 (the recipient list: `memory_correction`,
migration 073, and the preview routes), CP-11 and CP-15 (sending the notices and
the person's view of who confirmed, migration 074). Next: the recall fallback
(CP-13).

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

## Recipients (CP-10)

A consumer of `caura.memory.corrected` (`common/events/correction_handlers.py`;
core-worker on SaaS, core-api on the in-process bus) posts each event to
core-storage-api, which in one transaction writes a `memory_correction` row
(id, tenant, memory, kind, actor, corrected_at, old/new summary, detail,
status `preview`) and one `memory_correction_recipient` row per agent to tell
(agent_id, reads, first_seen, last_seen, last_trace_id, notice_state
`pending`). Migration 073.

- Recipients: the memory's `memory_exposure` rows with
  `first_seen < corrected_at`, less the correcting agent (when the actor is an
  agent), latest read first, at most 50. `truncated` counts the readers past
  the cap.
- One record per event: `event_id` is unique, so a redelivery returns the first
  record and writes nothing. A memory no longer in the tenant records nothing.
- Both tables cascade from their parent, and go with a tenant purge.

Preview, before anything is sent, with the same access rule as
`GET /memories/{id}` (another tenant's correction is a 404):

- `GET /api/v1/corrections/{id}?tenant_id=` → the record, `recipients`, and
  `counts {recipients, truncated, readers, pending}`;
- `GET /api/v1/memories/{id}/corrections?tenant_id=` → the memory's
  corrections, newest first.

An agent that is not on the recipient list gets `old_summary: null`.

## Sending (CP-11) and who confirmed (CP-15)

`POST /api/v1/corrections/{id}/send` (MCP `caura_manage op=send_correction`):
a signed-in person, an admin or tenant key, or an agent at trust_level >= 3
that can read the memory. `core_api.services.correction_sender` moves the
record `preview` → `sending` and posts the recipients, at most 200 per call, to
the bus's system sender (`POST {COLLABORATION_API_URL}/api/v1/bus/system/corrections`,
header `X-Caura-System-Secret` = `COLLABORATION_SYSTEM_SEND_SECRET`, else
`CORE_STORAGE_SHARED_SECRET`; `Idempotency-Key` = the correction id; on Cloud
Run, where the collaboration service refuses unauthenticated callers, also a
Google ID token for that URL in `X-Serverless-Authorization`, so core-api's
service account must be an invoker of it). Per
recipient:

| Bus answer | `notice_state` | `channel` |
|---|---|---|
| `accepted`, `already_sent` | `sent` | `bus` |
| `unknown_agent` (not on the bus) | `pending` | `recall` |
| `throttled`, `backlog_full` | `pending`, with `next_attempt_at` | `bus` |
| the call is refused (401/403/404/409/413/422) | `pending` | `recall` |
| no answer (network, 429, 5xx) | `pending`, retried after 30 s | `bus` |
| no `COLLABORATION_API_URL` (standalone OSS) | `pending` | `recall` |

A `recall` recipient is not unreachable: it is told on its next recall, so it
stays `pending` until then. The first recall or search response that hands it
the notice sets `notified_at` and moves it to `delivered` (fire-and-forget
after the response, one `UPDATE … WHERE notified_at IS NULL`, so it happens
once; a `confirmed` notice never moves back). Its own confirm then makes it
`confirmed`. `unreachable` is no longer written; rows written before keep it
and are shown like a `pending` recall notice (Prism: "Waiting for next
recall"; `delivered` on recall: "Seen on recall").

A throttled recipient is retried inline when the bus asks for at most 5 s (twice
at most), then by a background task in core-api (six tries, each waiting what
the bus asked, 5 s to 5 min). What is still waiting after that is retried by
the next send or status read. The record is `sending` while any recipient
waits on the bus, then `sent`. Sending is idempotent: a `sent` record comes
back as it is.

`GET /api/v1/corrections/{id}` returns `{id, memory_id, kind, actor: {kind,
id}, corrected_at, old_summary, new_summary, detail, status, sent_at, sent_by,
counts, recipients: [{agent_id, reads, first_seen, last_seen, channel,
notice_state, confirmed_at, notified_at, …}]}`; who corrected it is `actor`
(there is no `corrected_by` field: that name is only in the bus notice body),
and who sent it is `sent_by`. It refreshes the bus outcomes
(`GET .../bus/system/corrections/{id}`) at most every 30 s (`bus_checked_at`):
the bus's `outcome` (`sent|delivered|confirmed|late|failed`) becomes the
recipient's `notice_state`, and `confirmed_at` its reply or ack time. A
recipient confirmed through `POST /api/v1/corrections/{id}/confirm` (MCP
`caura_manage op=confirm_correction`, the recall fallback's path) keeps that
state. The record is `closed` once every recipient is `confirmed` or `failed`.
The counts are `{recipients, truncated, readers, pending, sent, delivered,
confirmed, late, failed, unreachable}` (`unreachable` counts legacy rows only).

Migration 074 adds the record's `sent_at`, `sent_by`, `bus_checked_at`, the
recipient's `channel`, `confirmed_at`, `notified_at`, `next_attempt_at`,
`attempts`, `bus_message_id`, `bus_delivery_id`, CHECKs on `status` and
`notice_state`, and `ix_memory_correction_recipient_agent (tenant_id, agent_id)`
for the recall fallback.

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

An agent the bus cannot tell (no bus registration, the bus disabled, or a bus
delivery that `failed`) gets it on recall instead (CP-13): every MCP
`caura_recall` and REST `/search` / `/recall` response for that agent, on any
query, carries `correction_notices: [{correction_id, memory_id, old_summary,
new_summary, corrected_at, ask}]`, at most 3, newest first, until the agent
confirms (`caura_manage op=confirm_correction`, `POST
/api/v1/corrections/{id}/confirm`) or the correction is 7 days old. The key is
absent when there is nothing to tell, so an ordinary recall is unchanged. The
first response that carries a notice marks that recipient `delivered` (see
Sending).

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
