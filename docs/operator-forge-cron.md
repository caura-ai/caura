# Skill Factory · Forge cron setup

The Forge worker mines fresh skill candidates per tenant; the promoter
flows clean candidates to `staged`. Both run inside a single cron tick.

## How the schedule works

The autonomous scheduler is a thin wrapper over the existing lifecycle
fanout pattern:

1. **External scheduler** (Cloud Scheduler / k8s CronJob / GitHub
   Actions cron / `cron` in the deploy box) hits
   `POST /admin/lifecycle/fanout/forge-distill` periodically.
2. **`core-api`** lists tenants with `skills_factory.enabled=true` and
   publishes one `caura.lifecycle.forge-distill-requested` event per
   tenant.
3. **The in-process consumer** in `core-api` (or `core-worker` in
   SaaS deployments) invokes `run_forge_cron_tick` for the tenant.
4. **One lifecycle_audit row per tenant per tick** records the work done
   as one number, `stats.candidates_produced` (candidates written plus
   promoted). The per-tick breakdown — scanned, held, auto-approved and the
   skip buckets — is in the structured `forge cron tick:` log line.

## Operator dial: `forge.cron_interval_hours`

The setting `org_settings.skills_factory.forge.cron_interval_hours`
(default `6`) is **informational** — the actual cadence is set by
whatever external system drives the fanout endpoint. Set the external
schedule to match this value. Nothing reads the setting, and no tick or
candidate records it: it notes the intended schedule for whoever reads
the settings. To see which tick minted a candidate, read its
`origin.run_id`, the tick's run label.

## Required external schedule entry

Pass `dedup_window_hours` with a value just under the schedule's interval
(`5.5` for every 6 hours). The consumer skips an org that already ran within
its dedup window, which defaults to 23 hours for daily schedules; without the
parameter a 6-hourly schedule runs once a day.

### Google Cloud Scheduler

```yaml
name: forge-cron-fanout
schedule: "0 */6 * * *"   # every 6 hours
time_zone: "UTC"
http_target:
  http_method: POST
  uri: https://<core-api-host>/api/v1/admin/lifecycle/fanout/forge-distill?dedup_window_hours=5.5
  oidc_token:
    service_account_email: <core-operations-sa>@<project>.iam.gserviceaccount.com
  headers:
    X-API-Key: ${ADMIN_API_KEY}   # the admin key — see core-api/auth.enforce_admin
```

### Kubernetes CronJob (alternative)

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: forge-cron-fanout
spec:
  schedule: "0 */6 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: curl
              image: curlimages/curl:latest
              command:
                - sh
                - -c
                - |
                  curl -fsS \
                    -X POST \
                    -H "X-API-Key: $ADMIN_API_KEY" \
                    "$CORE_API_BASE_URL/api/v1/admin/lifecycle/fanout/forge-distill?dedup_window_hours=5.5"
              envFrom:
                # whichever secret you use, it must expose ADMIN_API_KEY
                - secretRef: { name: caura-admin }
          restartPolicy: OnFailure
```

## What the operator sees

After the schedule lands, every tick produces:

- **Per-tenant audit rows** under `lifecycle_audit`
  (`action='forge-distill'`, `status='success' | 'failure'`,
  `stats.candidates_produced` = candidates written + promoted). The full
  breakdown (`candidates_written`, `promoted`, `scanned`, `held`, the
  `skipped_*` buckets, ...) is in the `forge cron tick:` log line.
- **Fresh candidate docs** at `documents.collection='skills'`,
  `data.status='candidate'`, `data.source='forge'`.
- **Inbox cards** for any candidate that the 6 auto-gates promoted to
  `staged` in the same tick.

## Same-tick promotion

`run_forge_cron_tick` runs `run_forge_distill` **then**
`promote_pending_candidates` on the same DB session. A candidate that
passes all 6 auto-gates (volume, diversity, freshness, poison, scan,
hash-binding) lands in `staged` within the same tick — operators
don't have to wait a second cron firing.

## Explicit activation review

Every passing Forge candidate lands `staged`, including candidates with no
scan findings. The former `skills_factory.sentinel.auto_promote_clean` opt-in
is retired: new settings writes refuse it, display omits legacy values, and
stored `true` values no longer bypass review. `auto_approved` remains in the
cron result for compatibility and is always zero.

A named organization admin or the system-admin credential approves a candidate
through the Skills Inbox with the `expected_content_hash` shown during review.
The endpoint rescans the current bytes, then writes activation and the server's
principal/time/hash stamp together, only if the reviewed status/hash/storage
version still match. A stale review returns `409`; a missing Forge hash returns
`422`. Fatal scan findings still refuse, and quarantine overrides still need a
reason. Generic REST/MCP writes cannot substitute for Forge approval.

Already-active skills are left unchanged, with no inferred approval backfill
or automatic deactivation. Activation and node delivery approval remain separate.
See [Skills Inbox API](skills-inbox-api.md#approve--staged--active).

## Dedup safety

Each delivery skips an org that already ran successfully within its dedup
window, measured from each run's tick. The window is the request's
`dedup_window_hours`, or 23 hours without it. With the schedule above
(`dedup_window_hours=5.5`, every 6 hours), every tick runs a full distill.
Re-curling the fanout endpoint within 5.5 hours of a tenant's successful tick
is a no-op for that tenant.

The window, not the schedule, sets the real cadence, so keep
`dedup_window_hours` just under the schedule's interval. Leave it out and a
6-hourly schedule distills once a day, with its other three ticks no-ops. Set
the cadence through the request, not by changing the 23-hour default
(`_PIPELINE_DEDUP_WINDOW_HOURS` in `common/events/lifecycle_handlers.py`).
The crystallize, entity-link and insights runs share that constant.

Only a successful tick counts. A tick in which every attempted cluster failed
on I/O (an LLM or storage outage) is finalised as `failure` and redelivered,
so the next tick still runs. Manual `python scripts/forge_dry_run.py`
invocations bypass the lifecycle path entirely and are not affected.

## Opt-in / opt-out

- **Default:** `skills_factory.enabled=false` per tenant. The fanout
  enumerator skips them entirely; no event published, no audit row
  written, no work done.
- **To enable:**
  `PATCH org_settings { "skills_factory": { "enabled": true } }`.
- **To pause:** flip back to `false`. The next fanout tick excludes
  the tenant immediately (one-line filter at
  `services/tenants.list_tenants_with_skills_factory_enabled`); no
  cache to invalidate.

## Failure modes + recovery

| Symptom | Likely cause | Recovery |
|---|---|---|
| Audit rows stuck in `pending` | Pub/Sub publish failed but `audit_begin` succeeded | Operator-visible; either re-publish (idempotent — same dedup window) or mark `failure` manually |
| Audit row `failure: common.llm not importable` | LLM provider chain not installed in deploy image | Install the provider chain (`pip install ...` per `core-api/pyproject.toml`); the cron path **does not** fall back to a fake LLM (intentional — see `_wire_llm_fn`) |
| Audit row `failure: forge tick wrote no candidates: all N attempted cluster(s) failed on I/O or LLM errors` | LLM provider or storage outage during the tick | Retried automatically (redelivery, then the next scheduled tick); check the provider and the `skipped_io_error` tracebacks if it persists |
| No candidates produced for a tenant | Either no labeled session traces in the freshness window, or `min_cluster_size`/`min_distinct_agents` thresholds set too high | Inspect `scanned` and the `skipped_*` counters in that tenant's `forge cron tick:` log line; lower thresholds via `org_settings.skills_factory.forge.*` |
| Same fingerprint keeps being re-proposed despite reject | Cooloff window already elapsed, or fleet/tenant scope mismatch | Inspect `forge_rejected_fingerprints` row; bump `rejection_cooloff_days` if too short |

## Related

- `scripts/forge_dry_run.py` — manual one-tick CLI (operator
  pre-flight before flipping the flag)
- `core-api/src/core_api/services/forge/cron_handler.py` — the cron
  entry point this doc describes
- `core-api/src/core_api/routes/lifecycle.py` — the fanout endpoint
- `skill-factory-implementation-plan.md §12` — knobs reference; archived from
  HEAD with `docs/live-memory-pitch/` (W2), recover with
  `git show 691659a:docs/live-memory-pitch/skill-factory-implementation-plan.md`
