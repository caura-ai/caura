# Evidence contract v1: where a memory came from, and whether anyone checked it

Status: in progress. Done: EC-04 (this spec), EC-07 (schema, migration 071),
EC-08 (write path, server-stamped `trace_id`), EC-09 (server-owned
verification fields). Open: the compact `evidence` object on recall (EC-10),
`tool_observed` on write (EC-13), the verify operation, the `verified_only`
filter.

## Goal

Every recalled memory says where it came from, whether anyone checked it, and
why it was picked. An agent reading a recalled fact today sees its content and
a score. It cannot tell a fact a tool returned from one another agent guessed,
or a fact a person confirmed from one nobody looked at again.

## Fields on `memories`

| Field | Type | Set by | Meaning |
|---|---|---|---|
| `source_uri` | text, null | writer | Existing. Where the content came from (URL, file, `upload:<name>`). |
| `run_id` | text, null | writer | Existing. The writer's own run id. Never stamped by the server. |
| `observed_at` | timestamptz, null | writer | When the fact was observed, if earlier than the write. NULL when the writer did not say; `created_at` stays the write time. |
| `trace_id` | text, null | writer, else server | The session or request the write came from. See below. |
| `tool_call_id` | text, null | writer | The tool call whose result the memory records. |
| `verification_status` | text, NOT NULL, default `unverified` | server only | See the state machine. CHECK-constrained to the five values. |
| `verified_by` | text, null | server only | Who moved the row off `unverified`: a user id, or `agent:<id>`. |
| `verified_at` | timestamptz, null | server only | When. |

`trace_id` and `tool_call_id` are capped at 200 characters, like `run_id`.

### Where `trace_id` comes from

A writer may send its own. When it sends none, core-api stamps
`session_context.server_trace_id()`, in this order:

1. The session: on MCP the `Mcp-Session-Id` the client sent, or the one the
   server issued on that response (`mcp-<hex>`); on REST the
   `X-Caura-Session-Id` header.
2. Otherwise an id issued for the one HTTP request (`req-<hex>`). A bulk write
   stamps one id for the whole batch; an item may still carry its own.

Outside an HTTP request (a background writer) nothing is stamped: there is no
request to name, and a made-up id would look like provenance. A row derived
from another (an auto-chunk child, an atomic fact) copies its parent's
`observed_at`, `trace_id` and `tool_call_id`, as it copies `source_uri` and
`run_id`.

### Write surfaces

- REST `POST /memories` and every item of `POST /memories/bulk`: all three
  writer fields.
- MCP `caura_write`: single writes take `observed_at`, `trace_id` and
  `tool_call_id` as parameters ("single only", like `source_uri` and
  `run_id`); a batch item carries its own inside `items`.
- Read: `MemoryOut` (write responses, list, search hits) and
  `GET /memories/{id}` return all six. The compact recall payload is unchanged
  until EC-10.

## Verification state machine

```
            ┌──────────────► tool_observed ──┐
unverified ─┤                                ├──► disputed
            └──────────────► human_verified ─┘
                 corroborated (reserved, not set in v1)
```

- `unverified`: every new row. The default; nothing about it was checked.
- `tool_observed`: the content records a tool's result the server can point
  at (`tool_call_id` or `source_uri` present). Set on write by EC-13, not by
  the writer.
- `human_verified`: a person, or an agent at `trust_level >= 3`, confirmed it
  through the verify operation. Audited.
- `disputed`: someone with the same rights marked it wrong, or a later
  contradiction did.
- `corroborated`: reserved for "independent sources agree". No v1 path sets it.

## Who may set what

| Field | Writer (REST, MCP, SDK, broker) | Server |
|---|---|---|
| `observed_at`, `tool_call_id` | yes, on create | copies to derived rows |
| `trace_id` | yes, on create | stamps when absent |
| `verification_status`, `verified_by`, `verified_at` | never: a create, a bulk item or a PATCH naming one is refused with a message saying the server sets it | the verify operation (dashboard users, agents with `trust_level >= 3`, audited) and EC-13's `tool_observed` |

The refusal is the C6 one `supersedes_id` already gets (`SERVER_OWNED_MEMORY_FIELDS`
in `core-api/src/core_api/schemas.py`): a 422 on single write and PATCH, a
per-item `error` on bulk.

## The `evidence` object on recall (planned, EC-10)

Recall returns a compact object per memory by default, about four keys, with
full detail only on request:

```json
"evidence": {
  "source": "tool:call_abc123",
  "trace": "mcp-5f2e…",
  "verification": "tool_observed",
  "why": "semantic 0.82, entity match"
}
```

- `source`: `tool_call_id` or `source_uri`, prefixed by kind; null when neither.
- `trace`: `trace_id`.
- `verification`: `verification_status`.
- `why`: a short reason the row was picked, from the ranking factors already
  computed. No new LLM call.

The payload must not grow noticeably: keys with no value are dropped.

## Done when

1. **Provenance**: at least 95% of recalled memories carry agent, written-at
   and a server-stamped `trace_id`.
2. **External evidence**: at least 30% of returned recall candidates carry a
   real `source_uri` or `tool_call_id`, measured over 30 days (stretch: 50%).
   "Real" excludes `source_uri = 'text-input'` (ingest's placeholder) and
   benchmark schemes such as `longmemeval:`. Baseline from a staging sample:
   organic agent writes carry ~0% `source_uri`/`run_id`. The full per-tenant
   baseline comes from the EC-16 report.

Both numbers, per tenant: `GET /api/v1/reports/evidence-coverage?window_days=30`
(tenant or admin credential). It measures the candidates recall returned in the
window when recall logging recorded any (`observability.recall_logging_enabled`
or `search_recall_logging_enabled`), and the tenant's live memories otherwise;
`denominator` says which. The excluded agent ids and `source_uri` values are
`PLACEHOLDER_AGENT_IDS` and `NON_EVIDENCE_SOURCE_URIS` in `common/constants.py`.

## Not in v1

- No ranking change: `verified_only` is a filter. A boost for verified rows
  comes later, behind a flag, after an A/B.
- No new LLM calls.
- Rows written before migration 071 stay `unverified` with NULL evidence.
  Nothing is backfilled.
