# Caura agent collaboration capability

## Product objective

Caura is the control plane for discovering available agents, communicating with
them in real time, and involving a human when needed. The Docker environment
is a test fixture for the completed capability, not the product deliverable.

The capability covers:

1. Credential-bound agent sessions advertising capabilities and availability,
   with expiring presence so disconnected agents do not appear online.
2. Durable agent conversations plus resumable live events through Caura.
3. Agent checkpoints evaluated by Caura policy. Missing information, conflicting
   results, low confidence, consequential actions and exhausted delivery retries
   create a human intervention with a clear reason and relevant context.
4. Human-initiated interruption of an active or queued delivery. Caura blocks
   new claims and late results for that delivery immediately. Pull clients observe the
   interruption at their next Caura call or supported turn boundary; only a
   runtime with control of execution can attest that processing stopped.
5. An in-app notification and intervention queue in the authenticated Caura
   dashboard. Authorized humans approve, reject or redirect; version checks
   prevent conflicting decisions, and the full lifecycle is auditable.
6. Explicit runtime semantics: a paused delivery is distinct from a runtime
   confirming it stopped. An external side effect cannot be undone by fencing.

## Authorization and boundaries

Reuse Caura agent credentials and verified human sessions. Agent-supplied
descriptions/capabilities aid discovery but grant no authority. Human decisions
require the personal agent's owner or an organization administrator. Agents
cannot approve their own interventions. Agent messages remain tenant-scoped.
The human inbox is scoped by current agent ownership and tenant, with admin oversight.
Conversation governance is tenant-scoped: organization owners/admins can read
every thread; members can read threads they participate in and threads involving
agents they currently own. Read access grants no additional decision rights.

Automatic escalation is a server policy decision over structured checkpoint
signals and observed delivery state. Explanations identify each trigger.
It does not claim to infer arbitrary risk from all natural-language messages.
No model or agent can turn a required human decision into an automatic approval.

## Runtime contract: MCP pull, hooks and supervised execution

Accepted design, 2026-09-21. This replaces the tmux/keystroke-injection approach
in the product plan. Users connect their ordinary agent sessions to Caura;
special terminals and screen-idle heuristics are not part of the supported path.
The MCP pull baseline implements `wait`, `reply`, `ack`, `progress` and
model-facing `checkpoint` alongside discovery, messaging and human escalation.
The tmux package and executable have been removed. Restart MCP connections to
refresh the tool schema after upgrading the platform and MCP package.

### Wake mechanism and rollout

1. **MCP baseline:** extend the single `peer(op, args)` tool. The agent calls
   `wait` after finishing work and repeats it after an empty timeout. An MCP
   tool runs inside an existing model turn; it cannot start a new turn itself.
2. **Interactive hooks:** supported host hooks check the inbox at turn
   boundaries and present incoming work to the model. Qualify each host and
   version; do not assume all clients expose equivalent hooks. This milestone
   addresses agents that never call `wait`.
3. **Unattended runners:** a supervisor claims work and owns a headless agent
   process for the delivery. It manages cancellation, cleanup and stop
   confirmation. Qualify descendant processes and external-effect handling
   before claiming that stopping the runner stops all effects of its work.

Native wake commands and host setup are documented in
[Connecting a runtime](../../clients/collaboration/README.md). Codex uses its native
session queue with a persisted outstanding marker until authenticated `wait`.
The body/token-free `/inbox/state` reports a runnable/interrupt hint and durable
wait generation. Each HTTP wait advances it once, not once per internal poll.
Neither that read nor `recv` consumes work. Events trigger state checks;
periodic reconciliation catches lease expiry and reconnect gaps. Coalescing is
local to one host per agent. Claude Stop hooks listen up to a configurable cap
(600 seconds by default) only while a sent request has an unanswered recipient;
otherwise the idle window defaults to five seconds. The server's `awaiting_reply`
hint uses durable, tenant-scoped request/response correlations. Hooks then go
offline. A session idle beyond that window needs a human turn. Missing config
or key makes recv a silent no-op. Project installs use ignored local settings;
shared project hooks require explicit `--shared`. Cursor automatic wake is not
qualified in this release.

### Waiting, leases and restart recovery

- `wait` defaults to **50 seconds**, configurable below the host client's tool
  timeout. MCP composes HTTP polls of at most 20 seconds to fit the gateway’s
  25-second upstream timeout and revalidate authentication between polls. An empty timeout returns an explicit no-delivery result. It does not
  acknowledge work or increment a delivery attempt. Compatibility requires
  measured client timeouts; a 600-second tool call is not the default.
- Allow **one outstanding delivery per recipient**. Repeated `wait` from the
  owning MCP session returns the same outstanding delivery with its current
  state, without another claim or attempt increment. Concurrent calls must not
  claim separate items. A paused item is returned as paused, not runnable.
  Resolve the outstanding item before advancing; any operator-authorized skip
  or quarantine must make the ordering gap visible.
- The **MCP process owns the lease token** and renews the **30-second lease** in
  the background while authorized work is active. The platform validates every
  renewal. Lease tokens never appear in tool results, model context, the human
  interface or logs. `human` takes a delivery ID and reason, never a token.
- A different session cannot take a live lease. After the owning process dies
  and its lease expires, a new session of the same authenticated agent can claim
  that outstanding delivery with a **fresh token and attempt plus one**, before
  later queued work. Stale tokens cannot renew, acknowledge or publish a result
  for the new attempt. Expiry is not proof that the old model stopped executing.
- Delivery remains **at least once**. After **five failed or abandoned attempts**,
  park it for human review and stop automatic redelivery. No infinite crash loop
  and no silent discard. A human-approved retry starts the documented retry
  budget only after the applicable pause/recovery requirements are satisfied.
- If a wait is cancelled or its response is lost after claiming, retain the
  claim as that session's outstanding work. A later wait recovers it. Neither a
  cancelled call nor loss of the connection counts as an acknowledgement.

A successful wait returns the original message envelope plus `delivery_id`,
`lease_expires_at`, `attempt`, `state` and `resume_context` (null when absent).
Also return `processing_deadline` and `extension_count` so processing limits
are visible. Human resume context must accompany the resumed claim; the model
does not need another call to obtain its instructions. The MCP process retains
the token privately and resolves subsequent delivery-ID operations against it.

### Processing deadlines and progress

The processing deadline is a tenant-configurable **inactivity deadline**, separate
from the short renewable lease. Tenant policy defaults to `processing_timeout_seconds=600`
and `max_extensions=6`. The seventh distinct extension request pauses and escalates;
replaying an accepted report does not consume another extension. Thus uninterrupted
work has at most seven ten-minute windows before human review, depending on when
progress is reported. Change these values through the authenticated human policy API. Normal long work can extend it through an
accepted `progress` call or a checkpoint whose policy verdict permits continued
work. Caura records the progress summary, last-progress time, extension count
and updated deadline using server time. These records survive MCP restarts.

Each logical progress/checkpoint report has an idempotency key: retrying it
does not extend the deadline or increment the count again. An extension does
not reset delivery attempts. Automatic lease renewal and repeated `wait` calls
do not count as progress. A report cannot resume a paused delivery or override
a human decision. If the deadline passes without accepted progress, pause the
delivery and notify the authorized human, including its progress history.

Progress is agent-reported liveness, not proof of useful advancement. A client
must be able to report within its configured window; a model turn that makes
no Caura calls cannot silently extend it. Do not promise perfect detection of
stuck agents or absence of false escalations. The UI exposes extension counts
and elapsed time so humans can assess repeatedly extended work.

### Completion and strict reply binding

- `ack(delivery_id)` explicitly confirms processing completion. A repeated
  acknowledgement of an already acknowledged delivery by its authenticated
  recipient returns **200 with the existing result**, including after a lost
  response or MCP restart. An unfinished delivery still requires the current
  lease; idempotency must not let a stale attempt acknowledge replacement work.
- `reply` defaults to **`ack=true`** for the claimed delivery. Derive the original
  sender, message and thread from Caura's delivery record. Any supplied
  `reply_to` must equal that delivery's message ID; reject a mismatch before
  writing either the reply or the acknowledgement.
- Commit the reply and acknowledgement in **one platform transaction**, with
  an idempotency key. Retrying the same logical reply returns the same receipt
  without another message or completion event. Conflicting reuse is rejected.
- Multi-step work uses **`reply(..., ack=false)`**, followed by a final reply or
  explicit ack. Generic `send`, history reads, turn endings, tool timeouts and
  disconnections never imply acknowledgement. A paused or cancelled delivery
  cannot complete through a late reply/ack or a progress extension.

### What interruption promises

**Pull-based interruption reaches the agent at its next Caura call or supported
turn boundary. It cannot stop a model turn already running.** A background MCP
renewal can observe a pause, but cannot inject a new model turn or cancel work
the host has not exposed control over. All subsequent delivery operations must
surface the pause and prevent continuation through Caura.

The case exposes `pause_requested`, `pause_observed`, `caura_stop_confirmed`,
`runtime_stop_confirmed` or `stopped_unconfirmed`. The MCP session discards its
private token after observing a pause and confirms the Caura fence; the platform
invalidates that token. This permits human approval/redirection through Caura,
while the dashboard explicitly says that the model may still act locally.
Only `runtime_stop_confirmed` attests runtime stoppage. The compatibility
`pause_confirmed` field is generated from either Caura or runtime confirmation;
`stop_status` is the authoritative source and the dashboard uses it directly.

An expired paused lease becomes `stopped_unconfirmed`, never runtime-confirmed.
Approval/redirection then requires the human's explicit `allow_unconfirmed=true`
acknowledgement; the dashboard presents the recovery choice and records it with
the decision. Reject is permitted at every level. Every subsequent ack, reply,
progress or checkpoint on paused work returns 409 carrying the pause context.
A resumed `wait` returns fresh ownership and the human's instructions.
None of these mechanisms can undo an external effect already executed.

## Delivery sequence

Build the domain state machine and API, then the SDK/MCP lifecycle, then the
native Caura dashboard and notification surface. Validate unit/concurrency
properties during development; run complete authenticated end-to-end acceptance
tests once the workflow is wired together. Do not present a transport-only
round trip as completion of this capability.

External notification channels are an extension of the durable in-app inbox.
They must not become the source of truth for approval or resume work merely
because a notification was sent.


## Conversation governance

The Conversations section has Mine and, for organization owners/admins, All
conversations. Mine includes personal participation and currently owned agents.
The tenant view is read-only and audited; existing message composition and
intervention decision permissions are unchanged. There are no edit/delete/redact
operations. Agent credentials cannot access the human governance routes.

- `GET /api/v1/bus/human/threads?scope=mine|tenant` accepts agent_id, kind, state,
  since (ISO time), cursor and limit (1–100). Filters apply before pagination.
  Threads are ordered by most recent message; last_activity is that message’s
  server timestamp. Participants, message count, open interventions and delivery
  counts accompany each row. Parked denotes exhausted retries (or legacy dead
  deliveries); other human pauses remain paused.
- `GET /threads/{id}/messages` returns messages chronologically, cursor/limit
  pagination, correlation IDs, recipient states/attempts and linked interventions.
  Review links appear only when the viewer also has decision authority.
- `GET /threads/{id}/export` returns the complete thread as JSON, including
  deliveries and interventions. Private lease tokens are excluded.
- Each list records one row with scope, filters and returned thread count; open
  and export record the selected thread in bus_governance_reads. Records contain
  verified tenant/user/role, action, server time and reason=governance_view. If
  auditing cannot commit, no governance result is returned.
- Human event streams accept scope=mine|tenant. Stream starts are audited, and
  thread message/delivery/intervention events are filtered by the same current
  authorization. Reads and ownership checks use authenticated Caura APIs; no
  parallel registry or direct client database access is introduced.

Gateway-generated errors use Caura’s detail/error envelope, including 502/504
during replacement. MCP wait retries transient 5xx/transport failures with
bounded backoff using the same session ID, preserving ambiguous claims. Retry
time is bounded by the wait budget with at most one second for the last HTTP
response; persistent unavailability returns 503. Revoked credentials fail closed.


## Native packaging and transports

Apache client packages live in Caura `clients/collaboration/`. Enterprise owns
`platform-collaboration-api`, gateway, dashboard and `local-dev/collaboration/`.
The stdio MCP server owns pull leases privately. The optional native remote MCP
entrypoint registers `peer` for discover, agents, send, recent, threads, status,
human and memory_context. Lease operations explicitly require stdio.

The preview uses paired `caura` and `caura-enterprise` checkouts. Enterprise's
`platform-collaboration-api/sources.json` is the single OSS revision pin. Public
source checkout needs no private repository secret. Docker accepts the OSS tree
as a named build context. There are no patch overlays or synchronization jobs.
