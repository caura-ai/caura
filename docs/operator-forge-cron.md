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
   publishes one `memclaw.lifecycle.forge-distill-requested` event per
   tenant.
3. **The in-process consumer** in `core-api` (or `core-worker` in
   SaaS deployments) invokes `run_forge_cron_tick` for the tenant.
4. **One lifecycle_audit row per tenant per tick** captures the work
   done — candidates produced, promoted, and the 5 skip-bucket counts.

## Operator dial: `forge.cron_interval_hours`

The setting `org_settings.skills_factory.forge.cron_interval_hours`
(default `6`) is **informational** — the actual cadence is set by
whatever external system drives the fanout endpoint. Set the external
schedule to match this value; the field is published in the audit row
and the inbox-card metadata so an operator can see "this candidate
was minted by the 06:00 UTC tick."

## Required external schedule entry

### Google Cloud Scheduler

```yaml
name: forge-cron-fanout
schedule: "0 */6 * * *"   # every 6 hours
time_zone: "UTC"
http_target:
  http_method: POST
  uri: https://<core-api-host>/admin/lifecycle/fanout/forge-distill
  oidc_token:
    service_account_email: <core-operations-sa>@<project>.iam.gserviceaccount.com
  headers:
    X-Memclaw-Admin-Token: ${MEMCLAW_ADMIN_TOKEN}   # see core-api/auth.enforce_admin
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
                    -H "X-Memclaw-Admin-Token: $MEMCLAW_ADMIN_TOKEN" \
                    "$CORE_API_BASE_URL/admin/lifecycle/fanout/forge-distill"
              envFrom:
                - secretRef: { name: memclaw-admin }
          restartPolicy: OnFailure
```

## What the operator sees

After the schedule lands, every tick produces:

- **Per-tenant audit rows** under `lifecycle_audit`
  (`action='forge-distill'`, `status='success' | 'failure'`,
  `stats={candidates_written, promoted, scanned, held,
  skipped_poisoned, skipped_sentinel, ...}`).
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

## Dedup safety

The shared lifecycle handler uses
`_PIPELINE_DEDUP_WINDOW_HOURS` (currently 1 hour) — re-curling the
fanout endpoint within the window is a no-op for any tenant whose
prior tick succeeded. Manual `memclawctl forge dry-run` invocations
bypass the lifecycle path entirely and are not affected.

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
| No candidates produced for a tenant | Either no labeled session traces in the freshness window, or `min_cluster_size`/`min_distinct_agents` thresholds set too high | Inspect `stats.scanned` + the 5 skip counters on the audit row; lower thresholds via `org_settings.skills_factory.forge.*` |
| Same fingerprint keeps being re-proposed despite reject | Cooloff window already elapsed, or fleet/tenant scope mismatch | Inspect `forge_rejected_fingerprints` row; bump `rejection_cooloff_days` if too short |

## Related

- `scripts/forge_dry_run.py` — manual one-tick CLI (operator
  pre-flight before flipping the flag)
- `core-api/src/core_api/services/forge/cron_handler.py` — the cron
  entry point this doc describes
- `core-api/src/core_api/routes/lifecycle.py` — the fanout endpoint
- `docs/live-memory-pitch/skill-factory-implementation-plan.md §12` —
  knobs reference
