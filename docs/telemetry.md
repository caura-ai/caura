# Telemetry: the anonymous daily heartbeat

*Last changed: 2026-09-19 (schema 1).*

A self-hosted Caura server sends **one anonymous heartbeat a day** to
`telemetry.caura.ai`. It tells us how many installs are alive, which
version they run, which providers they use and which SDK families talk to
them. Nothing else. This page is the contract: what is sent, why, how to
inspect it and every way to turn it off.

If you read nothing else: `CAURA_TELEMETRY=off` (or `DO_NOT_TRACK=1`) in the
environment of `core-api` turns it off for good. Opt-out is permanent for that
install: no re-prompting, no re-enabling on upgrade, no default flip in a
later release.

## Why

We ship an open-source server and have no idea how many people run it, on
what, or whether an upgrade broke half of them. Registry download counts
measure mirrors and CI. A daily heartbeat with bucketed counts is the
smallest signal that answers "is anyone out there, and are they still there
next week" without collecting anything about *what* they store.

The data is for internal use: it feeds the Growth screen in our ops tool and
nothing else. It is never sold, shared or joined with account data (a
self-hosted install has no account).

## What is sent

Exactly this shape, about 600 bytes, described by
[`telemetry-schema-v1.json`](telemetry-schema-v1.json). Every count is a
bucket string on one shared scale (`0`, `1`, `2-5`, `6-20`, `21-100`,
`101-1k`, `1k-10k`, `10k-100k`, `>100k`) so a small install cannot be
fingerprinted by exact numbers. Every provider is a closed enum: provider
*kind*, never a model name.

```json
{
  "schema": 1,
  "product": "caura-server",
  "deployment_id": "6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90",
  "sent_at": "2026-09-17T09:12:44Z",
  "version": "3.16.0",
  "runtime": { "python": "3.12", "os": "linux", "arch": "aarch64", "deploy": "docker" },
  "mode": { "standalone": true },
  "uptime_bucket": "1d-7d",
  "providers": {
    "embedding": "openai", "entity_extraction": "none", "rank": "noop",
    "event_bus": "inprocess", "redis": false, "sentry": false
  },
  "counts": {
    "memories": "1k-10k", "agents": "2-5", "tenants": "1",
    "plugin_nodes_7d": "1", "plugin_versions": ["2.21"]
  },
  "clients_24h": {
    "openclaw-plugin": "1", "caura-client-python": "1", "caura-client-node": "0",
    "caura-rail-python": "0", "caura-rail-node": "0", "mcp": "2-5", "other": "0"
  }
}
```

| Field | What it is | Where it comes from |
|---|---|---|
| `schema` | Always `1`. The collector rejects anything else. | constant |
| `product` | Always `caura-server`. | constant |
| `deployment_id` | Random `uuid4`, generated on the first send after boot, stored once, rotatable. Not derived from hardware, hostname or MAC. | `organization_settings` row `__deployment__` |
| `sent_at` | Clock at send, UTC. | clock |
| `version` | The core-api version string. A leading `v` is stripped, so the pinning form `CAURA_VERSION=v3.17.0` reports `3.17.0`. A source checkout with none of the sources set reports `dev`, which the collector excludes from every headline metric: set `CAURA_VERSION` if you want a source install counted. | `CAURA_VERSION` env, `/app/VERSION`, package metadata, else `dev` |
| `runtime.python` | Interpreter `major.minor`. No patch level. | `sys.version_info` |
| `runtime.os`, `runtime.arch` | `platform.system()`, `platform.machine()`, lower-cased. No kernel version, no distro. | `platform` |
| `runtime.deploy` | `docker` if `/app/VERSION` exists (the Dockerfile stamps it), else `source`. | filesystem |
| `mode.standalone` | Whether the server runs in standalone (single-tenant) mode. | `IS_STANDALONE` |
| `uptime_bucket` | `<1h`, `1h-1d`, `1d-7d`, `7d-30d`, `>30d`. Separates tire-kickers from installs. | process start time |
| `providers.embedding` | `none`, `openai`, `local` or `other`. `fake` is reported as `none`. The *configured* kind: a provider configured without a key still reports its kind even though the server fell back to the fake provider. | `EMBEDDING_PROVIDER` |
| `providers.entity_extraction` | `none`, `openai`, `anthropic`, `openrouter`, `gemini` or `other`. `fake` is reported as `none`. The configured kind, as above. | `ENTITY_EXTRACTION_PROVIDER` |
| `providers.rank` | `noop` when reranking is off, else the provider kind (`local` or `other`). | `RANK_ENABLED`, `RANK_PROVIDER` |
| `providers.event_bus` | `inprocess` or `pubsub`. | `EVENT_BUS_BACKEND` |
| `providers.redis`, `providers.sentry` | Whether a Redis URL / Sentry DSN is configured. Presence only, never the value. | `REDIS_URL`, `SENTRY_DSN` |
| `counts.memories`, `agents`, `tenants` | Bucketed totals, the same three storage calls `GET /api/v1/stats` makes. | storage |
| `counts.plugin_nodes_7d` | Bucketed number of OpenClaw plugin nodes that sent a fleet heartbeat in the last 7 days, summed over active tenants. A lower bound. | storage `GET /fleet/nodes/summary`, per tenant |
| `counts.plugin_versions` | Distinct plugin `major.minor` among those nodes, at most 10. | same |
| `clients_24h.*` | Authenticated requests since the last accepted heartbeat, bucketed per client family, matched on the `User-Agent` prefix, summed over every worker process of the container. Unknown agents fold into `other`; MCP requests count as `mcp` by transport. The raw header is never stored. | in-process counters, summed through the state directory |

### Never sent

Hostnames, IP addresses, tenant, agent, node or fleet names, emails, API
keys or any secret value, model names, memory content or metadata, exact
counts, the Sentry DSN, the storage URL, environment variable values. A test
walks every built payload and asserts each leaf is an allowlisted key with a
bucket, boolean or closed-enum value, and a second test builds a payload from
deliberately poisoned settings and asserts none of the poison appears.

The collector sees your IP address the way any HTTPS server does. It uses it
only inside a salted hash for a per-IP rate limit and never writes it to a
database or a log.

## When it runs

The heartbeat runs only when **every** row says on. Any single off wins, and
the reason is logged at boot and returned by `GET /api/v1/telemetry`.

| Condition | Result | Reason string |
|---|---|---|
| `CAURA_TELEMETRY` is `off`, `0` or `false` | off | `caura_telemetry_off` |
| `DO_NOT_TRACK` is set to anything but `0` | off | `do_not_track` |
| `CI` is set (non-empty) | off | `ci` |
| `GATEWAY_SHARED_SECRET` is set (behind the enterprise gateway) | off | `enterprise_gateway` |
| `PLATFORM_LLM_PROVIDER` or `PLATFORM_EMBEDDING_PROVIDER` is set (managed platform) | off | `managed_platform` |
| Running under pytest (`PYTEST_CURRENT_TEST`) | off | `pytest` |
| `CAURA_TELEMETRY_URL` is not `https://` (plain `http://` is allowed for `localhost`, `127.0.0.1` and `::1` only) | off | `invalid_endpoint_url` |
| Otherwise | **on** | |

Off means zero work: no background task is created, no HTTP client is built,
no DNS lookup happens, the state directory is not touched, and the client
counter is never incremented. A test patches `httpx.AsyncClient` and
`asyncio.create_task` and asserts neither is touched.

The `invalid_endpoint_url` row is the one an operator can hit by accident: a
mistyped override is decided at boot, printed at WARNING with the offending
URL, and shown by `GET /api/v1/telemetry` as `enabled: false` with that reason
and the `endpoint` it refused. Nothing is sent in clear text and nothing fails
silently.

## How to turn it off

Any one of these is enough, and each is permanent for that install.

- **`CAURA_TELEMETRY=off`** in `core-api`'s environment. In `.env`, set
  `CAURA_TELEMETRY=off`; in `docker-compose.yml`, uncomment the
  `CAURA_TELEMETRY: "off"` line under `core-api`.
- **`DO_NOT_TRACK=1`**: the
  [Console Do Not Track](https://consoledonottrack.com/) convention. If you
  already set it globally you were never counted.
- **`CI=true`** (or any non-empty value), which every CI system sets.
- **Block the host.** The only destination is `telemetry.caura.ai` on
  port 443. A firewall rule that drops it costs nothing on the server side:
  one attempt per day, a 5 second timeout, one warning-level log line, no
  retry.
- **Run behind the enterprise gateway or with platform providers.** Both
  switch it off by themselves.

## Cadence and delivery

- First send at boot + 5 minutes plus up to 60 seconds of jitter. The boot
  log line prints first, so an operator who reads it and sets the switch is
  never counted.
- Then every 24 hours ± 60 minutes, computed per cycle.
- One attempt per cycle: `POST https://telemetry.caura.ai/api/telemetry/heartbeat`
  with a 5 second total timeout. A failure (connection refused, timeout, a
  non-202 answer) is logged once at WARNING, recorded as `last_error` and
  `last_attempt_at` for `GET /api/v1/telemetry`, and the loop waits for the
  next cycle. No retry, no queue, no persistence of unsent payloads.
- **One beat per container, whatever the worker count.** The Docker image runs
  uvicorn with two workers; they elect a leader through a lock file in
  `CAURA_TELEMETRY_STATE_DIR` (default `<system temp dir>/caura-heartbeat`,
  created `0700`). The leader runs the loop; the other workers only count
  clients and flush their counters to that directory every 30 seconds, so the
  beat carries the sum over all workers. If the leader dies, another worker
  takes the lock within a minute and resumes the schedule. Every worker
  answers `GET /api/v1/telemetry` with the leader's values. If the directory
  is not writable the server falls back to one loop per worker and says so at
  WARNING on boot; set the variable empty to choose that deliberately.
- Every *container* (replica) of `core-api` runs its own loop with the shared
  `deployment_id`; the collector keeps one heartbeat per deployment per 20
  hours and drops the rest.
- The request carries `Authorization: Bearer <deployment_token>` and
  `User-Agent: caura-server/<version>`. The token is 32 random bytes generated
  with the id; its only power is to send heartbeats for that id, which is why
  it lives beside the id in the settings row and not in a secret store.
- A plain `http://` collector URL to anything but `localhost` (or `127.0.0.1`,
  `::1`) switches the heartbeat off at boot (`invalid_endpoint_url`, see "When
  it runs"), so a mistyped override cannot send in clear text and cannot be
  mistaken for a working one.

## Inspecting it

`GET /api/v1/telemetry` (normal API-key auth) returns the decision, the
reason if off, the deployment id, the collector URL, the last attempt, the
last and next send times, the last HTTP status, the last error (a short
string, `null` after a success), the one-line disable hint and
`payload_preview`: exactly what the next send would contain, built the same
way, with the client counts summed over every worker. Every worker of a
container gives the same answer. When the heartbeat is off the preview is
`null`: a disabled install does no work, not even to show you what it would
have sent.

```json
{
  "enabled": true,
  "reason": null,
  "deployment_id": "6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90",
  "endpoint": "https://telemetry.caura.ai/api/telemetry/heartbeat",
  "last_attempt_at": "2026-09-16T09:11:02Z",
  "last_sent_at": "2026-09-16T09:11:02Z",
  "last_status": 202,
  "last_error": null,
  "next_send_at": "2026-09-17T09:12:44Z",
  "payload_preview": { "schema": 1, "product": "caura-server", "...": "..." },
  "disable": "CAURA_TELEMETRY=off or DO_NOT_TRACK=1"
}
```

The boot log carries the same information on every start:

```
[telemetry] anonymous heartbeat ON: one ping a day to telemetry.caura.ai. Disable: CAURA_TELEMETRY=off or DO_NOT_TRACK=1. Inspect: GET /api/v1/telemetry. What is sent: https://github.com/caura-ai/caura/blob/main/docs/telemetry.md
[telemetry] anonymous heartbeat follower: worker pid 12 counts clients only; another worker of this container sends the one ping a day.
[telemetry] anonymous heartbeat OFF (do_not_track).
[telemetry] anonymous heartbeat OFF (invalid_endpoint_url): refusing to send to 'http://example.com/x'. CAURA_TELEMETRY_URL must be https:// (plain http is allowed for localhost only).
```

A multi-worker container prints one ON line and one follower line per
additional worker; the last line is a WARNING, the others are INFO.

## Starting over

`POST /api/v1/telemetry/rotate` (same auth; refused to the demo sandbox,
read-only and agent-scoped credentials, since it rewrites the install's
identity) writes a new `deployment_id` and token. The old id simply goes stale on the collector side after 30 days
without a beat; there is no link between the two. Use it if you cloned a
database into a new install and want the two counted separately, or if you
just want a clean slate.

## Pointing it somewhere else

`CAURA_TELEMETRY_URL` overrides the collector, for tests and for operators
who want the beat to land on their own endpoint. It must be `https://`
unless the host is `localhost`, `127.0.0.1` or `::1`; anything else switches
the heartbeat off with reason `invalid_endpoint_url` (a WARNING at boot, and
visible in `GET /api/v1/telemetry`).

`CAURA_TELEMETRY_STATE_DIR` is the per-container directory the workers
coordinate through (leader lock, per-worker client counters, the leader's
status). It holds no payload data and nothing that identifies the install
beyond the `deployment_id` already in your database.

## Retention

Raw heartbeat rows are purged after 13 months. Daily aggregates (active
installs, version mix, provider mix, client mix, all bucketed) are kept.

## Schema and change policy

The schema is a contract. New fields require a schema bump, an entry in
this change log, a `CHANGELOG.md` entry and a minor release. Nothing is added
silently, the default never flips, and an opt-out is never re-asked.

## Change log

- **2026-09-19**: no schema change. One beat per container regardless of
  worker count (leader election between uvicorn workers; the Docker image's
  two workers used to send two beats per cycle), client counts summed over
  workers, `last_error` / `last_attempt_at` added to `GET /api/v1/telemetry`,
  delivery failures logged at WARNING, a plain-http collector URL is now an
  off reason (`invalid_endpoint_url`) instead of a silent refusal, and a
  leading `v` is stripped from the version.
- **2026-09-17**: schema 1. First release with the heartbeat (`feat(core-api)`,
  next minor). The README's previous "no phone-home" statement was rewritten in
  the same change.
