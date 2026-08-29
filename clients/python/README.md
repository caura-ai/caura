# caura-client

> Formerly `memclaw-client` — the old package name, `memclaw_client` import, and `MemClaw` class remain permanent aliases. <!-- legacy-name-ok: taught as legacy alias -->

Official Python client for [Caura](https://caura.ai) — governed shared
memory for AI agent fleets (multi-agent, multi-tenant, MCP-native).

A thin wrapper over the Caura REST API. Point it at a managed
(`https://caura.ai`) or self-hosted (`http://localhost:8000`) deployment.

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

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, ...)` | `POST /api/v1/memories` | `Memory` |
| `search(query, top_k=5, ...)` | `POST /api/v1/search` | `list[Memory]` |
| `recall(query, top_k=5, ...)` | `POST /api/v1/recall` | `RecallResult` |
| `health()` | `GET /api/v1/health` | `dict` |
| `get_document(doc_id, *, collection, ...)` | `GET /api/v1/documents/{doc_id}` | `dict` |
| `submit_interview(...)` | `POST /api/v1/interview/submit` | `dict` |
| `close()` | — | `None` |

The client is a context manager (`with Caura(...) as mc:`) and raises
`AuthError` (401/403), `NotFoundError` (404), or `CauraAPIError` on failures.
Every result also exposes the full API payload on `.raw`.

### Unknown fields on writes are rejected

`write()` forwards any extra keyword arguments straight into the request body
(`mc.write("...", some_field=1)`). The API rejects a field it does not declare
with **422** and names it:

```python
try:
    mc.write("a memory", tags=["alpha"])   # `tags` is not a write field
except CauraAPIError as exc:
    exc.payload["error"]["details"]["unknown_fields"]   # ["tags"]
```

This used to return `201` with the field silently discarded, so an integration
that "worked" may start failing here — the data it sent was never being stored.
Caller-owned keys belong under `metadata` (`mc.write("...", metadata={"tags": [...]})`).

`search()` and `recall()` are unaffected: filter bodies still accept unknown
fields, deliberately. See
[api-surfaces.md](https://github.com/caura-ai/caura/blob/main/docs/api-surfaces.md#request-body-contract-writes-are-strict-searches-are-not).

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

### `submit_interview()` is an Interviewer-internal surface

`submit_interview()` is used by the `caura-interviewer` adapter below to
submit parsed session windows to the server. It is not intended as a
general-purpose SDK method: it calls the server synchronously (the server
interviews the window in-line, up to a 90s budget), so its `timeout`
defaults to 120s rather than the client-wide 30s, and the returned body
carries an extra `"http_status"` key so callers can tell a `207` partial
from a `200` committed. New SDK users should not need it.

For credentials, scopes, and the full API surface, see the
[Caura docs](https://caura.ai/docs). Production fleets should use
[per-agent keys](https://caura.ai/docs/integrations/per-agent-keys).

## caura-interviewer — Claude Code + Cursor adapter

Installing this package also provides the `caura-interviewer` CLI (the
legacy `memclaw-interviewer` entrypoint is installed too and keeps working): the <!-- legacy-name-ok: taught as legacy alias -->
Caura Interviewer's disk-parser adapter for Claude Code and Cursor
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

Every `CAURA_*` variable above also answers to its pre-rename `MEMCLAW_*` name; where both are set the first non-empty value wins. <!-- legacy-name-ok: taught as legacy alias -->

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

Crash-safety is inherited from the Interviewer protocol: the watermark
advances only after the server commits a window, and retries of the same
window dedup server-side via a deterministic attempt id — never a gap,
never a duplicate.

## License

Apache-2.0
