# Evidence contract v1: where a memory came from, and whether anyone checked it

Status: in progress. Done: EC-04 (this spec), EC-07 (schema, migration 071),
EC-08 (write path, server-stamped `trace_id`), EC-09 (server-owned
verification fields), EC-10/EC-12 (the `evidence` object on recall, with its
`why`), EC-11 (the `verified_only` filter), EC-13 (automatic states), EC-14
(the verify operation).

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
- Read: `MemoryOut` (write responses, list) and `GET /memories/{id}` return
  all six at the top level. Search and recall hits carry them inside
  `evidence` instead (below), so a hit does not pay for them twice.

## Verification state machine

```
            ┌──────────────► tool_observed ──┐
unverified ─┤                                ├──► disputed
            └──────────────► human_verified ─┘
                 corroborated (reserved, not set in v1)
```

- `unverified`: every new row. The default; nothing about it was checked.
- `tool_observed`: the content records a tool's result the server can point
  at: a `tool_call_id`, or a real `source_uri` (not ingest's `text-input`
  placeholder, not a benchmark scheme: `longmemeval:`, `locomo:`, `stale:`,
  `clbench:`, `test:`, `lme:`, `bench:`, `seed:`, `benchmark:`;
  `common.models.memory.is_real_source_uri`, the `NON_EVIDENCE_SOURCE_URIS`
  rule the coverage report counts with). Set on write by EC-13, not by
  the writer.
- `human_verified`: a person, or an agent at `trust_level >= 3`, confirmed it
  through the verify operation. Audited.
- `disputed`: a later contradiction did (the row became `conflicted`). v1 has
  no "mark it wrong" operation; a transition to `conflicted` disputes too.
- `corroborated`: reserved for "independent sources agree". No v1 path sets it.

### How the state moves (EC-13)

Server-side only, and no LLM call. Storage applies the rules in the same
UPDATE that changes the row (`postgres_service._verification_writes`,
`verification_on_status_change`), on every status write: the single and batch
status routes, the retraction path and a by-id PATCH.

| Event | `verification_status` | `verified_by` / `verified_at` |
|---|---|---|
| Write (REST, bulk item, MCP, auto-chunk child, atomic fact) | `tool_observed` if the row carries a `tool_call_id` or a real `source_uri`, else `unverified`. A derived row is computed from the source and tool call it copies from its parent, so it starts where its parent started. | NULL |
| Status becomes `conflicted` | `disputed` | kept (who had verified it) |
| A `disputed` row becomes `active`, `pending` or `confirmed` (a retraction, a person's transition) | restored, not stored: `human_verified` if `verified_by` is set, else the row's evidence state | kept |
| Any other status (`outdated`, `archived`, `cancelled`, ...) | kept: the status already says the row is old | kept |
| Content edited | the evidence state (`disputed` stays `disputed`): a person checked other text | cleared |
| `source_uri` or `tool_call_id` edited | recomputed from the new evidence, a person's verification kept (`disputed` stays) | kept |

Rows written before migration 071 are not backfilled; a status change alone
does not recompute them, except a dispute's resolution, which restores from the
row's own fields.

### The verify operation (EC-14)

- REST: `POST /memories/{id}/verify` and `POST /memories/{id}/unverify`
  (`?tenant_id=`). MCP: `caura_manage` op `verify` / `unverify` (ops on the
  existing tool, not a new one). The plugin does not offer them yet.
- `verify` sets `human_verified`, `verified_by` (a person's user id,
  `agent:<id>`, or the credential kind such as `tenant_key`) and
  `verified_at = now`. `unverify` clears `verified_by`/`verified_at` and
  returns the row to its evidence state; a `disputed` row stays `disputed`.
- Who: a signed-in person (admin or member), the admin key, a tenant key (the
  OSS contract: tenant key = tenant admin), or an agent with
  `trust_level >= 3` that may write the row (the cross-fleet / `scope_agent`
  check transitions use). A lower-trust or unregistered agent gets 403
  `AGENT_TRUST_TOO_LOW` / `AGENT_NOT_REGISTERED` naming the bar; an install or
  cross-tenant credential gets 403 `CREDENTIAL_KIND_FORBIDDEN`; a read-only
  credential is refused as on every write.
- A `disputed` memory: only a person or an admin/tenant credential may verify
  it. Settling a contradiction the pipeline found is a person's call, so an
  agent gets 403 `AGENT_CREDENTIAL_FORBIDDEN` whatever its trust. `unverify`
  stays open to a trusted agent on any row: it only lowers what readers
  believe.
- Every call writes an audit entry (`memory_verify` / `memory_unverify`,
  resource the memory, detail: old and new state, actor, owner, surface).

## Who may set what

| Field | Writer (REST, MCP, SDK, broker) | Server |
|---|---|---|
| `observed_at`, `tool_call_id` | yes, on create | copies to derived rows |
| `trace_id` | yes, on create | stamps when absent |
| `verification_status`, `verified_by`, `verified_at` | never: a create, a bulk item or a PATCH naming one is refused with a message saying the server sets it | the verify operation (dashboard users, agents with `trust_level >= 3`, audited) and EC-13's automatic states |

The refusal is the C6 one `supersedes_id` already gets (`SERVER_OWNED_MEMORY_FIELDS`
in `core-api/src/core_api/schemas.py`): a 422 on single write and PATCH, a
per-item `error` on bulk.

## The `evidence` object on recall (EC-10, EC-12)

Every hit of `POST /search`, `POST /recall` and MCP `caura_recall` carries a
compact object, four keys at most:

```json
"evidence": {
  "source": "tool:call_abc123",
  "trace": "mcp-5f2e…",
  "verification": "tool_observed",
  "why": "vector + keyword match"
}
```

- `source`: `tool:<tool_call_id>` when the writer named the tool call, else
  `source_uri` as written (a URL, `upload:<name>`); absent when neither.
  Ingest's `text-input` placeholder is not a source.
- `trace`: `trace_id`.
- `verification`: `verification_status`, always present (`unverified` is the
  answer most rows give, and the one an agent most needs to see).
- `why` (EC-12): at most 40 characters, built from the score factors and the
  retrieval strategy: the signals that matched (`vector`, `keyword`, `entity`)
  plus at most one qualifier (`reranked`, `in time range`, `recent`,
  `often recalled`). `entity match` for the entity short-circuit,
  `newest version of a recalled fact` for an injected successor. No LLM.

Keys with no value are left off. `diagnostic=true` (the existing D12 flag)
widens the object with `observed_at`, `run_id`, `tool_call_id`, `source_uri`,
`verified_by`, `verified_at` and the retrieval `strategy`; `score_parts` stays
where it already is, on the hit. The six evidence columns are not repeated at
the top level of a hit. `tests/test_recall_evidence.py` pins the compact size.

## `verified_only` (EC-11)

`verified_only: true` on `/search`, `/recall` and `caura_recall` keeps rows
whose `verification_status` is `tool_observed`, `corroborated` or
`human_verified`. It is a storage filter beside fleet, type and status, applied
by the scored search, the entity short-circuit and the successor lookup, so an
unverified correction is not injected into a verified-only result. Ranking is
unchanged; the survivors keep their order.

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
