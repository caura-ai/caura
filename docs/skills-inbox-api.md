# Skill Factory · Skills Inbox REST API

The Skills Inbox is the operator-facing, human-in-the-loop surface of the
Skill Factory. Forge mints skill *candidates*; the lifecycle promoter
flows clean ones to `staged`; the Inbox is where a human decides what
actually goes `active`. Every Inbox action is a status transition (or,
for Edit, a content revision) on the underlying skill doc in the
`skills` collection — there is no separate inbox store.

Backend source: `core-api/src/core_api/routes/skills_inbox.py`.

## Auth & prerequisites

**Feature flag.** Every endpoint requires
`org_settings.skills_factory.enabled = true` for the tenant. When the
flag is off, all endpoints return `403 SKILLS_FACTORY_DISABLED` (a
deliberate, explicit error rather than a silent 404).

**Transport.**

- **Browser clients** (the Inbox UI) authenticate with the session
  cookie; mutating `POST` calls must carry the CSRF token issued with
  the session.
- **Scripts / CLI** authenticate with a Bearer JWT:
  `Authorization: Bearer <token>`.
- **Self-hosted / standalone** deployments that hit core-api directly
  use the `X-API-Key` header (admin key or `CAURA_API_KEY`); in
  standalone mode the resolved context is already `orgRole=admin`. The
  admin key is tenant-less, so it has to name the tenant it is acting
  on — see below.

**Authorization.** The five action endpoints
(`approve`/`defer`/`edit`/`quarantine`/`reject`) require an admin
caller — `orgRole=admin` (or the admin API key). A non-admin caller
gets `403 SKILLS_INBOX_FORBIDDEN`. The `GET` list endpoint is readable
by any authenticated tenant member, so non-admin operators can still
see what's in flight.

**Which tenant's inbox.** Every endpoint — the list and all five
actions — accepts an optional `?tenant_id=` query parameter, and how it
resolves depends on the credential:

- A **tenant-scoped** credential carries its own tenant, and that tenant
  always wins. Omit the parameter, or pass the matching value; passing a
  *different* tenant is a `403` (`TENANT_MISMATCH`) — a tenant key may
  not act on someone else's inbox.
- An **admin** credential (the admin API key, or standalone mode) carries
  no tenant of its own, so it must name one: `?tenant_id=acme`. Omitting
  it is a `400` — the credential authenticated fine, the request just
  didn't say which inbox to open.
- A context with neither a tenant nor admin rights is genuinely
  unauthenticated and still gets `401`.

## Base path

```
/api/v1/skills-inbox
```

## Slug encoding — read this before scripting

Forge-minted slugs contain a literal `/` — e.g. `forge/abc-123` — and
the backend routes actions with a FastAPI **path converter**
(`{slug:path}`), which matches across raw slashes.

Encode **per segment** and keep the slashes raw:

```
✅ POST /api/v1/skills-inbox/forge/abc-123/approve
❌ POST /api/v1/skills-inbox/forge%2Fabc-123/approve   # breaks routing
```

In JavaScript: `slug.split("/").map(encodeURIComponent).join("/")` —
never `encodeURIComponent(slug)` on the whole thing. The `slug` field
returned by the list endpoint is always the full doc id (including the
`forge/` prefix) and is exactly what the action endpoints expect.

## List the inbox

```
GET /api/v1/skills-inbox?limit=50
```

Query parameters:

| Param | Default | Notes |
|---|---|---|
| `limit` | `50` | 1–200. Additionally capped by `org_settings.skills_factory.inbox_max_pending`. |
| `fleet_id` | – | Optional; narrow the list to one fleet. |
| `tenant_id` | – | Required for an admin credential, optional (and must match) for a tenant-scoped one — see [Auth & prerequisites](#auth--prerequisites). |
| `include_content` | `false` | Include the full SKILL.md body on each card. The default list is lean (`content: null`); the edit UI opts in. |

Returns the tenant's `status='staged'` skill cards, newest first.
Cards an operator has **deferred** carry a `deferred_at` timestamp and
sort to the bottom of the page so fresh candidates always surface
first. (`candidate` and `quarantined` docs do not appear here —
candidates are Forge/promoter territory, and quarantined docs live in
the security-review queue.)

Response shape (this example was requested with
`?include_content=true`; by default `content` is `null`):

```json
{
  "tenant_id": "acme",
  "fleet_id": null,
  "count": 1,
  "truncated": false,
  "items": [
    {
      "slug": "forge/summarize-oncall-handoff",
      "doc_id": "forge/summarize-oncall-handoff",
      "name": "Summarize on-call handoff",
      "description": "Produce the standard handoff summary…",
      "summary": "Turns the last on-call window into the 5-section handoff format.",
      "content": "# Summarize on-call handoff\n\n1. Pull the window…",
      "domain": "ops",
      "tags": ["oncall", "handoff"],
      "source": "forge",
      "kind": "create",
      "status": "staged",
      "fingerprint": "c3f1…",
      "scan_state": "clean",
      "scan_critical": 0,
      "scan_warn": 1,
      "sentinel_scan": {
        "status": "clean",
        "critical_count": 0,
        "warning_count": 1,
        "findings": [
          { "code": "S-STYLE-001", "severity": "warn", "message": "description exceeds recommended length", "fatal": false, "locator": "description" }
        ]
      },
      "forge_evidence": { "cluster_size": 5, "distinct_agents": 4 },
      "origin": {
        "agent_id": "forge",
        "run_id": "forge-cron-acme-20260718T0600",
        "cluster_size": 5,
        "distinct_agents": 4,
        "window_end": "2026-07-18T06:00:00+00:00"
      },
      "evidence": "Five agents repeated this procedure successfully…",
      "cites": ["b7e2…", "91cc…"],
      "created_at": "2026-07-18T06:02:11+00:00",
      "updated_at": "2026-07-18T07:00:00+00:00",
      "content_hash": "sha256:9b2e…",
      "target": null,
      "deferred_at": null
    }
  ]
}
```

Field notes:

- `slug` — the **full** doc id (with the `forge/` prefix where
  present). Feed it back verbatim to the action endpoints.
- `content` — the full SKILL.md body, included **only when the request
  passes `?include_content=true`** (the default list is lean; bodies are
  bounded by `skills_factory.body_max_bytes`, 40 KB default, × the page
  limit). The list is the only inbox read surface (there is no per-slug
  `GET`), so the edit UI opts in to pre-fill the Edit form; it's what an
  Edit must send back (see below).
- `sentinel_scan` — the latest Sentinel verdict:
  `{ status, critical_count, warning_count, findings }` where `status`
  is `clean` / `quarantined` / `failed`, or `null` when the doc was
  never scanned. `findings` carries up to 20 entries
  (`{ code, severity, message, fatal, locator? }`) so the UI can show
  WHY a scan tripped; the full list stays on the doc. The flat
  `scan_state` / `scan_critical` / `scan_warn` fields carry the same
  counts (kept for older consumers).
- `truncated` (top-level) — `true` when more staged cards exist than
  this page returned (the effective limit —
  `min(limit, inbox_max_pending, 200)` — cut the list). Narrow by
  `fleet_id` or raise `skills_factory.inbox_max_pending` to see more.
- `forge_evidence` — `{ cluster_size, distinct_agents }`: how many
  behavior traces the candidate was distilled from and how many
  distinct agents produced them. Only present on `source: "forge"`
  cards; the same counters also appear in `origin`.
- `evidence` — Forge's human-readable rationale for minting the
  candidate: a string on Forge cards (hand-authored docs may carry a
  structured object), and an **empty object `{}` when absent** —
  never `null` on the wire.
- `cites` — memory-ID provenance: the memories the skill was
  distilled from. Empty for hand-authored docs.
- `kind` / `target` — `create` for a brand-new skill; `update`
  candidates carry a `target` binding to the live skill they revise.
- `deferred_at` — set when an operator deferred the card; deferred
  cards sort to the bottom.

## Actions

All five are `POST /api/v1/skills-inbox/{slug}/<action>` and return
the same response shape:

```json
{ "slug": "forge/abc-123", "previous_status": "staged", "new_status": "active", "detail": null }
```

All five take the same `?tenant_id=` selector as the list endpoint, on
the same terms — an admin credential must name a tenant, a tenant-scoped
one may only name its own.

### `approve` — staged → active

Empty body. Runs a **pre-apply Sentinel rescan** against the exact
content being crystallized; the transition is refused (`422`) unless
the rescan comes back **clean with zero critical findings** (a dirty
or quarantine-grade verdict blocks it). A staged doc missing its
`content_hash` also refuses with `422` (fail closed). Concurrency is
guarded: if the doc is edited or transitioned by someone else while
the approve is in flight, the call returns `409` — reload and retry.

```bash
curl -X POST "$BASE/api/v1/skills-inbox/forge/abc-123/approve" \
  -H "Authorization: Bearer $TOKEN"
```

### `defer` — stash for later (stays `staged`)

Body optional: `{ "reason"? }`, or no body at all. Defer does **not** change the
status — it stamps `deferred_at` so the card sorts to the bottom of
the inbox and Forge may revise the candidate on a later run. Editing
or approving the card clears the defer marker.

### `edit` — revise content (staged only)

Body: `{ "description"?, "summary"?, "content"? }` — at least one
field required (`422` otherwise). Raw markdown only. The edit re-runs
the same validator as the original write: the content is rehashed and
**Sentinel rescans it**; if the scan finds critical content (prompt
injection, shell injection, …) the doc is **quarantined** instead of
staying staged — check `new_status` in the response. Otherwise the doc
stays `staged` with a fresh `content_hash`.

### `quarantine` — staged or candidate → quarantined

Body: `{ "reason" }` (required). Parks the skill for security review.
Reversible — quarantine does **not** write to the Forge cooloff
ledger; a security admin can still reject or (via the lifecycle)
restore it.

### `reject` — staged, candidate, or quarantined → rejected

Body: `{ "reason" }` (required), plus optional `cooloff_days` (1–365).
**Permanent.** Rejecting also writes the candidate's cluster
fingerprint to the Forge cooloff ledger
(`forge_rejected_fingerprints`), so Forge will not re-derive the same
skill for `cooloff_days` — default
`org_settings.skills_factory.rejection_cooloff_days` (30 days).

```bash
curl -X POST "$BASE/api/v1/skills-inbox/forge/abc-123/reject" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reason": "duplicate of an existing runbook skill"}'
```

## Status / action matrix

| Current status | `approve` | `defer` | `edit` | `quarantine` | `reject` |
|---|---|---|---|---|---|
| `staged` | ✅ → `active` | ✅ stays `staged` (marked deferred) | ✅ stays `staged` (or → `quarantined` if the rescan trips) | ✅ → `quarantined` | ✅ → `rejected` |
| `candidate` | ❌ 409 | ❌ 409 | ❌ 409 | ✅ → `quarantined` | ✅ → `rejected` |
| `quarantined` | ❌ 409 | ❌ 409 | ❌ 409 | ❌ 409 | ✅ → `rejected` |
| `active` / `rejected` / other | ❌ 409 | ❌ 409 | ❌ 409 | ❌ 409 | ❌ 409 |

## Typical operator workflow

1. **List** — `GET /api/v1/skills-inbox`. Fresh staged cards are at
   the top; anything you deferred earlier is at the bottom.
2. **Triage a card.** Read `summary`, `content`, and the Forge
   evidence (`origin.cluster_size`, `origin.distinct_agents`,
   `evidence`). Check the scan verdict: `scan_state` should be
   `clean` and `scan_critical` should be `0`.
3. **Good as-is?** `POST …/{slug}/approve`. The pre-apply rescan runs
   automatically; on success the skill is `active` and starts being
   delivered to agents (MCP search/read and the plugin reconciler —
   see the delivery doc below).
4. **Almost right?** `POST …/{slug}/edit` with the corrected
   `content` / `description` / `summary`, verify the response says it
   stayed `staged` with a clean rescan, then approve.
5. **Not sure yet?** `POST …/{slug}/defer` — the card drops to the
   bottom and Forge may refine the candidate on its next run.
6. **Suspicious content?** `POST …/{slug}/quarantine` with a reason —
   parks it for security review without poisoning the cluster.
7. **Never want it?** `POST …/{slug}/reject` with a reason — the
   cluster fingerprint goes on cooloff so Forge stops re-minting it.

## Error codes

| Code | Meaning |
|---|---|
| `400` | An admin credential didn't say which inbox to open — add `?tenant_id=`. |
| `401` | Missing/invalid credentials: the context has neither a tenant nor admin rights. An admin key that simply omitted `tenant_id` gets `400`, not this. |
| `403` | `SKILLS_FACTORY_DISABLED` (feature flag off for the tenant), `SKILLS_INBOX_FORBIDDEN` (action attempted by a non-admin), or `TENANT_MISMATCH` (a tenant-scoped credential named a different tenant in `?tenant_id=`). |
| `404` | No skill doc with that slug in the tenant's `skills` collection. Check slug encoding first — an over-encoded `%2F` routes to a nonexistent path. |
| `409` | Action not permitted from the doc's current status (see matrix), or the doc was concurrently transitioned/edited while your call was in flight — reload the inbox and retry. |
| `422` | Missing/invalid body field (e.g. `reject` or `quarantine` without `reason`, `edit` with no fields), an approve whose pre-apply rescan refused, or a malformed doc (no `content_hash` / no cluster fingerprint). |

## Related

- [`mcp-skill-delivery.md`](mcp-skill-delivery.md) — how an approved
  (`active`) skill actually reaches agents over MCP, and the
  active-only visibility rule.
- [`operator-forge-cron.md`](operator-forge-cron.md) — scheduling the
  Forge worker that fills this inbox.
