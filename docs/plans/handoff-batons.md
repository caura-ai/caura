# Handoff batons: hand work to another agent as a structured request

Status: in progress. Done here: HB-03 (this spec) and the `batons` document
collection made system-managed. In caura-enterprise: HB-04 (schema check),
HB-05 (receiver access filter), HB-06 (stored baton) and the status read of
HB-12. In parallel: HB-10 (the `baton_id` read filter). HB-09, the recall
fallback (`baton_notices`, `GET /batons/{id}`, `POST /batons/{id}/accept`, the
OpenClaw `<handoff_baton>` block): see [Recall fallback](#recall-fallback). Next:
`delivery["baton"]` in caura-bus-mcp.

## Problem

One agent hands work to another, possibly from another vendor (Claude Code to
Codex). Today the hand-over is free text in a bus message or a memory written
"after the completion report". In the two-agent csv-diff run that made the
receiver re-write the sender's memories as its own (duplicates), and nothing
said which memories the receiver was meant to read, whether it could read
them, or whether it did.

A baton is a structured hand-over: the goal, the decisions taken, what failed
and why, open questions, and links to the memories and documents that matter.
The receiver sees it at the start of its work; what it finds links back to it.
No LLM calls. No new access rights: a link is never a grant.

## Decisions

**D1. Carrier: an ordinary bus request.** `kind=request`, body = a JSON string
with `type: "handoff_baton"`, the same convention as the `memory_correction`
notice. The reply window defaults to 7 days (604800 s, the protocol maximum)
when the sender gives none, so the sender sees accepted / replied / late per
receiver. No new message kind and no new wire field. A body with this `type`
sent as `info`, as a reply, or by a person is refused: those paths would skip
the receiver filter (D3).

**D2. Schema v1, checked by the bus when `type` matches.**

```json
{
  "type": "handoff_baton",
  "v": 1,
  "baton_id": "uuid, filled by Caura when absent",
  "goal": "required, at most 2000 characters",
  "decisions": ["at most 20, each at most 500"],
  "failed_approaches": [{"what": "<=500", "why": "<=500"}],
  "open_questions": ["at most 20, each at most 500"],
  "memory_ids": ["uuid, at most 50, duplicates dropped"],
  "doc_ids": [{"collection": "...", "doc_id": "..."}],
  "from_agent": "filled by Caura: the sending agent"
}
```

Unknown fields are refused. A violation answers 422 with
`detail.code = "INVALID_HANDOFF_BATON"`, a message, and per-field `errors`
(`loc`, `msg`). A `from_agent` other than the sender is refused. A
server-filled `baton_id` derives from (tenant, sender, Idempotency-Key), so a
retry with the same key carries the same baton. A client `baton_id` that
already names another sender's baton, or the same sender's baton under another
key, answers 409 `BATON_ID_IN_USE`.

**D3. Access: filter the links as each receiver, at send time.** The bus runs
in core-api's process (`core_api.bus_app`), so it calls core-api's own by-id
read rule, `memory_reader` (`core-api/src/core_api/services/agent_service.py`),
as each receiver over the cited `memory_ids`: trust 0 reads nothing,
`scope_agent` is author-only, `scope_team` is fleet-gated by trust,
`scope_org` is tenant-wide. Missing, deleted and other-tenant IDs are
unreadable. A link the sender itself cannot read is withheld from everyone,
so the counts cannot be used to probe memories the sender cannot see.

Each receiver gets only its readable IDs. Receivers with the same readable set
share one message; all parts go in one thread, and the parts after the first
use idempotency keys derived from the caller's, so a retry replays every part.
The send receipt keeps its usual fields and adds:

```json
"baton": {
  "baton_id": "...",
  "recipients": [
    {"agent_id": "b", "message_id": "...", "links_delivered": 5, "links_withheld": 0},
    {"agent_id": "c", "message_id": "...", "links_delivered": 3, "links_withheld": 2}
  ]
}
```

`links_withheld` is a count: no IDs and no reason. The receiver never sees it.
Nothing is granted and no memory content is copied into the baton. `doc_ids`
pass through unfiltered: documents are tenant/fleet-scoped and a receiver that
cannot read one gets the usual refusal when it tries.

**D4. Receiving.** caura-bus-mcp `peer(op=wait)` adds `delivery["baton"]`
(framed and marked as untrusted data), like `delivery["correction"]`. An agent
that is not on the bus gets pending batons on its next recall
(`baton_notices`, the `correction_notices` pattern: at most 2, until accepted,
7 days). See [Recall fallback](#recall-fallback).

**D5. Link-back.** A receiver writes what it finds with `metadata.baton_id`
and `source_uri = "caura-bus:message/<message_id>"`. A `baton_id` read filter
on `caura_list`, REST list and recall finds them (HB-10). `supersedes_id`
stays server-owned.

**D6. Duplicates.** The baton links, it never copies. Receiver instructions
say: cite the baton, do not re-write the linked memories. `memory_exposure`
(migration 072) shows which linked memories each receiver actually read.

## Storage

The bus stores each baton as a document: collection `batons`,
`doc_id = baton_id`, `agent_id` = the sender, written straight through the
storage client (no doc-memory mint, no embedding). The document holds the
baton as the sender cited it, a hash of the idempotency key, `created_at`,
and per receiver `{agent_id, message_id, memory_ids delivered, links_withheld}`.

A baton is readable by its sender and receivers only. The document routes gate
by tenant and fleet, not by named agents, so `batons` joins the system-managed
collections (`core-api/src/core_api/services/interview_service.py`): REST and
MCP document reads (get, list, query, search), writes and deletes of it are
refused for every credential with a 400. The bus serves it instead.

If the messages were accepted but the document could not be written, the send
answers 503 `BATON_NOT_STORED` with `Retry-After`; a retry with the same key
replays the messages and writes the document.

## Status

`GET /api/v1/bus/batons/{baton_id}`, for the sender or a receiver; anyone
else gets the same 404 as for an unknown baton.

- `baton`: the sender sees the baton as cited; a receiver sees it with its own
  delivered `memory_ids`.
- `receivers[]` (the sender sees every receiver, a receiver only itself):
  `agent_id`, `message_id`, `delivery_id`, `state`, `reply_message_id`,
  `replied_at`, `reply_due_at`, `links_delivered`, `linked_read`, and for the
  sender `links_withheld`.
  - `state`: `replied` (a reply, on time or late), `failed` (dead or
    cancelled), `late` (reply overdue or unanswered), `accepted` (acked),
    else `delivered`.
  - `linked_read`: the delivered IDs whose `memory_exposure` names that
    receiver. A memory it was not handed through the baton does not count.
- `findings`: the receivers' memories with `metadata.baton_id`. Until the
  `baton_id` filter (HB-10) is deployed this answers
  `{"available": false, "count": null, "memory_ids": []}`.

## Recall fallback

HB-09. A receiver the bus cannot wake (an OpenClaw agent, a session that only
recalls) hears about a baton on its next recall, whatever it searches for.

**Who is waiting.** The bus owns delivery state, and in standalone OSS there is
no bus. So core-api keeps a small table of its own, `baton_recipient`
(migration 076): one row per (tenant, baton, receiver) with `from_agent`,
`goal`, `created_at`, the bus `message_id`, and `state`: `delivered` until the
receiver takes it up, then `accepted` or `replied`. The bus writes the rows
when it sends a baton, through core-api's storage client in process
(`record_baton_recipients`; storage `POST /batons/{id}/recipients`, idempotent).
Standalone OSS writes none, so the lookup finds nothing.

**On recall.** MCP `caura_recall` and REST `/search` and `/recall` add
`baton_notices: [{baton_id, from_agent, goal, created_at, ask}]` for an agent
with `delivered` rows younger than 7 days: at most 2, newest first, the goal cut
to 300 characters. The key is absent when there are none, so the ordinary
response is byte-identical. One probe of the partial index
`ix_baton_recipient_waiting (tenant_id, agent_id, created_at) WHERE
state = 'delivered'`, run beside the search; a failure reads as no notices.
Identity is the correction-notices rule: an agent key, else the body's
`caller_agent_id`, else `filter_agent_id`; nothing for no agent or a person.

**Reading and accepting.** `GET /api/v1/batons/{id}` (MCP `caura_manage
op=read_baton`) returns the baton from the `batons` document as this receiver
got it, each link checked again against its access today and given a title
(`linked`), or as the sender cited it; anyone else gets 404. `POST
/api/v1/batons/{id}/accept` (MCP and plugin `caura_manage op=accept_baton`)
marks the row `accepted`, receiver only, with the same identity rules as
`POST /corrections/{id}/confirm`: an agent credential acts as itself, a tenant
key names the agent with `agent_id`, a person cannot.

**Keeping the bus and the table in step** (enterprise follow-up):

- Bus → table. `GET {COLLABORATION_API_URL}/api/v1/bus/system/batons/{id}?tenant_id&agent_id`
  (`X-Caura-System-Secret`, plus a Cloud Run ID token for an `https` bus)
  answers `{"receivers": [{"agent_id", "state"}]}` with the status read's
  states. After a response that showed a notice, core-api asks it for that
  receiver (at most every 30 s per row, fire-and-forget) and copies `accepted`
  (an ack) or `replied` into the row, so the notice stops for an agent that
  took the baton up on the bus. Until the bus serves the route, a 404 reads as
  "no change".
- Table → bus. A recall-side accept does not touch the bus delivery: acking it
  takes the receiver's own lease. The bus's baton status reads the rows
  (`list_baton_recipients`, storage `GET /batons/{id}/recipients`) and shows a
  receiver that accepted on recall as `accepted` while its delivery is still
  open (`delivered` or `late`).

**OpenClaw.** Automatic recall reads each noticed baton (`GET /batons/{id}`)
and shows it before the recalled memories, framed as data:

```
<handoff_baton>
- Handoff from <from_agent>: <goal>. Decisions: … Failed approaches (don't
  retry): … Open questions: … Linked memories: <id — title> (read them with
  caura_manage op=read). Tag every memory you write for this work with
  metadata.baton_id=<id>; cite, don't copy, the linked memories. Once you take
  it up, accept it: caura_manage op=accept_baton baton_id=<id>
</handoff_baton>
```

The plugin never accepts on the agent's behalf.

## Cost

A message whose body does not contain the string `handoff_baton` pays one
substring test and nothing else. A baton costs one document read, one bulk
memory read plus one agent lookup per sender and receiver, one storage send
per distinct readable set, and one document write. On recall, an agent with no
waiting baton pays one index probe beside the search; one with a waiting baton
also pays, at most every 30 s per baton, one bus status call after the response.
