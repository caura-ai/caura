# Usage metering

Caura records what each tenant's model calls cost and what its memory saved, in
its own database. Counts only, never content, and nothing is sent anywhere:
this is not telemetry (see [telemetry.md](telemetry.md) for the separate,
opt-in heartbeat). Read it with [`GET /api/v1/spend`](api-reference.md).

## What is recorded

**Every model call the server makes**, with the provider's own token counts:
enrichment, embeddings, entity extraction, contradiction checks, duplicate
judgement, crystallization, skill distillation, insights, recall briefs,
document ingest and the rest. Each is attributed to the tenant, agent,
fleet and operation it was made for, and the provider and model that served
it.

| Column | Meaning |
|---|---|
| `calls` | Provider responses received, which are what a provider bills. Retries and fallbacks count as the calls they are. |
| `failures` | Attempts that raised, timeouts included. No tokens. A response that was billed and then rejected (truncated JSON, say) is one call and one failure. |
| `tokens_in`, `tokens_out` | As the provider reported them. OpenAI-compatible providers (OpenAI, Anthropic, OpenRouter), Vertex and Gemini report them; a local embedder or a self-hosted sidecar that does not counts as calls with 0 tokens. |
| `tokens_reasoning` | Reasoning ("thinking") tokens: billed as output, never in the visible text. |

A call no request or background task names a tenant for is recorded under
`_unattributed`, so a missed path shows up instead of disappearing.

**Savings, only where both sides are known exactly:**

| Metric | Meaning |
|---|---|
| `recall.count`, `recall.results` | Recalls that returned memories, and how many. Diagnostic searches are not recalls. |
| `recall.returned_tokens` | Tokens Caura handed back. |
| `recall.derived_returned_tokens`, `recall.derived_source_tokens` | For a result that is a piece of a larger memory (an auto-chunk of a long memory, or an atomic fact extracted from one), the tokens returned and the full size of the distinct parents it came from. The difference is the context saved: without the store, the agent would have sent the whole parent. A whole memory returned as itself saves nothing. |
| `recall.cross_agent_results` | Results another agent wrote: shared memory in use. |
| `hygiene.retired_memories`, `hygiene.retired_tokens` | Duplicates a merge took out of recall: crystallization, and the write-path near-duplicate merge. |
| `hygiene.written_tokens` | What crystallization wrote back in their place. |

Deliberately **not** recorded: savings "against sending every memory" (nobody
would), time saved, or anything else Caura cannot observe. Imported documents
are left out of savings: the server keeps no size for the original document it
could stand behind.

Tokens Caura counts itself (recall and merge sizes) use tiktoken
`cl100k_base`. Your agent's own model may tokenize a little differently.

## How it is stored

Per minute, not per call: core-api and core-worker add calls up in memory and
flush every 15 seconds into `llm_usage_minutes` and `value_usage_minutes`
(migration 066). Each flush is for one tenant: storage writes every row
under the tenant the request names, so no flush can reach another's
totals. Each carries an id storage records once, so a flush whose reply was
lost is re-sent and counted once. A crash loses at most the
last 15 seconds; a clean shutdown flushes first. If storage is unreachable, up
to 20 flushes wait and the oldest is then dropped, with an error naming its
row count.

Both tables go with a tenant when it is purged, and with a fleet when that
fleet is.

## Turning it off

Per tenant, in its settings:

```json
{"observability": {"usage_metering_enabled": false}}
```

Storage drops that tenant's rows from the next flush on, for core-api and
core-worker alike. Rows already recorded stay until the tenant is purged.

## Background model admission

Background extraction, enrichment, contradiction checks and asynchronous
interview synthesis share a tenant allowance across API and worker instances.
The unit is a provider-call attempt, including each retry and fallback. This is
not a currency cap; provider token totals remain the cost record.

Storage settings `BACKGROUND_LLM_ATTEMPTS_PER_MINUTE` (default 120) and
`BACKGROUND_LLM_CONCURRENCY` (default 4) bound each tenant's attempts in a
rolling 60-second window and its active admitted calls. Both must be positive.
Credits are reserved atomically on the writer before a call. Recent attempt ids
make lost-reply retransmission idempotent. Releasing a call frees its slot but
does not refund the attempt. Unknown reservation outcomes (including timeout,
malformed response and cancellation) trigger release with one short retry,
all within a two-second cleanup budget.
Release records a 15-minute tombstone even if reserve has not committed yet;
a late reserve cannot reactivate that attempt id. A never-admitted tombstone
has no admission timestamp and consumes no allowance; an already-admitted
attempt remains spent. If release itself is unavailable, or the caller crashes,
the active slot still expires after 15 minutes. The optional usage-metering
switch does not disable admission.

At a full allowance, a busy slot, or unavailable admission, no further
background call starts. Committed memory writes and accepted interview windows
stay successful. Interview jobs use separate per-process background tenant
slots (`PER_TENANT_BACKGROUND_LLM_CONCURRENCY`, default 2), leaving foreground
`llm` slots available. Their provider calls still require the shared storage
budget. On admission deferral, the processor resets the job to pending and
refunds its failure attempt, retrying the refund during pending reset if its
first write fails. If every storage write remains unavailable, the job can
remain processing with an unrefunded attempt; content-free `refund_unavailable`
and pending-reset warnings identify that case for stale-job recovery and
operator review. Other deferred work stores only identifiers, operation,
attempt count and terminal reason;
retry workers re-read current content and skip deleted, held or suppressed
sources. A changed retry generation protects new work from an older completion.
If a retry reference itself cannot be persisted within two seconds, the service
logs `retry_unavailable`; the model call remains refused. The memory write is
not rolled back. On the in-process API path this logged loss needs an operator
to retry the work. The worker instead fails the enrich-event handler with a
content-free exception, preserving event redelivery; a successfully persisted
deferral completes the handler normally.

Contradiction detection additionally has fair, bounded queues per API process:
16 running passes globally, at most 4 per tenant, 64 queued per tenant and 256
queued globally by default (`CONTRADICTION_DETECTION_CONCURRENCY`,
`CONTRADICTION_DETECTION_PER_TENANT_CONCURRENCY`,
`CONTRADICTION_DETECTION_TENANT_BACKLOG`, and
`CONTRADICTION_DETECTION_GLOBAL_BACKLOG`). Excess becomes a durable reference.
The API retry loop pages only tenants with due, unleased references (up to 32
tenant ids per page). Each turn first assigns one item per eligible tenant,
then distributes remaining capacity up to `BACKGROUND_RETRY_PER_TENANT_CONCURRENCY`
(default 4, range 1–8), with at most 16 globally in flight. A full tenant cannot
occupy the whole pool. `BACKGROUND_RETRY_POLL_SECONDS` defaults to one second
(range 1–60). Idle tenants do not consume claim turns. A 100-item bulk can shed
32 passes at the default gate limits; once due, the scheduler can claim them
in eight turns when capacity is available, rather than one per ten seconds.
Provider latency and the shared admission budget still bound actual completion.
Discovery uses the existing
internal tenant-list endpoint with `background_due=true`; normal lifecycle
discovery is unchanged. Discovery walks a partial C-collated tenant index with
one ordered probe per returned tenant, skipping duplicate references. At most
32 tenant ids reach its final sort; it does not sort the full due backlog.
Failed replays back off from 60 seconds, doubling
up to one hour, and stop after eight failures (including interrupted claims).
Admission refusals and pending embeddings do not consume that failure cap;
they retain the reference and use the admission delay (60 seconds for a pending
embedding). Exhaustion parks the reference
with fixed `terminal_reason=retry_exhausted` in the same transaction, retaining
identifiers and attempts as a durable queryable record; no memory text is stored.
Parked rows are excluded from due discovery and claims, and cannot be deleted
by a stale completion. A new explicit deferral reopens the row with a fresh
attempt count and generation; otherwise it remains parked until tenant purge.
Replay deferrals retain the existing reference and refund only the current
claim's provisional attempt. Finishing a retry advances its generation, so a
duplicate finish cannot refund prior failures; a newer external deferral is
protected from stale completion.
That shortcut applies only to the matching active claim. Child work for another
claim, or work deferred after replay starts completion, persists normally even
when the child inherited the replay context.
Admission and retry logs carry reasons and identifiers, never memory text.

Deploy storage with migration 080 first, then upgrade both API and worker.
Until both callers are upgraded, older callers do not participate in the shared
allowance. A new caller against old/unavailable storage refuses background
model work; there is no unlimited fallback. Tenant purge removes budget,
reservation and deferred-reference rows. No pre-existing content is rewritten.
