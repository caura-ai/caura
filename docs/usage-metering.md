# Usage metering

Caura records what each tenant's model calls cost and what its memory saved, in
its own database. A tenant is shown as a workspace in the Caura app, and this
page calls it a workspace from here on. Counts only, never content, and nothing
is sent anywhere: this is not telemetry (see [telemetry.md](telemetry.md) for
the separate, opt-in heartbeat). Read it with [`GET /api/v1/spend`](api-reference.md).

## What is recorded

**Every model call the server makes**, with the provider's own token counts:
enrichment, embeddings, entity extraction, contradiction checks, duplicate
judgement, crystallization, skill distillation, insights, recall briefs,
document ingest and the rest. Each is attributed to the workspace, agent,
fleet and operation it was made for, and the provider and model that served
it.

| Column | Meaning |
|---|---|
| `calls` | Provider responses received, which are what a provider bills. Retries and fallbacks count as the calls they are. |
| `failures` | Attempts that raised, timeouts included. No tokens. A response that was billed and then rejected (truncated JSON, say) is one call and one failure. |
| `tokens_in`, `tokens_out` | As the provider reported them. OpenAI-compatible providers (OpenAI, Anthropic, OpenRouter), Vertex and Gemini report them; a local embedder or a self-hosted sidecar that does not counts as calls with 0 tokens. |
| `tokens_reasoning` | Reasoning ("thinking") tokens: billed as output, never in the visible text. |

A call no request or background task names a workspace for is recorded under
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
(migration 066). Each flush is for one workspace: storage writes every row
under the workspace the request names, so no flush can reach another's
totals. Each carries an id storage records once, so a flush whose reply was
lost is re-sent and counted once. A crash loses at most the
last 15 seconds; a clean shutdown flushes first. If storage is unreachable, up
to 20 flushes wait and the oldest is then dropped, with an error naming its
row count.

Both tables go with a workspace when it is purged, and with a fleet when that
fleet is.

## Turning it off

Per workspace, in its settings:

```json
{"observability": {"usage_metering_enabled": false}}
```

Storage drops that workspace's rows from the next flush on, for core-api and
core-worker alike. Rows already recorded stay until the workspace is purged.
