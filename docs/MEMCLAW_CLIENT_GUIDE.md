# MemClaw — Agent Client Guide

A persistent, semantically-searchable memory + procedural-memory platform for AI
agents. This guide onboards three clients — **Claude Code**, **Hermes**, and
**OpenCode** — onto the running instance on `.53`.

> Audience: the agents themselves (and whoever wires them up). Everything here is
> verified against the live server, not aspirational.

---

## 0. TL;DR connection card

| Fact | Value |
|------|-------|
| Endpoint | `http://192.168.1.53:8000/mcp` |
| Transport | MCP **Streamable HTTP** |
| Server | `MemClaw v2.17.0` (MCP protocol `2025-06-18`) |
| Auth | **None** — standalone mode, single tenant `default` |
| Session | **Stateless** — no `Mcp-Session-Id` needed; POST `tools/call` directly |
| Identity | carried by the `agent_id` **argument on every tool call** |
| Health | `GET http://192.168.1.53:8000/api/v1/health` |
| Tools | 20 (16 memory + 4 procedural-memory) |

**The one rule that matters for multi-agent use:** each client must carry its
own stable identity, or every agent's memories pile into one namespace.

> **Identity alone is not isolation.** `agent_id` sets ownership, but the default
> write visibility `scope_team` is tenant-wide in standalone and is readable by
> *every* agent — so distinct `agent_id`s still see each other's `scope_team`
> memories. To actually partition namespaces, either write `visibility:
> "scope_agent"` (private to one `agent_id`) or group by `fleet_id`. See
> **§2b** for the two concrete patterns. This section is just about *who you are*.

Two ways to set identity, strongest first:

1. **Identity headers (recommended — set once per client, guaranteed).** In
   standalone mode (no gateway secret) the server honours
   `X-Tenant-ID: default` + `X-Agent-ID: <your-id>` request headers, and the
   header **overrides** any body arg. Put these in each client's MCP config once
   and you never think about it again — it doesn't depend on the model
   remembering to pass anything. (Verified live.)
2. **Body `agent_id` arg (fallback).** If headers aren't set, identity comes from
   the `agent_id` argument on each tool call. If that's *also* omitted it lands
   in the deployment-wide seeded default
   (`memclaw-caura-memclaw-c8624d-406`, from `MEMCLAW_DEFAULT_AGENT_ID`) — i.e.
   the shared bucket where namespaces mix. Don't rely on this for separation.

| Client | Use `agent_id` |
|--------|----------------|
| Claude Code | `claude` |
| Hermes | `hermes-orchestrator` |
| OpenCode | `opencode` |

---

## 1. Connecting each client

### 1a. Claude Code

Register the server once (user scope = all projects) **with identity headers**
so every call is attributed to `claude` without the model having to pass it:

```bash
claude mcp add --transport http memclaw http://192.168.1.53:8000/mcp --scope user \
  --header "X-Tenant-ID: default" \
  --header "X-Agent-ID: claude"
claude mcp list          # memclaw → ✓ Connected
```

(Use a more unique id — e.g. `claude-<host>` — if multiple machines run Claude
against the same MemClaw and you want them separated.)

Optional but recommended — install the bundled usage skill so the model knows
when to reach for each tool:

```bash
curl -X POST http://192.168.1.53:8000/api/v1/install-skill
```

Because identity rides on the `agent_id` argument (there are no per-connection
auth headers in standalone mode), tell the model to use its own id. Drop this in
`CLAUDE.md` or a project rule:

> When using any `memclaw_*` tool, always pass `agent_id: "claude"`.

### 1b. OpenCode

OpenCode speaks MCP natively. Add a **remote** server to `opencode.json`
(project root or `~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memclaw": {
      "type": "remote",
      "url": "http://192.168.1.53:8000/mcp",
      "enabled": true,
      "headers": {
        "X-Tenant-ID": "default",
        "X-Agent-ID": "opencode"
      }
    }
  }
}
```

The `X-Agent-ID` header pins every call to `opencode` — no need to instruct the
model to pass `agent_id` in the body.

### 1c. Hermes (and any custom / programmatic client)

The server is **stateless** — no `initialize` handshake or session id is
required. Any MCP client library pointed at the URL works; or call the JSON-RPC
endpoint directly over HTTP.

Minimal raw call (works as-is — copy/paste):

```bash
curl -s -X POST http://192.168.1.53:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-Tenant-ID: default' \
  -H 'X-Agent-ID: hermes-orchestrator' \
  -d '{
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
          "name": "memclaw_recall",
          "arguments": { "query": "deployment runbook", "top_k": 5 }
        }
      }'
```

Responses may be returned as a single JSON object **or** as an SSE frame
(`data: {...}`). Strip a leading `data: ` before parsing. Tool results arrive as
`result.content[0].text` containing a JSON **string** — parse it a second time.

Python sketch for Hermes:

```python
import httpx, json

MEMCLAW = "http://192.168.1.53:8000/mcp"
# Identity headers pin every call to this agent's namespace — set once here
# instead of passing agent_id in every body.
HEADERS = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream",
           "X-Tenant-ID": "default",
           "X-Agent-ID": "hermes-orchestrator"}

def call(tool: str, args: dict) -> dict:
    r = httpx.post(MEMCLAW, headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }, timeout=30)
    body = r.text
    if body.startswith("data:"):
        body = body.split("data:", 1)[1].strip()
    payload = json.loads(body)["result"]["content"][0]["text"]
    return json.loads(payload)

call("memclaw_write", {"content": "Hermes orchestrates ticket DAGs across the fleet."})
print(call("memclaw_recall", {"query": "what does hermes do", "top_k": 3}))
```

---

## 2. Identity, trust & scopes (read this before writing tools)

MemClaw is multi-tenant-aware but here runs **single-tenant** (`default`). Within
the tenant, every agent has an **identity** (`agent_id`) and an **effective
trust level** that gates which scopes it may touch.

**How identity is resolved** (first match wins): `X-Agent-ID` header (standalone
honours it when `X-Tenant-ID` is also sent) → body `agent_id` arg → the
deployment's seeded default. Setting the header in client config is the only
option that doesn't depend on the model remembering the body arg — use it.

```
scope = agent   → only memories/procedures the agent owns      (trust ≥ 1)
scope = fleet   → everything in a named fleet (fleet_id req.)   (trust ≥ 2)
scope = all     → cross-agent, whole tenant                     (trust ≥ 2)
include_deleted, bulk destructive ops                           (trust 3)
```

**Registration bumps trust.** A brand-new `agent_id` is *unregistered* and sits
at **trust 1** — it can read/write its own memories and use procedures, but
`scope=fleet`/`all` return `FORBIDDEN`. Writing **one memory** registers the
agent (creates its row) and unlocks the higher scopes. Verified server message:

```
"Agent 'X' is not registered (no row; effective trust 1 < required 2).
 Register the agent by writing one memory first."
```

So each new client's **first action** should be a `memclaw_write` (e.g. a
one-line "who am I" memory). After that, fleet/all introspection works.

**Visibility** (set at write time, independent of scope gating):
`scope_agent` (private) · `scope_team` (default) · `scope_org` (widest). Prefer
team/org so other agents benefit.

---

## 2b. Multi-VM / multi-repo topology

The common deployment: several VMs each running an agent, where multiple VMs may
work on the **same repo**, and one VM works on **many repos** over time. You want
memories about a repo **shared** across whoever touches it, while different repos
stay **separate**. Two independent knobs do this:

| Knob | Meaning | Scope | Set where |
|------|---------|-------|-----------|
| `agent_id` | **who/where** wrote it (`claude-vm1`, `hermes-vmC`) | one VM | **static** — `X-Agent-ID` header, once per VM |
| `fleet_id` | **which repo** the memory belongs to | one repo | **dynamic** — body arg per call, = repo slug |

`fleet_id` can't be a header because a VM hops between repos. In standalone you're
single-tenant (`default`), so the repo boundary is `fleet_id`, **not** a tenant.

**Verified behaviour:** an agent on `fleet_id=repo-foo` (`scope_team`) is read by
*any other* agent doing `recall scope=fleet fleet_ids=["repo-foo"]`, with the
original `agent_id` preserved as provenance; an agent on `repo-bar` sees none of
it. A plain `recall` (default `scope=agent`) sees only the caller's own memories.

### Setup

**1. Identity header per VM — derive from hostname so it's unique + stable:**

```bash
# each Claude VM (×3)
claude mcp add --transport http memclaw http://192.168.1.53:8000/mcp --scope user \
  --header "X-Tenant-ID: default" \
  --header "X-Agent-ID: claude-$(hostname -s)"
# each Hermes VM (×4): same two headers, X-Agent-ID: hermes-<hostname>
```

**2. Repo = fleet, shared via `scope_team`.** On every memclaw call the agent sets
`fleet_id` to a stable repo slug (git remote basename or repo folder name):

```
write   { fleet_id: "<repo>", visibility: "scope_team", content: "..." }
recall  { fleet_id-via: scope: "fleet", fleet_ids: ["<repo>"], query: "..." }
```

**3. First write registers the agent** (trust 1 → 2) and unlocks `scope=fleet`
reads. One throwaway "who am I" memory does it.

### The one discipline

To *read* the shared repo pool you must pass `scope="fleet"` + `fleet_ids=["<repo>"]`.
A default `recall` only returns the calling agent's own memories. Make the agent
do this automatically:

- **Claude** — drop this in the repo's `CLAUDE.md` (or a user rule):

  > **MemClaw fleet convention.** Treat the current git repo as the memory
  > workspace. Compute `REPO` = basename of `git remote get-url origin` (fall back
  > to the repo folder name). On every `memclaw_*` call: pass `fleet_id: REPO`;
  > on `memclaw_write` also pass `visibility: "scope_team"`; on `memclaw_recall` /
  > `memclaw_list` / `memclaw_stats` pass `scope: "fleet"` and
  > `fleet_ids: ["<REPO>"]`. Do **not** set `agent_id` in the body — identity
  > comes from the connection header.

- **Hermes** — set it in code from the repo it's driving:

  ```python
  import subprocess, os, json, httpx

  def repo_slug(path: str = ".") -> str:
      try:
          url = subprocess.check_output(
              ["git", "-C", path, "remote", "get-url", "origin"], text=True).strip()
          return os.path.splitext(os.path.basename(url))[0]   # ".../acme.git" -> "acme"
      except Exception:
          return os.path.basename(os.path.abspath(path))

  FLEET = repo_slug()   # compute once per repo Hermes operates on

  def call(tool, args):
      if tool == "memclaw_recall":                 # recall: list arg, no fleet_id
          args.setdefault("scope", "fleet")
          args.setdefault("fleet_ids", [FLEET])
      elif tool in ("memclaw_list", "memclaw_stats"):
          args.setdefault("scope", "fleet")
          args.setdefault("fleet_id", FLEET)
      elif tool == "memclaw_write":
          args.setdefault("fleet_id", FLEET)
          args.setdefault("visibility", "scope_team")
      # ... POST with X-Agent-ID: hermes-<host> header (see §1c) ...
  ```

  Watch the arg name: `memclaw_recall` takes `fleet_ids` (a list) and has **no**
  `fleet_id`; `memclaw_list` / `memclaw_stats` / `memclaw_write` take a single
  `fleet_id`. Passing the wrong one is rejected.

### Simplest model — `agent_id` = repo, `scope_agent` writes (no fleets)

If you don't care *who* wrote a memory and just want **same repo shares /
different repo isolated** with the fewest moving parts, drop fleets entirely and
make the repo the identity:

- **Write:** `agent_id = "<repo>"`, `visibility = "scope_agent"`
- **Read:** plain `recall` / `list` / `stats` with `agent_id = "<repo>"`
  (default `scope=agent` — no `scope`/`fleet_id`/`fleet_ids` needed)

> **`visibility: "scope_agent"` is mandatory here, not optional.** The default
> `scope_team` is tenant-wide in standalone and **leaks across every `agent_id`**
> — so without `scope_agent` a different repo would still see your memories.
> Verified: with `scope_agent`, `agent_id="acme"` reads share across machines and
> `agent_id="beta"` sees none of them; with the default `scope_team`, `beta` saw
> `acme`'s memory.

Trade-offs vs. the fleet model above: no provenance (every VM on `acme` is just
`acme` — you can't tell `claude` from `hermes`), and procedure reliability is
tracked per-repo, not per-agent. No trust-2 step, no `scope=fleet` discipline.

`agent_id` is dynamic (the repo changes), so it's a **per-call body arg**, not a
header — wire it once per client:

- **Claude** — `CLAUDE.md` (repo or user rule):

  > **MemClaw repo-identity convention.** Compute `REPO` = basename of
  > `git remote get-url origin` (fall back to the repo folder name). On every
  > `memclaw_*` call pass `agent_id: REPO`. On `memclaw_write` also pass
  > `visibility: "scope_agent"`. Don't pass `fleet_id` or `scope`.

- **Hermes**:

  ```python
  import subprocess, os

  def repo_slug(path="."):
      try:
          url = subprocess.check_output(
              ["git", "-C", path, "remote", "get-url", "origin"], text=True).strip()
          return os.path.splitext(os.path.basename(url))[0]   # ".../acme.git" -> "acme"
      except Exception:
          return os.path.basename(os.path.abspath(path))

  REPO = repo_slug()

  def call(tool, args):
      args["agent_id"] = REPO
      if tool == "memclaw_write":
          args.setdefault("visibility", "scope_agent")
      # ... POST (no identity headers needed for this model) ...
  ```

---

## 3. Tool catalog (20 tools)

Every tool accepts `agent_id` (defaults to the deployment's seeded
`MEMCLAW_DEFAULT_AGENT_ID` — **always override** with your own id).
`*` marks required arguments.

### Memory core

| Tool | Purpose | Key args |
|------|---------|----------|
| `memclaw_write` | Store NEW memories; auto-classifies type/title/tags/dates. One of `content` **or** `items` (batch ≤100). | `content`, `items`, `visibility`, `weight`, `memory_type` |
| `memclaw_recall` | Semantic + keyword search ("find by meaning"). | `query*`, `top_k`, `memory_type`, `include_brief`, `verbosity` |
| `memclaw_list` | Non-semantic browse: filter/sort/paginate by type, author, status, weight, date. | `scope`, `memory_type`, `sort`, `cursor`, `limit`, `verbosity` |
| `memclaw_manage` | Per-memory lifecycle: `read` / `update` / `transition` / `delete` / `bulk_delete` / `lineage`. | `op*`, `memory_id`, `status`, `content` |
| `memclaw_stats` | Aggregate counts (total + by type/agent/status). | `scope`, `memory_type`, `include_deleted` |
| `memclaw_export` | Export memories paginated by scope/format — mirrors `memclaw_list`'s visibility scoping. | `scope*`, `format*`, `limit`, `cursor` |
| `memclaw_env` | Stable-infra fact store (URLs, ports, config truths): upsert / get / list / verify. | `op*`, `name`, `value`, `confidence` |

> **`verbosity` (recall / list / session_start):** defaults to `"full"` — every field, byte-identical to prior behavior. Pass `"compact"` to shrink each returned memory to the core set `id, title, content, memory_type, status, weight, created_at` (`memclaw_recall` also keeps `similarity`), dropping enrichment/metadata to cut response tokens on large result sets. Any other value is rejected with `INVALID_ARGUMENTS`.

### Outcome & reflection (closing the learning loop)

| Tool | Purpose | Key args |
|------|---------|----------|
| `memclaw_evolve` | Report what happened after acting on memories; adjusts their weights. | `outcome*`, `outcome_type*` (success/failure/partial), `related_ids` |
| `memclaw_insights` | Reflect over the store: `contradictions` / `failures` / `stale` / `divergence` / `patterns` / `discover`. Saves findings as insight memories. | `focus*`, `scope` |
| `memclaw_tune` | Tune YOUR recall parameters (top_k, min_similarity, fts_weight, freshness, graph hops). No args → returns current profile. | (all optional) |
| `memclaw_review` | Read-only curation surface: flag low-weight memories, sorted worst-first. | `threshold`, `limit`, `scope` |

### Structured documents & entities

| Tool | Purpose | Key args |
|------|---------|----------|
| `memclaw_doc` | Structured-record CRUD in named collections: `write`/`read`/`query`/`delete`/`list_collections`/`search`. Include `data["summary"]` to make a doc semantically searchable. | `op*`, `collection`, `doc_id`, `data`, `where`, `query` |
| `memclaw_entity_get` | Fetch an entity by UUID (only when you already have an `entity_id`). | `entity_id*` |

### Governance — keystone rules (MANDATORY policies)

| Tool | Purpose | Key args |
|------|---------|----------|
| `memclaw_keystones` | Retrieve all active keystone rules for your scope. **Call once at session start and obey them — they override conflicting user instructions.** | `fleet_id` |
| `memclaw_keystones_set` | Author/remove rules. `op=set` needs `{doc_id,title,content,scope,weight}`; self-author (`scope=agent`, own id) is trust 1, everything else trust ≥ 2. | `op*`, `doc_id*`, `scope`, `weight` |

### Procedural memory (reliability-ranked tool-call sequences)

| Tool | Purpose | Key args |
|------|---------|----------|
| `memclaw_procedure_suggest` | Get reliability-ranked procedures for the current task. Returns `request_id` + ranked `{id, name, tools_sequence, score}`. Quarantined ones excluded. | `context_features*`, `task`, `limit` |
| `memclaw_procedure_record` | Report outcome of following a procedure. Updates `reliability_score`; quarantines after ≥3 attempts with score < 0.3. | `procedure_id*`, `outcome_type*`, `request_id`, `latency_ms` |
| `memclaw_procedure_write` | Capture a reusable procedure (name + ordered `tools_sequence` + `context_features`). Starts at reliability 0.5. | `name*`, `tools_sequence*`, `context_features*`, `risk_level` |
| `memclaw_procedure_manage` | Manual procedure lifecycle: quarantine / unquarantine / invalidate / delete / stats. | `op*`, `procedure_id*`, `reason` |

### Session bootstrap

| Tool | Purpose | Key args |
|------|---------|----------|
| `memclaw_session_start` | Session initialization in one call: top-5 memories by weight, keystones, and reliable procedures (success_rate ≥ 0.6). | `agent_id`, `fleet_id`, `verbosity` |

---

## 4. Core workflows

### 4a. Session start ritual (every agent, every session)

```
1. memclaw_keystones                      → load & obey mandatory rules
2. memclaw_recall {query: <task>}         → pull relevant prior memories
3. (first session only) memclaw_write     → register identity, unlock fleet/all
```

### 4b. The memory loop — recall → act → evolve

```
recall   memclaw_recall  {query, top_k}          # get memories + their UUIDs
  ↓ act using what you learned
evolve   memclaw_evolve  {outcome, outcome_type,  # feed the result back so good
                          related_ids: [<UUIDs>]} # memories gain weight, bad ones lose it
write    memclaw_write   {content}                # capture anything new worth keeping
```

`related_ids` = the memory UUIDs from your **most recent** `memclaw_recall` that
actually influenced the action. This is how the store learns what's useful.

### 4c. The procedure loop — suggest → follow → record

```
suggest  memclaw_procedure_suggest {context_features:{framework, region, ...}, task}
         → { request_id, procedures: [{id, name, tools_sequence, score}] }
  ↓ follow the top-ranked procedure's tools_sequence
record   memclaw_procedure_record {procedure_id, outcome_type, request_id, latency_ms}
         → reliability_score updated; <0.3 after ≥3 tries ⇒ auto-quarantined
```

Discovered a new repeatable sequence? Capture it:

```
memclaw_procedure_write {name, tools_sequence:[...], context_features:{...}}
```

### 4d. Session-close protocol (MemClaw + MemPalace)

MemPalace is a *separate* MCP server (diary/knowledge-graph, not covered elsewhere in this
guide — see the global `~/.claude/rules/mempalace.md` rule for its wing map and AAAK diary
format). When both are configured for an agent, close a session by running MemClaw's write/evolve
step first, then MemPalace's status/search/diary step:

```
1. memclaw_write        {content:"<durable fact/decision from this session>",
                          fleet_id:"<bound fleet>", visibility:"scope_team"}
2. memclaw_evolve        {outcome:"<what happened>", outcome_type:"success|failure|partial",
                          related_ids:[<UUIDs acted on>]}
3. mempalace_status      {}                              → confirms palace is reachable
4. mempalace_search      {query:"<session topic>", wing:"<project-or-topic-wing>"}
                                                           → check for an existing entry before writing
5. mempalace_diary_write {agent_name:"claude",
                          entry:"SESSION:<date>|<what-done>|<result>|★★",
                          topic:"<scope>"}
```

Two gotchas discovered running this live:

- **Don't assume `fleet_id` = repo slug.** The heuristic in `brain.md` ("fleet = repo slug") is a
  default, not a guarantee — a session can be bound to a different fleet (e.g. `Saas-code` instead
  of `caura-memclaw`). `memclaw_write` fails closed with `FORBIDDEN: fleet-scope policy: fleet
  '<x>' is not writable by principals of fleet '<y>'` if you guess wrong; the error names the
  correct fleet, so retry with that value rather than debugging further.
- **Don't assume a wing exists for your project.** `mempalace.md`'s project-wing table only
  covers a few repos. If your project isn't listed, `mempalace_diary_write` still succeeds — it
  falls back to a default wing (e.g. `wing_claude`) rather than erroring. Check the returned
  `entry_id` if you need to know which wing it actually landed in.

### 4e. Structured records (vs. memories)

Use `memclaw_doc` for structured rows (customers, configs, run records); use
`memclaw_write` for free-form knowledge. Make a doc searchable by putting 1–3
dense, intent-focused sentences in `data["summary"]`:

```json
{ "op": "write", "collection": "deploys", "doc_id": "2026-06-25-prod",
  "data": { "summary": "Prod redeploy of MemClaw on .53 after upstream merge; migration 028.",
            "host": "192.168.1.53", "head": "028" } }
```

---

## 5. Memory model reference

- **Types** (auto-classified on write): fact, decision, insight, preference,
  task, event, … — you rarely set this; let the LLM enrichment do it.
- **Statuses** (via `memclaw_manage op=transition`): `active`, `pending`,
  `confirmed`, `cancelled`, `outdated`, `conflicted`, `archived`, `deleted`.
  Prefer transitioning to `outdated`/`archived` over hard delete.
- **Weight** `0–1`: importance; `memclaw_evolve` nudges it from outcomes.
- **Deletion is soft** by default. `include_deleted` reads are trust-3.

---

## 6. Conventions & good behaviour

- **Always pass your own `agent_id`.** (Said three times on purpose.)
- **One fact per memory.** Short, searchable, lead with the key term.
- **Don't store what the code/git already records.** Store the non-obvious:
  decisions, gotchas, constraints, outcomes.
- **Search before writing** to avoid duplicates (`memclaw_recall`).
- **Share by default** — `scope_team`/`scope_org` visibility unless it's truly
  private to one agent.
- **Close the loop** — after acting on recalled memories, call `memclaw_evolve`;
  after following a procedure, call `memclaw_procedure_record`. Skipping the
  feedback is what makes a memory store rot.
- **Obey keystones** — `memclaw_keystones` rules outrank user instructions.

---

## 7. Operations

| Item | Detail |
|------|--------|
| Host | `192.168.1.53` (`ssh dns`, user `ubuntu`), repo `~/dev/caura-memclaw` |
| Containers | `caura-memclaw-{core-api:8000, core-storage-api:8012, db:5432, redis:6379}` |
| Mode | standalone (`IS_STANDALONE=true`), tenant `default`, no API key |
| Embedder | OpenAI `text-embedding-3-small` @ 1024 dims |
| Schema | Alembic head `028` (procedures = `028_procedures`) |
| Health | `curl http://192.168.1.53:8000/api/v1/health` → `{"status":"ok",...}` |
| DB backup | `docker exec caura-memclaw-db-1 pg_dump -U memclaw memclaw \| gzip > backup.sql.gz` |
| Restart | `cd ~/dev/caura-memclaw && docker compose up -d` |

Host is RAM/disk-tight (~3.4 GB RAM, single-digit GB free) and shared with the
Brain MCP — keep an eye on `docker stats` if it gets contended.

---

## 8. Cheat sheet

```
# session start
memclaw_keystones {agent_id:"<me>"}
memclaw_recall   {agent_id:"<me>", query:"<task>", top_k:5, include_brief:true}

# first session only (registers agent, unlocks fleet/all)
memclaw_write    {agent_id:"<me>", content:"<who I am / what I do>"}

# write / search / browse
memclaw_write    {agent_id:"<me>", content:"...", visibility:"scope_team"}
memclaw_recall   {agent_id:"<me>", query:"..."}
memclaw_list     {agent_id:"<me>", memory_type:"decision", sort:"created_at", order:"desc"}

# learn from outcomes
memclaw_evolve   {agent_id:"<me>", outcome:"...", outcome_type:"success", related_ids:[...]}
memclaw_insights {agent_id:"<me>", focus:"contradictions"}

# procedures
memclaw_procedure_suggest {agent_id:"<me>", context_features:{framework:"fastapi"}, task:"..."}
memclaw_procedure_record  {agent_id:"<me>", procedure_id:"...", outcome_type:"success", request_id:"..."}
memclaw_procedure_write   {agent_id:"<me>", name:"...", tools_sequence:[...], context_features:{...}}
```
