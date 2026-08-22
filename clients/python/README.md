# caura-client

> Formerly `memclaw-client` — the old package name, `memclaw_client` import, and `MemClaw` class remain permanent aliases.

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

mc = Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent")

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
mc = Caura("standalone", tenant_id="default", base_url="http://localhost:8000")
```

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, ...)` | `POST /api/v1/memories` | `Memory` |
| `search(query, top_k=5, ...)` | `POST /api/v1/search` | `list[Memory]` |
| `recall(query, top_k=5, ...)` | `POST /api/v1/recall` | `RecallResult` |
| `health()` | `GET /api/v1/health` | `dict` |

The client is a context manager (`with Caura(...) as mc:`) and raises
`AuthError` (401/403), `NotFoundError` (404), or `CauraAPIError` on failures.
Every result also exposes the full API payload on `.raw`.

For credentials, scopes, and the full API surface, see the
[Caura docs](https://caura.ai/docs). Production fleets should use
[per-agent keys](https://caura.ai/docs/integrations/per-agent-keys).

## memclaw-interviewer — Claude Code + Cursor adapter

Installing this package also provides the `memclaw-interviewer` CLI: the
Caura Interviewer's disk-parser adapter for Claude Code and Cursor
workstations. It reads agent session transcripts **read-only** — Claude
Code's `~/.claude/projects/…/*.jsonl` or Cursor's
`~/.cursor/projects/…/agent-transcripts/…/*.jsonl` — tracks a per-file
cursor via the server's forward-only watermark documents (no local state),
and submits event windows to `POST /api/v1/interview/submit`, where Caura
synthesizes them into typed memories. Requires the tenant to have
`interviewer.enabled = true`.

```bash
export MEMCLAW_API_KEY=mc_xxx MEMCLAW_TENANT_ID=my-team
export MEMCLAW_INTERVIEWER_PROJECTS="-Users-me-work-*"   # allowlist, default-deny

memclaw-interviewer status --since-hours 24     # cursors vs. local line counts
memclaw-interviewer run --dry-run -v            # parse + window, submit nothing
memclaw-interviewer run --max-windows 8         # submit due windows
memclaw-interviewer run --harness cursor        # harvest Cursor instead (or
                                                # MEMCLAW_INTERVIEWER_HARNESS=cursor)
```

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
  { "type": "command", "command": "memclaw-interviewer hook", "timeout": 300 }
] } ] } }
```

Cursor (`~/.cursor/hooks.json`):
```json
{ "version": 1, "hooks": {
  "sessionEnd": [ { "command": "memclaw-interviewer hook" } ]
} }
```

**Schedule the cron in one command.** Rather than hand-editing crontab,
`install` writes an idempotent cron entry (and a `0600` env file it sources,
since cron doesn't inherit your shell environment). Config comes from the
same flags/env as `run`:
```bash
memclaw-interviewer install --interval 30m        # add --harness cursor for Cursor
memclaw-interviewer uninstall                      # removes the entry + env file
```
It refuses to schedule a job that would no-op (missing credentials or no
project allowlist). On Windows (no `crontab`), use Task Scheduler to run
`memclaw-interviewer run` on a timer instead.

Crash-safety is inherited from the Interviewer protocol: the watermark
advances only after the server commits a window, and retries of the same
window dedup server-side via a deterministic attempt id — never a gap,
never a duplicate.

## License

Apache-2.0
