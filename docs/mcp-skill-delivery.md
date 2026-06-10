# MCP-direct skill delivery

How a Forge-produced (or human-authored) skill reaches an agent over
MCP — and the rule that gates it.

## The model

For MCP clients, **"delivery" is a read, not a push.** A skill lives as
a doc in the `skills` collection; the agent pulls it on demand through
the `memclaw_doc` tool it already has. There is no filesystem install,
no plugin runtime, no registration step.

```
1. Skill becomes status='active'   (approve, or auto_promote_clean)
2. Agent asks: "is there a skill for X?"
3. memclaw_doc op=search collection=skills query="…"
4. Server returns the matching ACTIVE skill; agent reads
   data.content (the SKILL.md body) and follows it
```

## The rule this PR adds: agents see only `active`

The Skill Factory lifecycle is `candidate → staged → active`. Before
this change, `memclaw_doc` returned skills regardless of status — so a
`candidate` (Forge just minted it) or `staged` (awaiting human review)
or `quarantined` (Sentinel blocked it) skill could surface to an agent.

Now, for the agent-facing `memclaw_doc` surface, **only `status='active'`
skills are discoverable**:

| op | behavior on `collection='skills'` |
|---|---|
| `read` | non-active doc → `Not found` (no existence leak) |
| `query` | `status='active'` forced (overrides any caller-supplied status) |
| `search` (scoped) | `status='active'` pushed into the SQL (exact top_k) |
| `search` (broad, no collection) | non-active skill rows dropped from results |
| `write` | runs the SF-002 lifecycle validator → defaults to `staged` (see below) |
| `delete` | non-active doc → `Not found` (atomic status guard in the DELETE WHERE) |
| `query` | `status='active'` scoped in; an explicit non-active `status` → 422 (use the Inbox) |
| `list_collections` | the `skills` count is corrected to active-only |

### The filter follows the *owning* tenant, not just the caller

The active-only decision is made per-row against the row's own
`tenant_id`, not solely the caller's opt-in flag. A skill is hidden when
it's non-active AND **either** the caller's tenant opted in **or** the
row belongs to a different tenant. The second arm closes a cross-tenant
leak: a caller whose own tenant hasn't opted in must still never see a
*sibling* tenant's in-flight skill through cross-tenant credentials.
This is safe — a non-opted-in owning tenant's skills are all `active`
(migration 022 backfill), so the cross-tenant arm only ever hides a
genuinely non-active row, never a legitimately visible one.

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
The validator runs only for opted-in tenants; non-opted-in tenants keep
byte-identical legacy write behavior.

This is the single mechanism that turns *"an operator approved it"*
into *"agents can find it."* Approval (or `auto_promote_clean`) is what
flips a skill to `active`; this filter is what makes `active` mean
discoverable.

## Gated on opt-in — zero change for non-opted-in tenants

The filter applies **only when `org_settings.skills_factory.enabled=true`**.
A tenant that hasn't opted in sees every skill exactly as before (their
skills are all `active` anyway — backfilled by migration 022 — so the
filter would be a no-op, but we skip it entirely to guarantee
byte-identical behavior). This preserves the merge-day invariant the
whole Skill Factory has held: **merging changes nothing until a tenant
explicitly opts in.**

## Operators inspect non-active skills via the Inbox, not MCP

The agent-facing MCP surface is intentionally active-only with **no
admin bypass**. An operator who needs to see `staged` / `candidate` /
`quarantined` skills uses the HITL Inbox API
(`/api/v1/skills-inbox/*`) — that's its purpose. Keeping the bypass out
of the MCP path means there's exactly one way for a non-active skill to
reach an agent: it doesn't.

## The hard ceiling (why this is the baseline, not the whole story)

MCP delivery is **pull** — the agent must *decide* to call
`memclaw_doc`. We can raise that probability (tool-description framing,
keystones, folding skill-search into the recall agents already do) but
never guarantee it: anything reached through a tool is an agent
decision.

To make skill use *reliable* rather than probabilistic, a skill must be
installed into the harness's own startup surface (its skill registry /
filesystem / system-prompt) so it's present before the model thinks —
which is necessarily per-harness and can't be done from the MemClaw
side alone.

So the tiers are:

- **MCP-direct (this PR)** — universal, zero-integration, *probabilistic*.
  Works on any MCP-capable harness; the agent has to choose to look.
- **Per-harness install (future)** — a reliability layer for harnesses
  we integrate deeply (e.g. writing `~/.claude/skills/<slug>/SKILL.md`).
  Guarantees presence; requires a harness-specific adapter.

They're complementary: MCP-direct is the floor that works everywhere;
install is the guarantee for chosen harnesses.

## What makes a skill findable: the summary

Scoped `op=search` ranks on the embedded `data.summary`. So the
Forge-distilled `summary` IS the discoverability surface — it should
read like a trigger ("Use when deploying to eu-west…"), not a label
("Deployment skill"). Same embedding path as hand-authored skills
(`doc_indexing.resolve_embed_source`).

## Related

- `core-api/src/core_api/mcp_server.py` — `memclaw_doc` handler (the filter)
- `core-api/src/core_api/repositories/document_repository.py` — `search(status=…)` mechanism
- `docs/operator-forge-cron.md` — how skills reach `active` autonomously
