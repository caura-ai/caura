# Caura — governed shared memory for AI agent fleets

Caura (formerly MemClaw) is an Agent DB — governed shared memory for AI agent fleets. Agents commit what they learn once; every agent in the fleet recalls it through MCP tools, REST, or Caura Rail (preview), subject to tenant isolation, visibility scope (scope_agent / scope_team / scope_org) and caller trust level. <!-- legacy-name-floor: taught as the former name -->

`caura-client` is the official Python client for Caura and the canonical
package name; [`caura`](https://pypi.org/project/caura/) and
[`caura-sdk`](https://pypi.org/project/caura-sdk/) install the same client.
It is a thin wrapper over the Caura REST API. Point it at a managed
(`https://caura.ai`) or self-hosted (`http://localhost:8000`) deployment.

> Formerly `memclaw-client`. That name is now only a redirect shell: installing it gives you this package and no `memclaw_client` module, so move imports to `caura_client` and the retired `MemClaw`/`MemClawError`/`MemClawAPIError` names to the `Caura` class and `CauraAPIError` below. <!-- legacy-name-floor: taught as legacy alias -->

## Install

```bash
pip install caura-client
```

## Quickstart

```python
from caura_client import Caura

# Recommended: the client is a context manager and closes its HTTP
# connection on exit (use close() for manual management).
with Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent") as mc:
    # Write a memory — enriched server-side with type, title, tags, importance.
    mc.write("Q3 revenue target is $4M, set on 2026-04-15.")

    # Search (ranked raw results)
    for m in mc.search("Q3 revenue target", top_k=5):
        print(m.title, "—", m.content)

    # Recall (LLM-synthesized context brief)
    print(mc.recall("Q3 revenue target").summary)
```

Self-hosted? Pass `base_url`:

```python
with Caura("standalone", tenant_id="default", base_url="http://localhost:8000") as mc:
    ...
```

Plain `http://` sends the API key in clear, so the client allows it only to a
loopback host (`localhost`, `127.0.0.0/8`, `::1`) and otherwise raises
`ValueError` naming the host. Use `https://`, or pass `allow_insecure_http=True`
(or set `CAURA_ALLOW_INSECURE_HTTP=true`) to accept the risk, e.g. on a trusted
private network.

New to Caura? Start with [what Caura is](https://caura.ai/docs) and the
[LongMemEval benchmark harness](https://github.com/caura-ai/caura-longmemeval).
To reach the same memory from an MCP client with no SDK, see the
[caura](https://pypi.org/project/caura/) page. Source and issues live at
[github.com/caura-ai/caura](https://github.com/caura-ai/caura).

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, ...)` | `POST /api/v1/memories` | `Memory` |
| `search(query, top_k=5, ...)` | `POST /api/v1/search` | `SearchResult` (a `list[Memory]`) |
| `recall(query, top_k=5, ...)` | `POST /api/v1/recall` | `RecallResult` |
| `health()` | `GET /api/v1/health` | `dict` |
| `get_document(doc_id, *, collection, ...)` | `GET /api/v1/documents/{doc_id}` | `dict` |
| `submit_interview(...)` | `POST /api/v1/interview/submit` | `dict` |
| `close()` | — | `None` |

The client is a context manager (`with Caura(...) as mc:`) and raises
`AuthError` (401/403), `NotFoundError` (404), or `CauraAPIError` on HTTP failures.
Network failures and timeouts raise `TransportError`, with the original `httpx`
exception in `__cause__`. Catch `CauraError` to handle both HTTP and transport
failures. Transport errors have no HTTP status code; requests are not retried.
Every result also exposes the full API payload on `.raw`. `search()` returns a
`SearchResult`: a list of `Memory` that also carries the response's
`recall_tracked`, `diagnostic` (set by `diagnostic=True`) and `warnings` (for
example a parameter the server ignored), and the whole body on `.raw`.

`recall()` reads the brief's `memories` and asks the server not to repeat them
under `items` (`items_alias=False`), which halves the response. Pass
`items_alias=True` if you read `raw["items"]`.

### Reading as an agent with a tenant key

With a tenant-scoped key, `search()` and `recall()` read as the tenant, so they
do not return any agent's `scope_agent` memories, including ones this client
wrote with `agent_id`. Pass `caller_agent_id` to read as that agent:

```python
with Caura("mc_tenant_key", tenant_id="my-team", agent_id="my-agent") as mc:
    mine = mc.search("deploy checklist", caller_agent_id=mc.agent_id)
```

The server then treats the read as that agent's: it registers the agent if it
is new, and holds the read to the agent's fleet and trust level, so a trust-1
agent reads its own fleet and is refused another. It is opt-in, never sent from
`agent_id` alone. An agent-scoped key already reads as its agent, and may only
name itself here.

### Fetching a document

`get_document()` returns the full `DocOut` envelope — the stored record is
nested under the `"data"` key, not returned directly. `collection` is a
required keyword-only argument, and a missing document raises
`NotFoundError`:

```python
with Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent") as mc:
    doc = mc.get_document("doc-123", collection="interviews")
    record = doc["data"]       # the stored record lives under "data"
```

### Lifecycle

`Caura` holds an `httpx.Client`, so prefer the `with` form above — the
connection is closed on exit. For manual management, call `close()`
explicitly when you are done:

```python
mc = Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent")
try:
    mc.write("...")
finally:
    mc.close()
```

### Request headers

Every request carries `X-API-Key` (your key), `Content-Type: application/json`
and a `User-Agent` of the form
`caura-client-python/<version> (python/<major>.<minor>)`. The `User-Agent`
lets a Caura server count which SDK families talk to it; it names only the
package, its version and the Python release. The client sends nothing to any
host other than the `base_url` you configure.

### `submit_interview()` is an Interviewer-internal surface

`submit_interview()` is used by the `caura-interviewer` adapter below to
submit parsed session windows to the server. It is not intended as a
general-purpose SDK method. By default the server stores the window, advances
its watermark and answers `200` with `"status": "accepted"` and
`memories_written` 0: the memories are written in the background, after the
response. A server with `interview_async_submit` turned off interviews the
window in-line, up to a 90s budget, and answers `200` committed or `207`
partial with the count; that is why `timeout` defaults to 120s rather than the
client-wide 30s. The returned body carries an extra `"http_status"` key. New
SDK users should not need it.

For credentials, scopes, and the full API surface, see the
[Caura docs](https://caura.ai/docs). Production fleets should use
[per-agent keys](https://caura.ai/docs/integrations/per-agent-keys).

### Unknown fields on writes are rejected

`write()` forwards any extra keyword arguments straight into the request body
(`mc.write("...", some_field=1)`). The API rejects a field it does not declare
with **422** and names it:

```python
try:
    mc.write("a memory", tags=["alpha"])   # `tags` is not a write field
except CauraAPIError as exc:
    exc.details["unknown_fields"]   # ["tags"]
```

This used to return `201` with the field silently discarded, so an integration
that "worked" may start failing here — the data it sent was never being stored.
Caller-owned keys belong under `metadata` (`mc.write("...", metadata={"tags": [...]})`).

`search()` and `recall()` are unaffected: filter bodies still accept unknown
fields, deliberately. See
[api-surfaces.md](https://github.com/caura-ai/caura/blob/main/docs/api-surfaces.md#request-body-contract-writes-are-strict-searches-are-not).

## caura-interviewer — Claude Code + Cursor adapter

Installing this package provides the `caura-interviewer` CLI. When upgrading an
older install, run `caura-interviewer install`; for one release it can find and replace
an existing `memclaw-interviewer` crontab entry. <!-- legacy-name-floor: migration instruction for the retired entry point -->
The Caura Interviewer is a disk-parser adapter for Claude Code and Cursor
workstations. It reads agent session transcripts **read-only** — Claude
Code's `~/.claude/projects/…/*.jsonl` or Cursor's
`~/.cursor/projects/…/agent-transcripts/…/*.jsonl` — tracks a per-file
cursor via the server's forward-only watermark documents (no local state),
and submits event windows to `POST /api/v1/interview/submit`, where Caura
synthesizes them into typed memories. Requires the tenant to have
`interviewer.enabled = true`.

```bash
export CAURA_API_KEY=mc_xxx CAURA_TENANT_ID=my-team
export CAURA_INTERVIEWER_PROJECTS="-Users-me-work-*"     # allowlist, default-deny

caura-interviewer status --since-hours 24       # cursors vs. local line counts
caura-interviewer run --dry-run -v              # parse + window, submit nothing
caura-interviewer run --max-windows 8           # submit due windows
caura-interviewer run --harness cursor          # harvest Cursor instead (or
                                                # CAURA_INTERVIEWER_HARNESS=cursor)
```

Every `CAURA_*` variable above also answers to its pre-rename `MEMCLAW_*` name; where both are set the first non-empty value wins. <!-- legacy-name-floor: taught as legacy alias -->

**Privacy:** default-deny — with no allowlist the CLI lists discovered
project dirs and exits with guidance; `--all-projects` is the explicit
opt-in. Credential-shaped strings are scrubbed locally before anything
leaves the machine, and the server masks PII again on receipt.

**Triggers:** run it from cron, or wire the harness's session-end hook so a
session is interviewed the moment it ends (a failed harvest never fails
the session — the hook always exits 0). The SAME hook command serves both
harnesses: each sends `transcript_path` on stdin, and the harness is
inferred from the path shape.

Claude Code (`~/.claude/settings.json`):
```json
{ "hooks": { "SessionEnd": [ { "hooks": [
  { "type": "command", "command": "caura-interviewer hook", "timeout": 300 }
] } ] } }
```

Cursor (`~/.cursor/hooks.json`):
```json
{ "version": 1, "hooks": {
  "sessionEnd": [ { "command": "caura-interviewer hook" } ]
} }
```

**Schedule the cron in one command.** Rather than hand-editing crontab,
`install` writes an idempotent cron entry (and a `0600` env file it sources,
since cron doesn't inherit your shell environment). Config comes from the
same flags/env as `run`:
```bash
caura-interviewer install --interval 30m          # add --harness cursor for Cursor
caura-interviewer uninstall                        # removes the entry + env file
```
It refuses to schedule a job that would no-op (missing credentials or no
project allowlist). On Windows (no `crontab`), use Task Scheduler to run
`caura-interviewer run` on a timer instead.

A run ends with a one-line summary. On a default server it reports windows
"accepted for synthesis in the background", since the memories are written
after the response; "memories written" counts what a synchronous server wrote
in-line.

Crash-safety is inherited from the Interviewer protocol: the watermark
advances only after the server has stored a window, and retries of the same
window dedup server-side via a deterministic attempt id, so each window is
stored once. Its memories come from the server's background synthesis. A
window whose synthesis keeps failing is parked server-side for an operator;
its watermark has already moved, so this adapter does not send it again.

## License

Apache-2.0
