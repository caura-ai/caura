# core-api scripts

## `gen_broker_openapi.py` — frozen v1 broker OpenAPI contract

`../openapi.broker.json` is the **frozen v1 contract** for the four
broker-facing gateway endpoints that memclawd (the on-prem broker) calls
against Caura cloud:

| Method | Path                     |
| ------ | ------------------------ |
| POST   | `/api/v1/memories/bulk`  |
| POST   | `/api/v1/search`         |
| GET    | `/api/v1/health`         |
| GET    | `/api/v1/version`        |

The baseline is a **subset** of core-api's full (~91-path) OpenAPI surface:
only these four paths plus the schema / security components they reach. Its
`info` is normalized to a fixed contract identity (`CONTRACT_VERSION = "v1"`,
not core-api's rolling package version), so it changes **only when a broker
endpoint's shape changes** — not on every release.

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
