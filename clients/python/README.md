# memclaw-client

Official Python client for [MemClaw](https://memclaw.net) — governed shared
memory for AI agent fleets (multi-agent, multi-tenant, MCP-native).

A thin wrapper over the MemClaw REST API. Point it at a managed
(`https://memclaw.net`) or self-hosted (`http://localhost:8000`) deployment.

## Install

```bash
pip install memclaw-client
```

## Quickstart

```python
from memclaw_client import MemClaw

mc = MemClaw("mc_xxx", tenant_id="my-team", agent_id="my-agent")

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
mc = MemClaw("standalone", tenant_id="default", base_url="http://localhost:8000")
```

## API

| Method | Endpoint | Returns |
|---|---|---|
| `write(content, ...)` | `POST /api/v1/memories` | `Memory` |
| `search(query, top_k=5, ...)` | `POST /api/v1/search` | `list[Memory]` |
| `recall(query, top_k=5, ...)` | `POST /api/v1/recall` | `RecallResult` |
| `health()` | `GET /api/v1/health` | `dict` |

The client is a context manager (`with MemClaw(...) as mc:`) and raises
`AuthError` (401/403), `NotFoundError` (404), or `MemClawAPIError` on failures.
Every result also exposes the full API payload on `.raw`.

For credentials, scopes, and the full API surface, see the
[MemClaw docs](https://memclaw.net/docs). Production fleets should use
[per-agent keys](https://memclaw.net/docs/integrations/per-agent-keys).

## memclaw-interviewer — Claude Code adapter (Interviewer Phase 2)

Installing this package also provides the `memclaw-interviewer` CLI: the
MemClaw Interviewer's disk-parser adapter for Claude Code workstations. It
reads Claude Code session transcripts (`~/.claude/projects/…/*.jsonl`)
**read-only**, tracks a per-file cursor via the server's forward-only
watermark documents (no local state), and submits event windows to
`POST /api/v1/interview/submit`, where MemClaw synthesizes them into typed
memories. Requires the tenant to have `interviewer.enabled = true`.

```bash
export MEMCLAW_API_KEY=mc_xxx MEMCLAW_TENANT_ID=my-team
export MEMCLAW_INTERVIEWER_PROJECTS="-Users-me-work-*"   # allowlist, default-deny

memclaw-interviewer status --since-hours 24     # cursors vs. local line counts
memclaw-interviewer run --dry-run -v            # parse + window, submit nothing
memclaw-interviewer run --max-windows 8         # submit due windows
```

**Privacy:** default-deny — with no allowlist the CLI lists discovered
project dirs and exits with guidance; `--all-projects` is the explicit
opt-in. Credential-shaped strings are scrubbed locally before anything
leaves the machine, and the server masks PII again on receipt.

**Triggers:** run it from cron, or wire Claude Code's SessionEnd hook so a
session is interviewed the moment it ends (a failed harvest never fails
the session — the hook always exits 0):

```json
{ "hooks": { "SessionEnd": [ { "hooks": [
  { "type": "command", "command": "memclaw-interviewer hook", "timeout": 300 }
] } ] } }
```

Crash-safety is inherited from the Interviewer protocol: the watermark
advances only after the server commits a window, and retries of the same
window dedup server-side via a deterministic attempt id — never a gap,
never a duplicate.

## License

Apache-2.0
