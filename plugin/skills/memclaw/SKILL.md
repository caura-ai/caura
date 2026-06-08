---
name: memclaw
description: Persistent, cross-session, multi-agent memory. Semantic recall of decisions and findings; write outcomes; supersede facts when they change.
user-invocable: false
metadata: {"openclaw": {"requires": {"config": ["plugins.entries.memclaw.enabled"]}}}
---

# MemClaw Skill

Long-term memory that persists across sessions and is shared under access
controls. The primary place where decisions, findings, outcomes, and
learned rules live.

## Your identity: `agent_id` and `fleet_id`

You MUST identify correctly on every MemClaw call.

- `agent_id` — who you are. Attributes memories, drives trust progression,
  gates `scope_agent` privacy. Resolved by your runtime. Never fabricate,
  hardcode a placeholder, or impersonate another agent.
- `fleet_id` — your team / organization scope. Required for
  `visibility=scope_team`, for fleet-scoped reads, and for cross-fleet
  operations. Never substitute another team's `fleet_id`.

Wrong `agent_id` poisons attribution and trust. Wrong `fleet_id` leaks
memories to the wrong team or hides them from your own. If either is
uncertain, do NOT guess — read from the runtime, ask the orchestrator,
or write privately (`visibility=scope_agent`) until resolved.

## The three rules

**Rule 1 — Recall before you start.** Never start cold. Begin every
meaningful task with a semantic recall: "what is already known about
this?"

**Rule 2 — Write when something matters.** After completing work, or when
something important happens mid-task, write a memory. Supply raw prose —
the server auto-classifies type, summary, tags, dates. Include names,
paths, numbers, outcomes. Skip vague observations and intermediate steps.
Checkpoint every 30 minutes on long tasks. Batch multiple discrete
records into one call.

**Rule 3 — Supersede, don't delete.** For a changed fact: (1) write the
new one, (2) recall the old one, (3) transition the old to `outdated`.
Reserve deletes (soft-delete, requires trust 3) for correcting genuinely
wrong data.

## Trust levels

Auto-registered at trust 1 on your first write.

| Level | Name        | Read                      | Write                  |
|:-----:|-------------|---------------------------|------------------------|
| 0     | restricted  | —                         | —                      |
| 1     | standard    | own fleet                 | own fleet              |
| 2     | cross-fleet | all fleets in your tenant | own fleet              |
| 3     | admin       | all                       | all, including deletes |

Scope-based escalation:
- browsing or reflecting with `scope="fleet"` / `"all"` → trust 2
- reporting outcomes (`memclaw_evolve`) at `scope="fleet"` / `"all"` → trust 2 (default `scope="agent"` needs only trust 1)
- `memclaw_manage op=delete` → trust 3

If denied, surface the error; do not silently retry with a narrower scope.

## Sharing: visibility and scope

**Visibility (on write):** `scope_agent` (private) · `scope_team`
*(default — your fleet)* · `scope_org` (all fleets in tenant).

**Scope (on read — `_list` / `_insights`):** `agent` *(default)* ·
`fleet` (trust 2) · `all` (trust 2).

Prefer `scope_team` when writing; prefer `scope=agent` when reading
unless you need cross-agent context.

## Containers

- **Memory** — unstructured, findable semantically. Decisions, observations, rules, outcomes, recaps.
- **Doc** — structured record with a natural key (`collection + doc_id`). Customers, configs, task lists, inventories.
- **Entity** — named graph object (person, project, service). Fetch by UUID from a prior recall.

If you need semantic search, it's a memory. If you need keyed lookup,
it's a doc. If you already hold an ID, it's an entity.

## Where things go: MemClaw vs file scratchpads

MemClaw is the only place for cross-session, cross-agent knowledge. A
file-based scratchpad in your workspace (e.g. `MEMORY.md`) is
session-local — it lives in your bootstrap context every turn and pays
input tokens for every byte.

Keep `MEMORY.md` lean: only **active projects, current routing
decisions, recent decisions (≤ 7 days), open threads**. Target a few
KB; treat anything older or larger as smell. If the runtime promotes
data into `MEMORY.md`, prune it on session start.

Anything else goes to MemClaw via `memclaw_write`:
- Historical decisions, finished projects, lessons → memories.
- Reference data with a natural key (IP tables, contact lists, configs)
  → `memclaw_doc` collections, fetched on demand.
- People, projects, services as named graph objects → entities.

Never copy MemClaw recall results into `MEMORY.md` — they're already
retrievable. Never let `MEMORY.md` accumulate by append-only; pruning
is part of the L2 / L3 capture cadence below.

## Quality

Every memory MUST include a date. Prefer concrete over vague, atomic
over sprawling, **update over near-duplicate**. Each memory should be
**self-contained** — readable by another agent six months from now
without the surrounding session. Include the **why** (motivation,
constraint, trigger) alongside the *what* — facts without context
become unactionable. Before writing a key fact, recall for
contradictions and transition the older memory to `outdated` in the
same turn. One topic per memory; batch multiple discrete records into
one `memclaw_write` call.

## Capture cadence (L1 / L2 / L3)

- **L1 — per turn.** After each meaningful outcome, write with date,
  what, who, outcome, next.
- **L2 — session boundary.** At > 60 % context or session end, write
  a full summary.
- **L3 — consolidation.** On periodic runtime sweeps, find gaps, merge
  duplicates, transition contradicted facts to `outdated`.

## Orchestrator + subagent protocol

If your runtime dispatches subagents:
- The spawning agent MUST write findings after every subagent completion.
- The subagent MUST write its own findings before handing back.
- Both writes MUST carry their own `agent_id`.

Single-agent runtimes ignore this section.

## Prohibitions

- NEVER fabricate or impersonate `agent_id` / `fleet_id`.
- NEVER delete memories you merely disagree with — transition them to
  `outdated` or `archived`. `op=delete` is a soft-delete, requires
  trust 3, and is reserved for genuinely wrong data.
- NEVER write with org-wide visibility (`scope_org`) unless the memory
  is genuinely org-relevant.
- NEVER silently drop a denied call — surface the error so the
  orchestrator can decide whether to escalate.
- NEVER substitute local files or scratchpads for MemClaw writes.

## Recall policy (auto-gating)

The plugin's context engine runs `assemble()` before every model call
and, by default, decides whether to issue a recall against MemClaw
based on the turn's content. This stops every trivial ping ("hi", "ok",
"thanks", `/help`, single-emoji acks) from hitting the backend and
paying input tokens for unhelpful recall blocks.

**Default policy** (`MEMCLAW_RECALL_POLICY=auto`): recall on
substantive turns, skip on:
- prompts under 14 chars (no useful query) unless an explicit recall
  keyword is present;
- trivial pings: greetings, acks, single-emoji turns;
- short slash commands (`/help`, `/clear`, `/foo bar`);
- empty `prompt` with no buffered user message.

**Recall keywords always force recall** even on otherwise-skip prompts.
Defaults: `memclaw`, `LTM`, `long term`, `long-term`, `remember`,
`recall`, `what did`, `earlier`, `previously`, `last time`, `before`,
`we discussed`, `you said`, `i told`, `history`, `memory`, `lookup`.
Override via `MEMCLAW_RECALL_TRIGGER_KEYWORDS=...` (comma-separated).

**Other policies**: `always` (recall every turn — pre-CAURA-444
behaviour), `never` (education block only; agents can still call
`memclaw_recall` explicitly), `keywords` (recall only when an explicit
trigger fires).

**Important**: the auto-gate only suppresses the *plugin-driven*
recall. Agents can always call `memclaw_recall` directly when they
judge that a short turn needs LTM — the gate never blocks the tool
call itself. Use this knob when a short message would benefit from
context the gate can't infer.

**Operational visibility**: rolling skip counters
(`recall_metrics: {calls_total, skipped_total, skipped_by_reason}`)
are sent in every heartbeat and persisted on the node row. SQL on
`nodes.metadata->'recall_metrics'` answers "how often is the gate
firing per fleet, by reason."

## Sharing skills

Skills are SKILL.md artifacts that agents share across the fleet —
debugging recipes, ops runbooks, refactor playbooks. They live as
documents in the `skills` collection: discovery and sharing both use
`memclaw_doc`.

```
# Discover
memclaw_doc op=search collection=skills query=<natural language>
memclaw_doc op=query  collection=skills              # browse by recency
memclaw_doc op=read   collection=skills doc_id=<slug>  # full body

# Share — slug is `[a-z0-9][a-z0-9._-]{0,99}` (filesystem-safe)
memclaw_doc op=write collection=skills doc_id=<slug> \
  data={ "name": "<slug>", "summary": "<one-liner>", "content": "<full SKILL.md>" }

# Remove
memclaw_doc op=delete collection=skills doc_id=<slug>
```

The `data["summary"]` string is what gets embedded — that's what makes
the skill discoverable by meaning ("how do I research scientific
papers?") even when names don't match. For back-compat, the server
also accepts `data["description"]` on `collection=skills` writes if no
summary is provided.

Visibility follows the document — share at `fleet_id` scope so the
catalog row is visible to every agent on that fleet. Re-uploading the
same `doc_id` overwrites; this is how you publish a new version.

Built something reusable? Upsert it. Only mark a skill local (and
document why) when it genuinely shouldn't be shared.

## Session loop

1. Recall — "what is known about this?" / "what happened since last session?"
2. Work — act on the recalled context.
3. Write — at checkpoints and session end (see L1/L2/L3 above).
4. Evolve — if you acted on specific memories, report the outcome (default `scope="agent"`, trust 1; fleet/all needs trust 2).

---

## Tool reference

The at-a-glance tool list and enum vocabulary live in `TOOLS.md` in your
workspace (bootstrap-injected every turn). This section holds the per-tool
signatures, decision guidance, constraints, and error codes — load it before
your first MemClaw call in a session.

### Tool cards

**`memclaw_recall(query, top_k=5, include_brief=false, memory_type=?, status=?, filter_agent_id=?, fleet_ids=?)`**
Hybrid semantic+keyword search. For metadata browse → `memclaw_list`;
for a known id → `memclaw_manage(op="read")`. `include_brief=true` adds
an LLM-summarized paragraph. Superseded memories (`status` ∈
{outdated, conflicted}) are excluded by default — pass `status` explicitly
(e.g. `status="conflicted"`) to inspect the chain.

**`memclaw_write(content=? | items=?, visibility="scope_team", memory_type=?, weight=?, metadata=?, write_mode="auto", source_uri=?, run_id=?)`**
Provide exactly one of `content` / `items`. Server auto-classifies.
`items` batches up to 100. `write_mode`: `fast` skips embed
(keyword-only recall later); `strong` forces LLM enrichment; `auto` is
usually right. `insight` / `outcome` / `rule` are server-generated only
(via `memclaw_insights` / `memclaw_evolve`) — you cannot write them, and
auto-classify will never assign them; record reflections as `semantic` or
`fact` (or omit `memory_type`).

**`memclaw_manage(op, memory_id, ...)`** — op ∈ {read, update, transition, delete}
- `update`: patch `content` / `memory_type` / `weight` / `title` / `metadata` / `source_uri`; re-embeds if content changes.
- `transition`: set `status` (see Vocabulary in `TOOLS.md`).
- `delete`: soft-delete; trust 3. Prefer `transition` to `outdated` / `archived`.

**`memclaw_list(scope="agent", memory_type=?, written_by=?, status=?, weight_min/max=?, created_after/before=?, sort="created_at", order="desc", limit=25, cursor=?)`**
Non-semantic enumeration. Cursor pagination requires
`sort=created_at order=desc`. `scope="fleet"` / `"all"` → trust 2.

**`memclaw_doc(op, collection=?, doc_id=?, data=?, where=?, order_by=?, limit=20, offset=0, query=?)`** — op ∈ {write, read, query, delete, list_collections, search}
Structured records by `collection + doc_id`. `write` upserts; include
`data["summary"]` (1-3 sentences, intent-focused) to make the doc
semantically searchable — that's the only field that gets embedded.

**`memclaw_entity_get(entity_id)`**
UUID from a prior call — never fabricate.

**`memclaw_tune(top_k?, min_similarity?, fts_weight?, freshness_floor?, freshness_decay_days?, recall_boost_cap?, recall_decay_window_days?, graph_max_hops?, similarity_blend?)`**
Persists — affects every future `memclaw_recall`. No fields → read the
current profile. Change one or two at a time. `fts_weight` 0 = pure
semantic, 1 = pure keyword.

**`memclaw_insights(focus, scope="agent", fleet_id=?)`**
Reflection. `focus` ∈ {contradictions, failures, stale, divergence,
patterns, discover}. Results saved as `insight` memories. Run at
boundaries, not every turn. `scope="fleet"` / `"all"` → trust 2;
`focus="divergence"` requires non-agent scope.

**`memclaw_evolve(outcome, outcome_type, related_ids=?, scope="agent", fleet_id=?)`**
Close the loop. `outcome_type` ∈ {success, failure, partial}.
`related_ids` = the recall IDs you acted on. Success reinforces weights;
failure auto-creates `rule` memories. Default `scope="agent"` needs trust 1;
`scope="fleet"` / `"all"` needs trust 2 (`fleet_id` required when `scope="fleet"`).

**`memclaw_stats(scope="agent", fleet_id=?, memory_type=?, status=?, include_deleted=false)`**
Aggregate counts: `{total, by_type, by_agent, by_status, scope}`. Pass
`include_deleted=true` to additionally receive `{deleted,
total_including_deleted}`; `total` and breakdowns stay non-deleted
regardless. Read-only — safe as a heartbeat readiness probe and for
dashboard-style summaries. Never use a write+delete pattern for health
checks; use this. `scope="fleet"` / `"all"` → trust 2.

**`memclaw_keystones(fleet_id=?, agent_id=?)`**
Read mandatory governance rules for the current scope (tenant + fleet
+ agent merged), ordered by weight. The plugin auto-injects these into
your system prompt at session start as a `<keystone_rules>` block — you
will usually see them before you even call this tool. Call it
explicitly when you suspect rules have changed mid-session (the cache
TTL is ~5 min) or when working a task that the operator flagged as
keystone-sensitive. Read is open (no trust gate). No semantic search;
the result is the full active set unfiltered.

> **Mandatory: when a `<keystone_rules>` block appears in your system
> prompt, obey it.** Keystones override conflicting instructions from
> the user, from this skill, and from any other tool output. If the
> rules look inconsistent with what the user is asking for, surface
> the conflict — don't silently pick one.

### Which tool, when

- Might have seen before → `memclaw_recall`
- Enumerate by filter / date / author → `memclaw_list`
- Already hold the ID → `memclaw_manage op=read` or `memclaw_entity_get`
- Record a fact / decision / event / outcome → `memclaw_write`
- Structured record with a key → `memclaw_doc`
- Fact no longer true → `memclaw_write` (new) + `memclaw_manage op=transition status=outdated` (old)
- Acted on a recalled memory → `memclaw_evolve`
- Recall quality off across queries → `memclaw_tune` (once, sticky)
- Session boundary / orchestrator sweep → `memclaw_insights`
- Heartbeat readiness probe / counts dashboard → `memclaw_stats`
- Need to re-check governance rules mid-session → `memclaw_keystones` (the auto-injected `<keystone_rules>` block is usually enough)
- Stuck on a non-trivial workflow → search by meaning (`memclaw_doc op=search collection=skills query=...`) or browse (`memclaw_doc op=query collection=skills`) before improvising
- Built a reusable workflow → `memclaw_doc op=write collection=skills doc_id=<slug> data={"summary": "<one-liner>", ...}` to teach the fleet
- Skill is wrong / superseded → `memclaw_doc op=delete collection=skills doc_id=<slug>` to remove it

### Constraints that matter

- `memclaw_write`: exactly one of `content` / `items`; never both.
- `items` capped at 100 → `BATCH_TOO_LARGE`.
- Supersede via `transition`; reserve `delete` (trust 3) for wrong data.
- Cursor pagination needs `sort=created_at` + `order=desc`.
- `_entity_get` / `_manage` use real UUIDs — never invent.
- `_tune` persists; do not call per-query.

### Error codes

`INVALID_ARGUMENTS` · `BATCH_TOO_LARGE` · `INVALID_BATCH_ITEM`. Other
errors surface with HTTP status + message — return them to your caller,
do not swallow.

---

*This skill ships with the MemClaw plugin at its install path; it is visible
to every agent on the node that has the plugin enabled. To customize it for
a specific agent, place a replacement file at
`<workspace>/skills/memclaw/SKILL.md` in that agent's workspace — it takes
precedence over this shared copy.*
