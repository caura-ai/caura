# core-api scripts

## `gen_broker_openapi.py` — frozen v1 broker OpenAPI contract

`../openapi.broker.json` is the **frozen v1 contract** for the broker-facing
gateway operations that memclawd (the on-prem broker) calls against Caura
cloud:

| Method | Path                            | Broker caller (`internal/cloud`) |
| ------ | ------------------------------- | -------------------------------- |
| POST   | `/api/v1/memories/bulk`         | `SaveMemory` (`memory_save`, audit mirror) |
| POST   | `/api/v1/search`                | `Search` (`memory_search`)       |
| GET    | `/api/v1/health`                | health probe                     |
| GET    | `/api/v1/version`               | version handshake                |
| GET    | `/api/v1/memories`              | `ListMemories` (`memory_list`)   |
| GET    | `/api/v1/memories/{memory_id}`  | `GetMemory` (`memory_recall`)    |
| PATCH  | `/api/v1/memories/{memory_id}`  | `UpdateMemory` (`memory_update`) |
| DELETE | `/api/v1/memories/{memory_id}`  | `DeleteMemory` (`memory_delete`) |

Selection is **method-level** (`BROKER_OPERATIONS` in
`gen_broker_openapi.py`), not path-level: `/api/v1/memories` also serves POST
and DELETE, which the broker never calls, and gating those would fail this gate
on unrelated changes.

The baseline is a **subset** of core-api's full (~91-path) OpenAPI surface:
only these operations plus the schema / security components they reach. Its
`info` is normalized to a fixed contract identity (`CONTRACT_VERSION = "v1"`,
not core-api's rolling package version), so it changes **only when a broker
endpoint's shape changes** — not on every release.

### Adding a broker endpoint — the cross-repo obligation

**Nothing in this repo can detect that the broker started calling a new cloud
endpoint.** The gate only protects operations listed in `BROKER_OPERATIONS`;
one that is missing is simply unguarded, silently. That is how the last four
rows above went unprotected — the broker's MCP dispatcher was wired to serve
`memory_recall` / `memory_list` / `memory_update` / `memory_delete`, and the
baseline was never widened to match.

So when a new `internal/cloud` client method lands in memclawd against a
core-api endpoint, add it here in the same change. Adding a row is **additive**
— it only widens what the gate protects, so it does not move
`CONTRACT_VERSION`. Removing one, or renaming a path/method the broker calls,
is a breaking contract change and does.

Endpoints the broker calls that live in the **enterprise** stack
(`/api/v1/installs/*` in platform-auth-api, `POST /api/v1/audit-log` in
platform-audit-api) have their own per-service baselines in that repo. As of
2026-08-11 those cover claim / heartbeat / policy-stream / register and the
audit-log write, but **not** `POST /api/v1/installs/leave` or
`POST /api/v1/installs/commands/ack`, which the broker also calls.

### Regenerate

Run from the `core-api/` directory (the repo root must be on `PYTHONPATH` for
the `common` package):

```bash
cd core-api
PYTHONPATH=.. uv run python scripts/gen_broker_openapi.py   # rewrite the baseline
```

Then commit `core-api/openapi.broker.json`.

`--check` regenerates in memory and fails (exit 1) if the committed baseline is
stale; `--out PATH` writes elsewhere instead of the committed file.

### CI gate (see `.github/workflows/ci.yml`)

1. **Freshness** — `gen_broker_openapi.py --check` fails if the committed
   baseline drifts from the code on the branch.
2. **Breaking change** — `oasdiff breaking` (pinned `tufin/oasdiff` Docker
   image) compares **main's** baseline against **this branch's** baseline and
   fails the PR on any breaking change. On the first merge (no baseline on main
   yet) the gate skips gracefully.

**A breaking change to this contract is not allowed to merge as-is.** Breaking
the broker↔cloud API requires a deliberate contract version bump
(`CONTRACT_VERSION`) per the broker↔cloud API-versioning RFC — not a silent
edit to a v1 endpoint.
