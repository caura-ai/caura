# Handoff batons: hand work to another agent as a structured request

Status: in progress. Done here: HB-03 (this spec) and the `batons` document
collection made system-managed. In caura-enterprise: HB-04 (schema check),
HB-05 (receiver access filter), HB-06 (stored baton) and the status read of
HB-12. In parallel: HB-10 (the `baton_id` read filter). Next: receiving
(`delivery["baton"]` in caura-bus-mcp, `baton_notices` on recall).

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
7 days). Not part of this change.

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

## Cost

A message whose body does not contain the string `handoff_baton` pays one
substring test and nothing else. A baton costs one document read, one bulk
memory read plus one agent lookup per sender and receiver, one storage send
per distinct readable set, and one document write.
