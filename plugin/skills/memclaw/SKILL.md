---
name: memclaw
description: The agent's persistent long-term memory — the only knowledge that survives across sessions, shared across the fleet under access control. Consult it at the start of a task to recall prior decisions, findings, and rules before acting, and write outcomes, decisions, and lessons as work completes. Use whenever a memclaw_* tool is present, whenever the user refers to past work ("what did we decide", "last time", "earlier"), or whenever any durable fact needs to be stored, recalled, superseded, or shared with the fleet. Do not use it for throwaway within-session scratch state.
user-invocable: false
metadata: {"openclaw": {"requires": {"config": ["plugins.entries.memclaw.enabled"]}}}
---

# MemClaw Skill

MemClaw is your long-term memory. Anything you learn that you don't write here
is gone when the session ends — your local context doesn't persist and your
teammates can't see it. So treat MemClaw as the default home for every
decision, finding, outcome, rule, and reusable workflow, and consult it before
you act. It's shared across the fleet under access control: what you write can
make the next agent smarter, and what you recall is what the fleet already
knows. Using it is the job, not an optional extra.

**The plugin runs a baseline loop for you — the tools are still yours.** On this
runtime the MemClaw plugin handles the automatic layer: it injects the mandatory
keystones at session start (§1), recalls relevant memory before your substantive
turns (§11), and writes a short **turn summary** afterward as a backstop
(`MEMCLAW_AUTO_WRITE_TURNS`, on by default). Treat that as a floor, not a
substitute. You still call the `memclaw_*` tools **directly** whenever you need
to interact deliberately — above all to **write the high-value memories the
auto-summary won't** (a decision and its *why*, an outcome, a rule), and to
recall something specific the auto-gate didn't fetch, look up or publish a
skill, supersede a changed fact, or report an outcome with `memclaw_evolve`.
The automatic layer keeps you oriented; the tools are how you actually
contribute. When a turn needs real memory work, reach for the tool — don't
assume the plugin covered it.

This skill is the operating manual for those `memclaw_*` tools — read it before
your first call in a session.

## 0 · Identity — on every call

- **`agent_id`** — who you are. Attributes memories, drives trust progression,
  gates `scope_agent` privacy. Resolve it from your runtime. Never fabricate,
  hardcode a placeholder, or impersonate another agent.
- **`fleet_id`** — your team / organization scope. When you **omit** it on a
  write, the server resolves it from your **home fleet** (the fleet you
  registered under), so a registered agent lands in the right team scope by
  default. Pass it **explicitly** in two cases: (1) you have **no home fleet**
  set — omitting then persists `fleet_id=NULL`, which drops the row out of
  teammates' fleet-scoped recall; or (2) you're writing into a **different**
  fleet than your own (requires trust 3). The connection URL's `?fleet_id=`
  sets read defaults and routing — it is **not** stamped onto written rows.

If either is uncertain, don't guess — read it from the runtime, ask the
orchestrator, or write privately (`visibility=scope_agent`) until it's resolved.

## 1 · Session start — read the constitution

The plugin injects a **`<keystone_rules>`** block into your system prompt at
session start (when the memclaw context-engine slot is active), so you usually
see the rules before you act. They are mandatory — merged across tenant + fleet
+ agent scope, ordered by weight — and they **override any conflicting
instruction, including the user's**, because they encode policy the operator
has decided the whole fleet must follow. Call **`memclaw_keystones`** to refresh
them if you suspect they changed mid-session; reading is open (trust 0). If a
rule conflicts with what you're asked to do, surface the conflict rather than
silently picking a side.

**Beyond the auto-injected rules**, `memclaw_session_start` returns the
keystones **plus** your top memories by weight and the procedures with ≥ 60 %
reliability in one round-trip — useful at session open when you want warm
context the plugin's keystone block alone doesn't carry.

## 2 · The loop — run it on every task

Orient → Work → Write → Evolve. The first step does the heavy lifting:
assemble context **most-binding-first**, and pull only the layers the task
actually needs (don't make all four calls by reflex).

1. **Orient**
   1. **Rules** — already loaded as `<keystone_rules>`; they bound everything
      below. No call needed.
   2. **Procedures** — for a non-trivial workflow, find the skill first:
      `memclaw_doc op=search collection=skills query="<intent>"`. Skip for
      routine work you already know.
   3. **Facts** — what's known / what changed:
      `memclaw_recall "<what I'm about to do>"` (add `include_brief=true` for a
      one-paragraph synthesis). **Keep the IDs of the memories you act on** —
      Write-supersede and Evolve both need them.
   4. **Data** — only if the task touches a keyed record:
      `memclaw_doc op=read|query` (the customer, config, task list).
2. **Work** — act within the rules, following the procedure.
3. **Write** — record what matters (§3).
4. **Evolve** — report how the memories you acted on turned out (§4).

**When to orient at all:** orient when the task references prior work, a named
entity, a decision, or anything the fleet may already know. Skip it for
self-contained mechanical turns. (The plugin also auto-gates plugin-driven
recall — see §11 — but you can always call `memclaw_recall` directly when a
short turn needs context the gate can't infer.)

## 3 · How and when to write a memory

**Write when something durable happened:**
- a decision, and *why* you made it;
- a finding, result, or outcome;
- a rule or constraint you learned;
- the end of a meaningful task.

**Don't write the noise.** Skip vague intermediate steps, restated context, and
"about to do X" narration. Ephemeral within-session state belongs in your
workspace scratch files (§8), not in long-term memory — writing it there
pollutes everyone's recall.

**How to write:** supply raw prose — you don't classify or tag anything. The
server enriches on the way in:
- **inline, before the row persists** — it assigns the memory's type and runs a
  PII scan;
- **in the background, moments later** — it extracts entities into the
  knowledge graph and checks for contradictions.

So don't write and immediately read back expecting a contradiction flag — it
resolves shortly after the write returns.

Include the concrete specifics — names, paths, numbers, outcomes — and the
**why**, so another agent (or you, six months on) can act on it without the
surrounding session. Default `visibility=scope_team` so your fleet benefits.
Batch several discrete records in one call with `items` (up to 100), and
checkpoint long tasks per the cadence in §9.

**Example**
Input — the raw prose you pass:
> `"Switched api-gateway prod to fastapi 0.136.3 — 0.137 broke include_router via a starlette upper-bound. Pin held; smoke tests green."`

Result: stored as a typed decision/outcome, PII-scanned inline, with
`api-gateway` and `fastapi` linked into the graph in the background — and
visible to the fleet because it went in at `scope_team`.

Never paste secrets — API keys, tokens, credentials — into memory `content`.
The PII scan is a safety net, not permission; keep them out entirely.

## 4 · Report outcomes so the memory compounds

When you act on memories you recalled, tell the memory how it went:
`memclaw_evolve(outcome, outcome_type, related_ids)`, where `related_ids` are
the IDs you kept during Orient. Success reinforces those memories' weight. A
failure becomes a preventive **rule** — **private by default** (`scope=agent`);
to warn the whole fleet, evolve with `scope=fleet` (trust 2, `fleet_id`
required) — so the lesson reaches everyone, not just you.

## 5 · Two stores, one rule

- **Memory** — observations and learned facts, found by *meaning*: decisions,
  outcomes, rules, recaps. Read with `memclaw_recall`, write with
  `memclaw_write`.
- **Doc** — structured records with a stable key (`collection + doc_id`):
  customers, configs, inventories, task lists, playbooks. All through
  `memclaw_doc`.
- **Entity** — a named graph object (person, project, service). Fetch by a UUID
  surfaced in a prior recall (`memclaw_entity_get`).

**Rule of thumb:** need semantic search → it's a memory. Need keyed lookup →
it's a doc. Already hold an ID → it's an entity.

**Cross-store discovery.** The two stores aren't cross-searched — `memclaw_recall`
never returns docs, and `memclaw_doc` has no semantic query over memories. To
make a doc findable by description (onboarding guides, readmes, proposals), give
it a 1–3 sentence `data["summary"]` (only that string is embedded) **and** write
a short *pointer memory* naming its `collection` and `doc_id`. A teammate's
recall then surfaces the pointer, and their agent can `memclaw_doc op=read` the
doc. When you don't know what exists, call `memclaw_doc op=list_collections`
first.

## 6 · Trust and sharing

You auto-register at **trust 1** on your first write.

| Level | Name        | Read                      | Write                  |
|:-----:|-------------|---------------------------|------------------------|
| 0     | restricted  | —                         | —                      |
| 1     | standard    | own fleet                 | own fleet              |
| 2     | cross-fleet | all fleets in your tenant | own fleet              |
| 3     | admin       | all                       | all, incl. deletes     |

Operations that escalate the required level:
- browsing / reflecting with `scope="fleet"` or `"all"` → trust 2
- reporting outcomes (`memclaw_evolve`) at `scope="fleet"` / `"all"` → trust 2 (default `scope="agent"` needs only trust 1)
- `memclaw_manage op=delete` → trust 3

**Knowing your own level.** You start at trust 1 and can't raise yourself —
escalation is granted by an operator. There's no self-query, so don't
pre-emptively avoid an operation you're unsure about: attempt it. A permission
error names both your current level and the one required (e.g. *"Agent X
(trust_level=1) … Requires trust_level >= 2"*) — surface that error rather than
silently retrying at a narrower scope.

**Visibility (on write)** decides who can see a memory: `scope_agent` (private)
· `scope_team` *(default — your fleet)* · `scope_org` (all fleets in tenant).
**Scope (on read / `_list` / `_insights`):** `agent` *(default)* · `fleet`
(trust 2) · `all` (trust 2). Prefer `scope_team` on write and `scope=agent` on
read unless you need cross-agent context. *Naming caveat:* writes take
`visibility=scope_*`; reads/list/keystone filters take `scope=*` — two axes,
similar spelling.

## 7 · Keeping knowledge clean

A few habits keep recall trustworthy and sharp:

- **Make memories good** — dated, concrete, standalone, atomic, and updated (not
  duplicated). Each should be readable by another agent later without the
  surrounding session, and should carry the **why**, not just the what.
- **Supersede, don't delete.** When a fact changes: (1) write the new one, (2)
  recall the old one, (3) `memclaw_manage op=transition status=outdated`. This
  keeps the lineage. Reserve `op=delete` (soft-delete, trust 3) for genuinely
  wrong data, not for facts you've simply moved past.
- **Resolve conflicts; don't pick one silently.** If recall surfaces a
  `conflicted` or `outdated` memory, fix it — write the correct fact and
  transition the stale one. Two live opposing beliefs degrade every future
  recall for everyone.

## 8 · MemClaw vs your workspace files

MemClaw is the only place for cross-session, cross-agent knowledge. A
file-based scratchpad in your workspace (e.g. `MEMORY.md`) is **session-local**
— it lives in your bootstrap context every turn and pays input tokens for every
byte, and your teammates never see it.

- Keep `MEMORY.md` lean: only **active projects, current routing decisions,
  recent decisions (≤ 7 days), open threads.** Target a few KB; prune anything
  older or larger on session start.
- Everything else goes to MemClaw via `memclaw_write` (history, finished work,
  lessons), `memclaw_doc` collections (reference data with a natural key), or
  entities (people / projects / services).
- Never copy MemClaw recall results into `MEMORY.md` — they're already
  retrievable. Never substitute a local file for a MemClaw write.

## 9 · Capture cadence (L1 / L2 / L3)

- **L1 — per task.** At task completion or a real decision point (not every
  turn), write with date, what, who, outcome, next. Tool-by-tool progress is
  not an L1 write — that's scratchpad (§3).
- **L2 — session boundary.** At > 60 % context or session end, write a full
  summary.
- **L3 — consolidation.** On periodic runtime sweeps, find gaps, merge
  duplicates, transition contradicted facts to `outdated`.

## 10 · Orchestrator + subagent protocol

If your runtime dispatches subagents:
- The spawning agent writes findings after each subagent completes.
- The subagent writes its own findings before handing back.
- Both writes carry their **own** `agent_id`.

Single-agent runtimes ignore this section.

## 11 · Recall policy (auto-gating)

Before each model call the plugin's context engine decides whether to issue a
plugin-driven recall, so trivial turns ("hi", "ok", `/help`, single-emoji acks)
don't hit the backend and pay tokens for an unhelpful recall block.

- **Default** (`MEMCLAW_RECALL_POLICY=auto`): recall on substantive turns; skip
  very short prompts, trivial pings, and short slash-commands.
- **Recall keywords always force recall** (e.g. `remember`, `recall`, `last
  time`, `we discussed`, `previously`, `history`); override the set with
  `MEMCLAW_RECALL_TRIGGER_KEYWORDS`.
- Other policies: `always`, `never` (education block only), `keywords`.
- The gate only suppresses *plugin-driven* recall — **you can always call
  `memclaw_recall` directly** when a short turn needs context the gate can't
  infer.

Rolling skip counters (`recall_metrics`) ride the heartbeat for per-fleet
visibility.

The plugin also auto-writes a short **turn summary** after substantive turns
(`MEMCLAW_AUTO_WRITE_TURNS`, on by default). That's a backstop, not a
replacement for the deliberate, high-value writes in §3 — and it never evolves,
supersedes, or files docs for you. Do that work yourself.

## 12 · Reuse and publish workflows — the `skills` collection

Proven workflows live as `SKILL.md` documents in the **`skills`** collection.
You don't learn a new tool per playbook — it's the same `memclaw_doc`, so your
vocabulary never grows with the library.

```text
# Discover before improvising on a non-trivial workflow:
memclaw_doc op=search collection=skills query="<intent>"
memclaw_doc op=read   collection=skills doc_id=<slug>   # full body

# Publish something reusable so the fleet inherits it:
memclaw_doc op=write collection=skills doc_id=<slug> \
  data={ "name": "<slug>",
         "summary": "<1-line, intent-focused — this is what gets embedded>",
         "content": "<full SKILL.md>" }
# Re-uploading the same doc_id overwrites it (upsert; no version history).

# Remove a wrong/superseded one:
memclaw_doc op=delete collection=skills doc_id=<slug>
```

Slugs are filesystem-safe: `[a-z0-9][a-z0-9._-]{0,99}`. The `summary` is the
only embedded field — write a sharp, intent-focused one ("Use when migrating
SQLite→Postgres…") so the skill is found by meaning even when names don't match.

## 13 · Procedures — reliability-ranked workflows

A **procedure** is an executable tool-call sequence the fleet has tried before,
ranked by how reliably it has *worked*. Where a `skills`-collection doc is prose
you read, a procedure is a ranked recipe you follow and then grade — so the next
agent's ranking reflects real outcomes, not guesses.

The loop is suggest → follow → record:

```text
# 1. Before a non-trivial, repeatable workflow, ask what has worked:
memclaw_procedure_suggest context_features={framework, region, library, …} \
  task="<goal>"
#   → request_id + ranked procedures (id, name, tools_sequence, score).
#   Quarantined (proven-unreliable) procedures are already excluded.

# 2. Follow the top procedure's tools_sequence. Keep its id.

# 3. Report how it went — THIS is what makes ranking improve:
memclaw_procedure_record procedure_id=<id> outcome_type=success|failure

# Capture a new reusable procedure (starts at reliability 0.5):
memclaw_procedure_write name="<slug>" tools_sequence=[…] \
  context_features={…} [reasoning_guide=… risk_level=low|medium|high]
```

**`validation_passed` — verified vs. self-reported (do not skip).** An agent
grading its own work tends to call it a success. If — and only if — an
**independent** check confirmed the outcome (a separate evaluator agent, a test
run, a CI gate), pass `validation_passed=true` on `memclaw_procedure_record`.
Verified outcomes move a separate counter and surface `verified_reliability` in
the response, so a procedure proven by real tests is distinguishable from one
with only self-reported wins. Leave it unset for a self-graded outcome — don't
mark your own homework as verified.

A procedure auto-quarantines after ≥ 3 attempts with reliability below 0.3 and
drops out of `suggest`. Manual lifecycle (read stats, quarantine/unquarantine,
invalidate, delete) is `memclaw_procedure_manage`.

## 14 · Stable-infra facts — `memclaw_env`

Infrastructure constants — service URLs, ports, hostnames — are not memories and
not docs; they're **env truths** with a confidence that rises as agents confirm
them. Use `memclaw_env` (`op=upsert|get|list|verify`) for "the staging API is at
`https://…:8443`" rather than burying it in prose. `verify(name)` bumps the
confidence without changing the value; re-`upsert` with a new value resets it
when the fact changes. Prefer this over a plain memory whenever a teammate would
ask "is this still the right URL/port/host?".

## A full loop, end to end

One task — orient, work, write, evolve — with the IDs threaded through:

```text
# 1. Orient — recall, and keep the IDs that come back
memclaw_recall "deploy api-gateway to staging" include_brief=true
#   → mem_8f2a (rule: "staging deploys need a smoke test"), mem_4d1c (last deploy)

# 2. Work — run the deploy, following the rule in mem_8f2a

# 3. Write — record the outcome (team-visible; home fleet resolved on omit)
memclaw_write content="api-gateway v2.3 deployed to staging; smoke test green" \
  visibility=scope_team

# 4. Evolve — report against the memories you acted on
memclaw_evolve outcome="deploy succeeded, smoke test passed" \
  outcome_type=success related_ids=[mem_8f2a, mem_4d1c]
#   if it had failed in a way the whole fleet should avoid:
#   add scope=fleet (trust 2) so the preventive rule reaches teammates
```

---

## Tool reference

Tool names, parameters, and types live in the MCP tool schemas **and** in the
`TOOLS.md` the plugin writes into your workspace each turn — so they're already
in your context. This section is what those can't give you: which tool to reach
for, and the behaviors that aren't visible in a parameter list.

### Which tool, when

- Might have seen it before → `memclaw_recall`
- Enumerate by filter / date / author → `memclaw_list`
- Already hold the ID → `memclaw_manage op=read` / `memclaw_entity_get`
- Record a fact / decision / event / outcome → `memclaw_write`
- Structured record with a key → `memclaw_doc`
- Find or publish a workflow → `memclaw_doc … collection=skills`
- Fact no longer true → `memclaw_write` (new) + `memclaw_manage op=transition status=outdated` (old)
- Acted on a recalled memory → `memclaw_evolve`
- One-call session warm start (keystones + top memories + reliable procedures) → `memclaw_session_start`
- Re-check governance rules mid-session → `memclaw_keystones` (the auto-injected `<keystone_rules>` block is usually enough)
- Repeatable workflow — what has worked before → `memclaw_procedure_suggest`, then `memclaw_procedure_record` after
- Capture a reusable tool-call recipe → `memclaw_procedure_write`
- Manage a procedure (stats / quarantine / invalidate) → `memclaw_procedure_manage`
- Infra constant (URL / port / host) → `memclaw_env`
- Recall quality off across queries → `memclaw_tune` (once; sticky)
- Session boundary / sweep → `memclaw_insights`
- Bulk-export your memories → `memclaw_export`
- Triage the worst-rated memories for cleanup → `memclaw_review`
- Readiness probe / counts → `memclaw_stats`

> Authoring keystones (`memclaw_keystones_set`) is **not available to plugin
> agents** — you can *read* governance rules (`memclaw_keystones`) but not write
> them. Keystones are authored over MCP/REST by a trusted operator.

### Behaviors the schema won't tell you

- **`memclaw_recall`** excludes superseded memories (`status` ∈ {outdated, conflicted}) by default — pass `status` explicitly to walk the chain.
- **`memclaw_write`** can't write `insight` / `outcome` / `rule` types — those are server-generated (via `memclaw_insights` / `memclaw_evolve`). `write_mode`: `fast` skips embedding → keyword-only recall afterwards; `strong` forces full LLM enrichment; `auto` is usually right.
- **`memclaw_manage op=transition`** targets: `active · pending · confirmed · cancelled · outdated · conflicted · archived · deleted` (also in `TOOLS.md`).
- **`memclaw_doc`** — `where` is scalar exact-match only (no array descent). A doc is invisible to `op=search` unless it has a `data["summary"]` (the only embedded field). Scope the search to a collection when you know it; omit `collection` for the single best match across the tenant.
- **`memclaw_tune`** persists and reshapes every later recall — change one or two knobs at a time; call with no arguments to read your current profile (`fts_weight` 0 = pure semantic, 1 = pure keyword).
- **`memclaw_insights`** saves findings as `insight` memories; run it at boundaries, not every turn. `focus="divergence"` needs a non-agent scope.
- **`memclaw_procedure_record`** drives ranking off the outcome you report, so honest grading matters. `validation_passed=true` is reserved for *independently* verified outcomes (separate evaluator / test / CI) — it moves the verified counters and `verified_reliability`; a self-graded success leaves it unset. Auto-quarantine: ≥ 3 attempts and reliability < 0.3.
- **`memclaw_review`** is read-only — act on its low-weight results via `memclaw_manage op=transition` (outdated/archived); it doesn't mutate anything itself.
- **`memclaw_stats`** is read-only — use it as a readiness/health probe, never a write-then-delete check.

### Anti-patterns

- Saving every intermediate step as a memory — pollutes recall.
- Storing narrative as a doc, or structured keyed records as memories.
- Saving a discoverable doc with no pointer memory — teammates won't find it.
- Guessing `agent_id` / `fleet_id`, or inventing UUIDs.
- Deleting when you should supersede.
- Writing org-wide (`scope_org`) anything that isn't genuinely org-relevant.
- Substituting `MEMORY.md` / local files for a MemClaw write.
- Silently dropping a denied call — surface the error so the orchestrator can decide.

### Constraints & errors

- `memclaw_write`: exactly one of `content` / `items`; `items` ≤ 100 → `BATCH_TOO_LARGE`.
- Cursor pagination needs `sort=created_at` + `order=desc`.
- `_entity_get` / `_manage` use real UUIDs — never invent.
- Error codes: `INVALID_ARGUMENTS` · `BATCH_TOO_LARGE` · `INVALID_BATCH_ITEM`. Other errors surface with HTTP status + message — return them to your caller, don't swallow.

---

*This skill ships with the MemClaw plugin at its install path; it is visible to
every agent on a node that has the plugin enabled
(`plugins.entries.memclaw.enabled`). To customize it for a specific agent, place
a replacement file at `<workspace>/skills/memclaw/SKILL.md` — it takes
precedence over this shared copy.*

---

## Appendix A · Hermes Agent Configuration

MemClaw is configured as an MCP server in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  memclaw:
    url: http://192.168.1.53:8000/mcp
    headers:
      X-Tenant-ID: default
      X-Agent-ID: hermes-orchestrator
    connect_timeout: 60
    timeout: 120
```

### Identity header priority

The server resolves identity in this order (first match wins):
1. `X-Agent-ID` header (set in config — guarantees identity, model never forgets)
2. Body `agent_id` arg on each tool call
3. Deployment default (`memclaw-caura-memclaw-c8624d-406` — shared bucket, avoid)

### REST API (supplement to MCP)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /api/v1/health` | GET | Health check |
| `GET /api/v1/status` | GET | Full status with dependencies |
| `GET /api/v1/stats` | GET | Aggregate counts |
| `GET /api/v1/memories?agent_id=...` | GET | List memories |
| `POST /api/v1/memories` | POST | Create memory |
| `POST /api/v1/recall` | POST | Semantic search |

REST does **not** include procedures, keystones, insights, evolve, tune, manage, or doc. Those are MCP-only.

### Monitoring

```bash
hermes mcp test memclaw                         # "✓ Connected" + "✓ Tools discovered: 20"
curl -s http://192.168.1.53:8000/api/v1/health  # Direct health check
grep -i memclaw ~/.hermes/logs/mcp-stderr.log    # MCP error log
```

---

## Appendix B · E2E Test Results (2026-06-30, all 20 tools)

30 operations tested. **28/30 PASS (93.3%)**. All failures by-design.

### Core Memory

| Tool | Op | Status | Latency | Notes |
|------|----|--------|---------|-------|
| `memclaw_write` | content | PASS | 590ms | Returns UUID; fast mode; auto-classifies |
| `memclaw_recall` | query | PASS | 297ms | Hybrid semantic+keyword; similarity scores |
| `memclaw_list` | filter/sort | PASS | 19ms | Cursor pagination |
| `memclaw_manage` | read | PASS | 14ms | Full details with metadata |
| `memclaw_manage` | update | PASS | 714ms | Patches content+weight, re-embeds |
| `memclaw_manage` | transition | PASS | 25ms | Status changes |
| `memclaw_stats` | aggregate | PASS | 26ms | Breakdowns by type/agent/status |

### Procedures

| Tool | Op | Status | Latency | Notes |
|------|----|--------|---------|-------|
| `memclaw_procedure_suggest` | ranked list | PASS | 847ms | Scored with breakdown |
| `memclaw_procedure_write` | create | PASS | 505ms | Stores with embedding |
| `memclaw_procedure_record` | outcome | PASS | 31ms | Updates reliability (0.5→0.667) |
| `memclaw_procedure_manage` | stats | PASS | 12ms | Reliability + quarantine status |

### Governance

| Tool | Op | Status | Latency | Notes |
|------|----|--------|---------|-------|
| `memclaw_keystones` | read | PASS | 51ms | 0 rules is normal |
| `memclaw_keystones_set` | set | FAIL | 15ms | Trust 1 < 2 (by design) |

### Documents

| Tool | Op | Status | Latency | Notes |
|------|----|--------|---------|-------|
| `memclaw_doc` | write | PASS | 359ms | Upserts, auto-indexes |
| `memclaw_doc` | read | PASS | 9ms | By collection+doc_id |
| `memclaw_doc` | query | PASS | 16ms | where dict filter |
| `memclaw_doc` | search | PASS | 466ms | Cross-collection semantic |
| `memclaw_doc` | list_collections | PASS | 23ms | Names + counts |
| `memclaw_doc` | delete | FAIL | 8ms | Fleet policy blocks (by design) |

### Analytics

| Tool | Op | Status | Latency | Notes |
|------|----|--------|---------|-------|
| `memclaw_evolve` | outcome | PASS | 427ms | Adjusts weights, generates rules |
| `memclaw_insights` | patterns | PASS | 467ms | 6 focus modes |
| `memclaw_tune` | get | PASS | 8ms | Returns current profile |

### Entity, Env, Session, Misc

| Tool | Op | Status | Latency | Notes |
|------|----|--------|---------|-------|
| `memclaw_entity_get` | lookup | PASS | 134ms | Entity + linked memories |
| `memclaw_env` | upsert | PASS | 21ms | Infra facts tenant-wide |
| `memclaw_env` | list | PASS | 19ms | All env truths |
| `memclaw_env` | verify | PASS | 27ms | Bumps verification_count |
| `memclaw_session_start` | (default) | FAIL | 18ms | body agent_id needs fleet_id; use headers |
| `memclaw_review` | low-weight | PASS | 13ms | Flags below threshold |
| `memclaw_export` | bulk | PASS | 21ms | Paginated with cursor |

**Failures (all by-design):**
- `memclaw_keystones_set` — trust gating (register first)
- `memclaw_doc delete` — fleet policy blocks destructive ops
- `memclaw_session_start` — body arg requires fleet_id; use headers instead

---

## Appendix C · Troubleshooting

### 1. UPSTREAM_TIMEOUT on recall
**Symptom**: `Search embedding timed out` after ~10s
**Cause**: Embedding service (OpenAI) slow or unreachable
**Fix**: Retry with shorter query, or use `memclaw_list` for non-semantic browsing

### 2. context_features validation error
**Symptom**: `Input should be a valid dictionary`
**Cause**: Passing list instead of dict
**Fix**: `context_features = {'docker': True}` not `['docker']`

### 3. keystones_set FORBIDDEN
**Symptom**: `Agent 'X' (trust_level=1) < required 2`
**Cause**: Unregistered agent at trust 1
**Fix**: Write one memory first (`memclaw_write`), then retry

### 4. session_start INVALID_ARGUMENTS
**Symptom**: `agent_id requires fleet_id`
**Cause**: Body `agent_id` arg requires `fleet_id` too
**Fix**: Omit body `agent_id` (rely on header) or pass both

### 5. Fleet-scope FORBIDDEN
**Symptom**: `fleet 'X' is not writable by principals of fleet 'Y'`
**Cause**: Fleet membership policy
**Fix**: Write to your home fleet or omit `fleet_id` (null fleet)

### 6. doc delete FORBIDDEN
**Symptom**: `principals of fleet are not permitted to delete memories`
**Cause**: Fleet policy blocks destructive doc ops
**Fix**: Update to empty/stale state instead, or transition to archived

### 7. Identity headers not taking effect
**Symptom**: agent_id still shows default after config change
**Cause**: MCP connections cached at session start
**Fix**: `/reload-mcp` or start a new session

### 8. Slow writes (10-12s)
**Symptom**: `memclaw_write` takes 10+ seconds
**Cause**: Embedding generation latency
**Expected**: Normal; `write_mode: fast` helps but embedding still takes time

### 9. Duplicate memory on write
**Symptom**: `status: duplicate` with `existing_id`
**Cause**: Near-duplicate already exists
**Fix**: Use `existing_id` field instead of `id`

### 10. evolve CONFLICT
**Symptom**: `Duplicate memory exists`
**Cause**: Memory already evolved
**Fix**: Check metadata with `memclaw_manage op=read` before evolving

### 11. bulk_delete FORBIDDEN
**Symptom**: `FORBIDDEN` on bulk_delete
**Cause**: Requires trust >= 3
**Fix**: Use individual `memclaw_manage op=transition status=archived` instead

### 12. Insights invalid focus
**Symptom**: `Invalid focus 'gaps'`
**Cause**: Wrong focus value
**Fix**: Use one of: `contradictions`, `failures`, `stale`, `divergence`, `patterns`, `discover`
