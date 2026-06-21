# P1 — Staging VPC Serverless Connector saturating under burst (memclaw)

**Filed:** 2026-06-17 by Ran (via wet-test forensics).
**Severity:** P1 — silently disables Path C entity-aware contradiction detection on dev; same class will hit prod under load.
**Status:** Unfixed; needs infra change.
**Project:** `alpine-theory-469016-c8` (GCP).
**First observed:** 2026-06-16 20:12 UTC during R1 wet-test of CAURA-131/133/134 on dev v2.16.0.

---

## TL;DR

The `memclaw-staging-vpc` Serverless VPC Connector is an **e2-micro × min=2/max=10** with `vpc-access-egress=all-traffic` on every staging Cloud Run service. Bursts of ~9 parallel outbound TCP handshakes from `staging-memclaw-core-api` (issued by Path C `_fetch_entity_context` fan-out) saturate the connector. httpx's 5s `connect` timeout fires on the un-completed handshakes → cascading `ConnectTimeout` → `asyncio.gather` blows the 30s `_CONTEXT_FETCH_TIMEOUT_SECONDS` budget → Path C falls back to the base LLM judge → CAURA-131's entity-aware judge and CAURA-133's entity-aware prompt are **silently bypassed**. CAURA-134's 30s ceiling cannot be raised high enough to mask a connector that drops handshakes outright.

This is the **third** wet-test verification of the contradictions epic that has been blocked by a non-detector issue (after the prod↔staging Pub/Sub leak and the rotated OpenAI API key).

---

## Observed issue

5-trial run of `scripts/repro_contradictions_collision.py` on dev:

| Trial | Verdict | Evidence |
|---|---|---|
| 1–5 | All `🤔 Inconclusive` at 90s wait; on later poll, **2/5 trials had both sides extracted, neither flagged**. CAURA-132 forensic log shows: |

```
20:12:45  PATH_C_DETECTION context_fetch_failed memory=8b4df9a6-609f-424f-a34a-42c1f5fe50e4
          exc_type=TimeoutError exc= candidates=8. Falling through to base LLM judge.
20:12:46  PATH_C_DETECTION judge_selection memory=8b4df9a6 candidates=8 entity_aware=0 base=8
20:12:54  PATH_A_SEMANTIC verdict memory=252fbbf3 candidate=8c021f8c verdict=False confidence=0.90
20:13:04  PATH_A_SEMANTIC verdict memory=8b4df9a6 candidate=936b6324 verdict=False confidence=0.90
```

Trial 3 was a true positive (same `elif` entity, "Tel Aviv" vs "Haifa") and was missed. Path A and Path C both said no contradiction. The `exc=` is empty — the signature of `asyncio.TimeoutError`. The entire 30s context-fetch budget was consumed.

---

## RCA investigation

### Step 1 — eliminate storage-server slowness

`staging-memclaw-core-storage-reader` during the failing minute (2026-06-16 20:12:00–20:13:00 UTC):

```
16 requests, all 200, latency 15–141 ms
```

No 5xx, no slow queries, no cold start (revision `00437-8j7` was already serving). **Storage is fine.**

### Step 2 — locate the cascade

`core-api` log in the same minute shows **9 simultaneous `POST /memories/entity-links` calls all hit `ConnectTimeout` at attempt 1/5**:

```
20:12:33.029  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.20s
20:12:33.730  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.22s
20:12:34.230  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.18s
20:12:36.430  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.19s
20:12:37.130  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.19s
20:12:37.130  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.21s
20:12:37.131  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.22s
20:12:38.130  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.20s
20:12:38.330  storage_client.POST /memories/entity-links: ConnectTimeout on attempt 1/5, retrying in 0.20s
```

These 9 requests **do not appear in storage-reader's access log** — they never reached HTTP. They failed at the TCP handshake. In the same window, calls to `audit-logs/bulk`, `entities/bulk-resolve`, `entities/relations`, `memories/similar-candidates`, and `GET /memories/{uuid}` all hit `ConnectTimeout` too. The failure is **caller-side, connection-layer, not endpoint-specific**.

### Step 3 — identify the shared bottleneck

Every staging-memclaw Cloud Run service routes through one connector:

```
$ gcloud run services describe staging-memclaw-core-api ...
vpc-access-connector  = memclaw-staging-vpc
vpc-access-egress     = all-traffic

$ gcloud compute networks vpc-access connectors describe memclaw-staging-vpc
machineType   : e2-micro          # 0.25 vCPU shared, 1 GB RAM
minInstances  : 2
maxInstances  : 10
minThroughput : 200  Mbps
maxThroughput : 1000 Mbps
state         : READY
```

Services on this connector:
```
staging-memclaw, staging-memclaw-core-api, -core-storage-reader, -core-storage-writer,
-core-worker, -platform-admin, -platform-audit, -platform-auth, -platform-devops-bot,
-platform-storage
```

Caller scale:
```
staging-memclaw-core-api  minScale=10  maxScale=200  containerConcurrency=2
```

Storage ingress:
```
staging-memclaw-core-storage-reader  ingress = internal-and-cloud-load-balancing
```

With `vpc-access-egress=all-traffic` on the caller and `internal-and-cloud-load-balancing` ingress on the callee, **every** `core-api → storage` request traverses the e2-micro connector — including the burst of 9 parallel TCP handshakes from `_fetch_entity_context`. The connector throttles concurrent handshakes; httpx's `connect=5s` budget expires before the handshake completes; the gather raises `TimeoutError` at 30s.

### Step 4 — why the prior fix (CAURA-134) doesn't cover this

CAURA-134 raised the outer `_CONTEXT_FETCH_TIMEOUT_SECONDS` from 5s → 30s under the assumption that the failure was occasional slow storage queries. The grounded data shows handshakes are *not landing at all*: a single failing TCP handshake plus 3-attempt retry already burns 15s+ at the storage_client layer; with 9 in parallel and the connector saturated on each retry, no outer timeout under 60–90s can succeed reliably. The connector is a hard ceiling on parallel handshake rate — raising the wait time only changes which retry wave fails.

---

## Impact

| Surface | Effect |
|---|---|
| Path C entity-aware detection on dev | Disabled de-facto. Every wet-test trial since CAURA-131 has been judged by the base LLM judge instead. Indistinguishable from a prompt regression. |
| Verification of CAURA-131 / 133 / 134 | Blocked. The judge they wire to is bypassed. |
| Other staging features (enrichment, audit-logs bulk, embeddings, dedup) | Intermittent latency spikes / partial retries during bursts; not fatal because most callers have higher-level retries. |
| **Prod risk** | Prod's connector config has not been audited as part of this report. If the prod connector is also e2-micro, the same class will hit prod as tenant fan-out grows. **Recommended to audit prod alongside the dev fix.** |

---

## Proposed mitigation

**Pick one of A or B; either alone is sufficient.**

### Option A — Upgrade the connector machine type (recommended)

```
gcloud compute networks vpc-access connectors update memclaw-staging-vpc \
  --region=us-central1 \
  --project=alpine-theory-469016-c8 \
  --machine-type=e2-standard-4 \
  --min-instances=2 --max-instances=10
```

- **Cost delta**: e2-standard-4 vs e2-micro at min=2 ≈ ~$120/mo additional on staging. Free if scaling down min instances.
- **Risk**: zero — same connector, same network, more capacity.
- **Rollback**: single `gcloud connectors update` reverting machineType.

### Option B — Stop routing Cloud Run → Cloud Run through the connector

Change caller egress so traffic to public `*.run.app` URLs goes direct, not via the connector:

```
gcloud run services update staging-memclaw-core-api \
  --region=us-central1 \
  --project=alpine-theory-469016-c8 \
  --vpc-egress=private-ranges-only
```

Apply to `staging-memclaw-core-worker` and any other Cloud Run service that talks to storage. Storage-reader's ingress is `internal-and-cloud-load-balancing`, so the request still needs to be reachable via the internal mesh — verify the path before flipping the flag.

- **Cost delta**: $0 (less connector traffic = potential cost reduction).
- **Risk**: low–medium. Egress mode change can break callers that *do* need VPC routing (e.g., private Cloud SQL). Audit per-service.
- **Rollback**: single `--vpc-egress=all-traffic` revert.

### Verification of mitigation

After the change, re-run the wet-test:

```bash
export MEMCLAW_API_URL=https://memclaw.dev MEMCLAW_API_KEY=<key> MEMCLAW_TENANT_ID=<tenant>
python scripts/repro_contradictions_collision.py --wait 90 --person "Priya,Dana,Noa,Elif" \
  # × 5 trials
```

Pass criteria:
1. **Zero** `storage_client.*: ConnectTimeout on attempt N/5` warnings during the trials (`gcloud logging read 'jsonPayload.message=~"ConnectTimeout on attempt"' --freshness=10m`).
2. CAURA-132 log: `PATH_C_DETECTION context_fetched memory=… new_ctx_size=N cand_ctx_sizes={…}` appears for each trial (replaces `context_fetch_failed`).
3. `judge_selection` shows `entity_aware>0`, not `entity_aware=0 base=N`.
4. For trials with same canonical subject + different fact (the priya-class positive case): verdict surfaces.

---

## Reproducibility / how to query the evidence

```bash
# 9-way ConnectTimeout cascade on a single Path C wave
gcloud logging read 'resource.labels.service_name="staging-memclaw-core-api"
  AND jsonPayload.message=~"ConnectTimeout on attempt"
  AND timestamp>="2026-06-16T20:12:00Z"
  AND timestamp<="2026-06-16T20:13:00Z"' \
  --limit 50 --freshness=2d \
  --project=alpine-theory-469016-c8

# Confirm storage-reader was healthy and fast in the same window
gcloud logging read 'resource.labels.service_name="staging-memclaw-core-storage-reader"
  AND httpRequest.requestUrl!=""
  AND timestamp>="2026-06-16T20:12:00Z"
  AND timestamp<="2026-06-16T20:13:00Z"' \
  --limit 100 --freshness=2d \
  --project=alpine-theory-469016-c8

# Path C outer timeout firing
gcloud logging read 'jsonPayload.message=~"PATH_C_DETECTION context_fetch_failed"
  AND timestamp>="2026-06-16T20:10:00Z"' \
  --limit 20 --freshness=2d \
  --project=alpine-theory-469016-c8
```

---

## Related references

- Code: [`core-api/src/core_api/services/contradiction_detector.py`](../core-api/src/core_api/services/contradiction_detector.py) — `_fetch_entity_context`, `_CONTEXT_FETCH_TIMEOUT_SECONDS=30.0`, Path C call site.
- Code: [`core-api/src/core_api/clients/storage_client.py`](../core-api/src/core_api/clients/storage_client.py) — httpx pool `connect=5.0`, `_with_retry`.
- PR history: CAURA-128 → CAURA-134 (Path C epic). CAURA-134 raised the outer timeout 5s→30s; this report shows 30s is also insufficient while the connector remains the bottleneck.
- Prior infra incident: `docs/BUG-pubsub-cross-env-leakage.md` (Pub/Sub topology cross-env leak, P0, mitigated 2026-06-07).

---

## Reporter

- **Discovery**: 2026-06-16 20:12 UTC during R1 wet-test on dev v2.16.0 (post OpenAI key rotation + `ENTITY_EXTRACTION_MODEL=gpt-4o-mini`).
- **Trials**: 5 × `scripts/repro_contradictions_collision.py --wait 90`.
- **Key memory ids** for cross-reference: A=`252fbbf3-4ad6-4de7-aa36-c9f116b134cf`, B=`8b4df9a6-609f-424f-a34a-42c1f5fe50e4` (same Elif entity, opposite cities, missed).
