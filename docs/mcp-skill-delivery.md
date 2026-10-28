# MCP-direct skill delivery

How a Forge-produced (or human-authored) skill reaches an agent over
MCP — and the rule that gates it.

## The model

For MCP clients, **"delivery" is a read, not a push.** A skill lives as
a doc in the `skills` collection; the agent pulls it on demand through
the `caura_doc` tool it already has. There is no filesystem install,
no plugin runtime, no registration step.

```
1. Skill becomes status='active'   (approve, or auto_promote_clean)
2. Agent asks: "is there a skill for X?"
3. caura_doc op=search collection=skills query="…"
4. Server returns the matching ACTIVE skill; agent reads
   data.content (the SKILL.md body) and follows it
```

## The rule this PR adds: agents see only `active`

The Skill Factory lifecycle is `candidate → staged → active`. Before
this change, `caura_doc` returned skills regardless of status — so a
`candidate` (Forge just minted it) or `staged` (awaiting human review)
or `quarantined` (Sentinel blocked it) skill could surface to an agent.

Now, for the agent-facing `caura_doc` surface, **only `status='active'`
skills are discoverable**:

| op | behavior on `collection='skills'` |
|---|---|
| `read` | non-active doc → `Not found` (no existence leak) |
| `query` | no `status` (or `status='active'`) → scoped to `'active'`; an explicit non-active `status` → `INVALID_ARGUMENTS` (inspect those via the Skills Inbox API) |
| `search` (scoped) | `status='active'` pushed into the SQL (exact top_k) |
| `search` (broad, no collection) | non-active skill rows dropped from results |
| `write` | runs the SF-002 lifecycle validator → defaults to `staged` (see below) |
| `delete` | in a tenant that turned the Skill Factory on: non-active doc → `Not found` (atomic status guard in the DELETE WHERE) |
| `list_collections` | the `skills` count is corrected to active-only |

### Every row, every tenant

The active-only decision is made per row and applies to every tenant:
the caller's own skills, and a sibling tenant's read through
cross-tenant credentials.

## Writes go through the lifecycle, not around it

An agent *can* write a skill over MCP — but the write is not a back door
past review. The MCP `op=write` path runs the **same SF-002 validator**
(`validate_and_normalize_skill_write`) the REST route does, so an
agent-direct skill write flows through the planned lifecycle instead of
landing in an unvalidated limbo:

- **status defaults to `staged`** — the doc lands in the HITL Inbox, not
  agent-visible, until an operator approves it to `active` (or
  `auto_promote_clean` does). No caller-supplied status is needed or
  honored for promotion.
- **status RBAC** — MCP callers carry no admin/forge identity (there is
  no such accessor on this surface), so a caller-supplied
  `status='active'` / `'candidate'` / system status is **403 FORBIDDEN**.
  This is what closes self-promotion. An admin who legitimately authors
  an `active` skill uses the REST/dashboard path, which has the real
  auth context.
- **source RBAC** — `source='forge'` (internal-only) and `source='manual'`
  (admin-only) are 403'd; agents write `source='agent'`.
- **Sentinel pre-scan + content_hash + byte/slug caps** — a dirty scan
  quarantines the doc; the body/description caps and slug regex are
  enforced; `content_hash` is server-stamped, never trusted from the body.

So the full agent loop has no dead end:
`agent MCP write → staged → Inbox → approve → active → discoverable`.
The validator runs for every tenant, and an agent needs trust level 2 to
write a skill at all.

This is the single mechanism that turns *"an operator approved it"*
into *"agents can find it."* Approval (or `auto_promote_clean`) is what
flips a skill to `active`; this filter is what makes `active` mean
discoverable.

## The same for every tenant

The filter and the write validator apply whatever
`org_settings.skills_factory.enabled` says: the flag turns the Forge on,
not the review. A tenant that never opted in kept the skills it had:
migration 070 marked its status-less skills `active`. What changes for
such a tenant is that a skill an agent writes now lands `staged` and
waits for an org admin in the Inbox, and agents read only `active`
skills. `op=delete` still applies its status guard only for a tenant
that opted in.

## Operators inspect non-active skills via the Inbox, not MCP

The agent-facing MCP surface is intentionally active-only with **no
admin bypass**. An operator who needs to see `staged` / `candidate` /
`quarantined` skills uses the HITL Inbox API
(`/api/v1/skills-inbox/*`) — that's its purpose. Keeping the bypass out
of the MCP path means there's exactly one way for a non-active skill to
reach an agent: it doesn't.

## The hard ceiling (why this is the baseline, not the whole story)

MCP delivery is **pull** — the agent must *decide* to call
`caura_doc`. We can raise that probability (tool-description framing,
keystones, folding skill-search into the recall agents already do) but
never guarantee it: anything reached through a tool is an agent
decision.

To make skill use *reliable* rather than probabilistic, a skill must be
installed into the harness's own startup surface (its skill registry /
filesystem / system-prompt) so it's present before the model thinks —
which is necessarily per-harness and can't be done from the Caura
side alone.

So the tiers are:

- **MCP-direct (this PR)** — universal, zero-integration, *probabilistic*.
  Works on any MCP-capable harness; the agent has to choose to look.
- **Per-harness install** — a reliability layer for harnesses we
  integrate deeply (writing `<harness>/skills/<slug>/SKILL.md` so the
  skill is present before the model thinks). Guarantees presence;
  requires a harness-specific adapter. **OpenClaw is the first such
  harness** (the Caura plugin reconciles the catalog to disk every
  heartbeat).

They're complementary: MCP-direct is the floor that works everywhere;
install is the guarantee for chosen harnesses.

### The push path is gated by the same rule

Per-harness install is a **push** (catalog → node disk → native loader),
the mirror of MCP's pull. It must enforce the *same* active-only gate,
or `candidate` / `staged` / `quarantined` skills would land on every
node's disk even though MCP hides them.

So the OpenClaw plugin reconciler pulls from a dedicated server surface,
**`POST /api/v1/skills/installable`**, instead of a raw
`/documents/query`:

- every tenant → only `status='active'` skills (migration 070 marked the
  skills a tenant that never opted in already had `active`, so none left
  a node);
- the reconciler fails *safe* on a non-2xx (preserves on-disk skills,
  writes nothing), so an outage never pushes a non-active skill to disk.

The policy lives entirely server-side — the plugin sends no `status`
filter, so it can't be made to pull a non-active skill. A skill rejected while active (`active → rejected`) drops
out of `installable` and the reconciler removes it from disk on the next
tick. Push (OpenClaw) and pull (`caura_doc`) now agree on exactly what
an agent may see.

### Verifying installs reached the fleet

Gating decides what *should* land; observability confirms what *did*.
Each heartbeat carries the reconciler's summary — `installed` (the
active skills converged onto that node's disk right now), plus per-tick
`added` / `removed` / `skipped` / `collisions` / `protected` deltas —
which the backend stores as the latest snapshot at
`nodes.metadata.reconcile` and surfaces via `GET /api/v1/fleet/nodes`. So
an operator who flips a skill to `active` can confirm it actually reached
each node, and see *why* a malformed catalog row was skipped — closing
the loop from "approved" to "installed on the fleet."

`installed` is the standing truth, not a delta: it's reported on every
tick (even steady-state ticks with empty `added`/`removed`), so a node's
current live-skill set is always legible.

When more than one target dir is configured (see below), those top-level
arrays are deduped *across* targets. The summary also carries a
`targets[]` array with a **per-target** breakdown — one entry per dir
(`{ dir, mode, installed, added, removed, collisions, protected }`) — so
an operator can see exactly *which* dir a skill landed in or collided in.
For the default single-target case it's one entry mirroring the top-level
arrays. A `registeredDirs[]` array lists the target dirs Caura has
ensured are on OpenClaw's skill load path this tick (the `register: true`
opt-in below); it's empty unless a target opts in.

### Reconcile targets: `owned` vs `additive`

By default the reconciler manages one **`owned`** dir — the plugin's own
`skills/` — where it has full authority: anything on disk not in the
catalog is pruned. Operators can add extra target dirs via the
`CAURA_SKILL_TARGETS` env var (JSON array of `{ dir, mode, register? }`):

- **`owned`** — fully Caura-managed (destructive prune). Use only for
  dirs Caura exclusively controls.
- **`additive`** — a **shared/foreign** dir. Caura writes its active
  skills there but **only ever touches entries it wrote**, tracked by a
  per-skill ownership marker file:
  - a slug already occupied by an *unowned* skill is a **collision** —
    skipped, never overwritten (reported in the summary's `collisions`
    list, kept distinct from the catalog-shape `skipped` list);
  - an unowned skill is **never removed**, even on an empty catalog;
  - only marker-bearing entries are updated/pruned.

This makes the "empty catalog / wrong tenant wipes the dir" hazard apply
only to `owned` dirs — `additive` dirs lose only Caura's own entries,
never the client's.

#### Reaching agents: `register`

The plugin's own `owned` dir is already published as a plugin skill, so it
reaches agents automatically. An **extra** target dir (typically
`additive`) is *not* on OpenClaw's skill load path by default — its skills
land on disk but stay invisible. Set **`register: true`** on the entry and
the reconciler adds that dir to **`skills.load.extraDirs`** in
`~/.openclaw/openclaw.json` — OpenClaw's documented, watched mechanism for
extra skill directories (`docs/tools/skills-config.md`; consumed by the
skills-snapshot refresh). The write is append-only and idempotent (existing
entries preserved; written only when the dir is newly added), and fail-safe
(a missing/unreadable config is logged, never fatal to the heartbeat). The
owned dir is never registered this way.

`skills.load.watch` defaults to `true`, so OpenClaw picks up a newly
registered dir and refreshes its skills snapshot without a restart.
**But** a long-lived agent *session* keeps its cached `<available_skills>`
snapshot — a session that was already running won't see the newly
registered skills until a fresh session starts (new `--session-key`).

## What makes a skill findable: the summary

Scoped `op=search` ranks on the embedded `data.summary`. So the
Forge-distilled `summary` IS the discoverability surface — it should
read like a trigger ("Use when deploying to eu-west…"), not a label
("Deployment skill"). Same embedding path as hand-authored skills
(`doc_indexing.resolve_embed_source`).

## Related

- `core-api/src/core_api/mcp_server.py` — `caura_doc` handler (the pull filter)
- `core-api/src/core_api/routes/documents.py` — `POST /skills/installable` (the push filter)
- `plugin/src/reconcile-skills.ts` — the OpenClaw reconciler that consumes it
- `core-storage-api/src/core_storage_api/routers/documents.py` — `search(status=…)` mechanism
- `docs/operator-forge-cron.md` — how skills reach `active` autonomously
