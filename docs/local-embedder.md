# Local Embedder (TEI sidecar)

MemClaw can serve embeddings from a self-hosted [HuggingFace Text Embeddings
Inference](https://github.com/huggingface/text-embeddings-inference) (TEI)
container instead of OpenAI's hosted API. **Default model: `BAAI/bge-m3`**
(1024-dim, MIT-licensed, multilingual, no instruction prefix needed) — matches
the schema set in alembic migration `012_vector_dim_1024`.

## TL;DR

Two steps — set the four envs in `.env`, then bring up the stack with the profile:

```env
# .env
EMBEDDING_PROVIDER=openai
OPENAI_EMBEDDING_BASE_URL=http://tei:80/v1
OPENAI_EMBEDDING_MODEL=BAAI/bge-m3
OPENAI_EMBEDDING_SEND_DIMENSIONS=false
```

```bash
docker compose --profile embed-local up -d
```

The `tei` service starts on the compose network at `http://tei:80`. With the
four envs set, `core-api` routes embedding calls there instead of
`api.openai.com`. Without those envs, the TEI container runs but is unused —
`core-api` falls through to hosted OpenAI. **Both pieces are required.**

## Verify it's working

After `up -d`, confirm TEI is actually being hit:

```bash
# Watch TEI's request log live. Embed calls show up here in real time.
docker compose --profile embed-local logs -f tei
```

In another terminal, fire one write → search round-trip:

> These examples assume `IS_STANDALONE=true` in `.env` (see the
> [Auth modes](../README.md#self-hosted-open-source) section in README).
> Replace `standalone` with your actual API key if you're using `ADMIN_API_KEY`
> or `MEMCLAW_API_KEY` instead, and add `tenant_id` matching your setup.

```bash
curl -fsS -X POST http://localhost:8000/api/v1/memories \
  -H "X-API-Key: standalone" -H "content-type: application/json" \
  -d '{"tenant_id":"default","content":"smoke check for local embedder"}'

curl -fsS -X POST http://localhost:8000/api/v1/search \
  -H "X-API-Key: standalone" -H "content-type: application/json" \
  -d '{"tenant_id":"default","query":"smoke check","top_k":5}'
```

You should see two `POST /v1/embeddings` lines in the TEI log (one for the
write's content, one for the search's query) and a non-empty `memories`
array in the search response. If the TEI log stays silent during your
calls, double-check the four envs were picked up — `core-api` is silently
hitting OpenAI.

## Why bother

| | OpenAI hosted | TEI + bge-m3 (this) |
|---|---|---|
| Cost @ 1000 RPS | ~$10.5k/mo | ~$1.0k/mo (2× L4) |
| Idle p50 search | ~230 ms | ~127 ms (CPU), ~50 ms (L4) |
| Data residency | leaves your env | stays in your VPC |
| R@5 (LongMemEval N=30) | 0.889 | 0.963 |
| Accuracy | 86.7 % | 93.3 % |

Numbers from the local-embedder bench-off (LongMemEval `single-session-user`,
N=30, seed=42) that drove this default.

## How it works

`OpenAIEmbeddingProvider` accepts an optional `base_url`. When set, the OpenAI
SDK routes every embed call there instead of `api.openai.com`. TEI speaks the
same `POST /v1/embeddings` API shape, so no other code changes are required.

Two flags are TEI-specific:

* **`OPENAI_EMBEDDING_SEND_DIMENSIONS=false`** — TEI rejects the
  `dimensions=` SDK kwarg. Must be `false` whenever `OPENAI_EMBEDDING_BASE_URL`
  points at TEI. The OpenAI hosted path keeps the default `true` (dim is
  pinned to `VECTOR_DIM`).
* **`OPENAI_EMBEDDING_TRUNCATE_TO_DIM`** — Matryoshka-style output slice +
  L2-renormalize. Only relevant if you swap to a model whose native dim is
  larger than the schema (e.g. Qwen3-Embedding-4B = 2560, schema = 1024).
  bge-m3 is native 1024 against the 1024 schema — leave empty.

Two are model-specific:

* **`OPENAI_EMBEDDING_MODEL`** — passed to the OpenAI SDK and surfaced to TEI
  unchanged. TEI ignores it (TEI runs whatever model `--model-id` says) but
  the value is logged and cached on so we keep it accurate.
* **`EMBEDDING_QUERY_INSTRUCTION`** — only set this for instruction-aware
  models (Qwen3-Embedding, e5-instruct, KaLM). When set, the search path
  prepends `Instruct: <task>\nQuery: <text>` to every query; the ingest path
  is unchanged. bge-m3 is symmetric — leave empty.

The full asymmetric-encoding contract is in `common/embedding/protocols.py`
(`embed_query` vs `embed`).

## Hardware sizing (CPU)

The compose service ships with the **CPU image** (`cpu-1.7`) by default. Good
enough for a laptop, a small VM, or a single-tenant client. Numbers below are
for `BAAI/bge-m3` at ~200-token average input, idle (single-call) latency.

| Hardware | TEI image | Realistic RPS | p50 latency |
|---|---|---|---|
| 4 vCPU laptop class (M-series, Ryzen 7, etc.) | `cpu-1.7` | 50–150 | ~250 ms |
| 8 vCPU AMD (e2-standard-8) | `cpu-1.7` | 200–400 | ~130 ms |
| 8 vCPU Intel (n2-standard-8, AVX-512) | `cpu-1.7` | 400–700 | ~80 ms |
| 1× L4 GPU (g2-standard-8) | `89-1.7` | 3,000–8,000 | ~50 ms |

A single replica's per-call latency degrades sharply under concurrent load
(measured: 57 ms idle → 571 ms at 5×2 concurrent fan-out on CPU).

For production at sustained ≥ 200 RPS you want **two TEI replicas behind a
load balancer**. Cost reference: 2× L4 on `g2-standard-8` runs ~$1k/mo
on-demand, ~$650/mo with committed-use discount, and holds p99 < 200 ms at
1000 RPS sustained. See the GPU section below for setup.

## GPU acceleration

The default `tei` image is CPU-only. To enable a GPU you need three things:
the right TEI image tag for your GPU, host-side NVIDIA plumbing, and the
Compose `deploy:` block.

### 1. Pick the right TEI image tag

HuggingFace publishes per-architecture builds. Using the wrong tag either
fails to pull or runtime-crashes on first embed. Find your GPU's compute
capability at <https://developer.nvidia.com/cuda-gpus>, then pick:

| GPU | Compute cap | TEI tag |
|---|---|---|
| T4 (g4-class) | sm_75 | `turing-1.7` |
| A10, RTX 3090 | sm_86 | `86-1.7` |
| A100, RTX A6000 | sm_80 | `1.7` (the "default CUDA" tag) |
| **L4, RTX 4090** | sm_89 | `89-1.7` — verified in this repo's bench |
| H100 | sm_90 | `hopper-1.7` |

### 2. Host requirements

- **NVIDIA driver 535+** (CUDA 12.4 compatible). Older drivers start the
  container but fail on first embed call. Verify with `nvidia-smi` on the
  host.
- **[nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)**
  installed. Verify with:
  ```bash
  docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
  ```
  Should print the host's GPU table. If not, finish the toolkit install
  before continuing.

### 3. Set the image tag in `.env`

```env
TEI_IMAGE_TAG=89-1.7   # or your GPU's tag from the table above
```

### 4. Uncomment the `deploy:` block in `docker-compose.yml`

(Or copy this same block into a `docker-compose.override.yml` so you don't
modify the tracked file.)

```yaml
services:
  tei:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Plain `docker compose up` honours this block — no Swarm mode needed.

### 5. Restart with the GPU image

```bash
docker compose --profile embed-local up -d --force-recreate tei
```

The first start downloads the bge-m3 weights (~2 GB) and may take several
minutes. The healthcheck has 30 retries + `start_period: 60s` to cover this;
subsequent starts reuse the `tei_cache` volume and come up in seconds.

### 6. Verify the GPU is actually being used

```bash
docker compose --profile embed-local exec tei nvidia-smi
```

You should see a `python` (or `text-embeddings-inference`) process holding
~2 GB of GPU memory. If `nvidia-smi` errors with "command not found" or the
process list is empty, the container is on CPU mode — re-check steps 2–4.
The most common silent fallback is forgetting step 4 (`deploy:` block stays
commented out → Compose ignores GPU, container runs CPU-only on the GPU
image, which works but at ~CPU speed).

### Memory + multi-replica

bge-m3 is ~2 GB on GPU. An L4 (24 GB) or A10 (24 GB) has plenty of headroom,
so you can run **multiple TEI replicas on one GPU** if you're concurrency-
bound rather than throughput-bound (a single replica saturates batching at
~5–10 concurrent in-flight embed calls). Each replica needs its own
container, port mapping, and entry in `OPENAI_EMBEDDING_BASE_URL` (or a
load balancer in front of them).

### Production / Kubernetes

Production GPU deployments typically use Kubernetes with the [NVIDIA GPU
Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/index.html)
on a dedicated node pool. Compose `deploy:` doesn't apply; the equivalent
manifest:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tei
spec:
  replicas: 2
  template:
    spec:
      nodeSelector: { cloud.google.com/gke-accelerator: nvidia-l4 }
      tolerations:
        - key: nvidia.com/gpu
          operator: Exists
          effect: NoSchedule
      containers:
        - name: tei
          image: ghcr.io/huggingface/text-embeddings-inference:89-1.7
          args: [--model-id=BAAI/bge-m3, --port=80, --auto-truncate]
          ports: [{ containerPort: 80 }]
          resources:
            limits: { nvidia.com/gpu: 1 }
          readinessProbe:
            httpGet: { path: /health, port: 80 }
            initialDelaySeconds: 60   # cold model download
            periodSeconds: 10
            failureThreshold: 30
```

Front it with a `Service` of type `ClusterIP` (or internal `LoadBalancer`
inside a private VPC) and point `OPENAI_EMBEDDING_BASE_URL` at that Service's
DNS name (e.g. `http://tei.default.svc.cluster.local:80/v1`).

## Swapping models

Set `TEI_MODEL_ID` and (if needed) the four flags above. The combinations
that work cleanly with the current 1024-dim schema:

| Model | Native dim | Truncate? | Instruction? | Notes |
|---|---|---|---|---|
| `BAAI/bge-m3` | 1024 | no | no | **Default.** MIT, 100+ langs, 8K ctx. |
| `Snowflake/snowflake-arctic-embed-l-v2.0` | 1024 | no | no (recommended `query:` prefix) | Apache-2.0, 74 langs. Same shape as bge-m3. |
| `Qwen/Qwen3-Embedding-0.6B` | 1024 | no | yes | Apache-2.0, instruction-aware. Best quality for this dim, but needs GPU. |
| `Qwen/Qwen3-Embedding-4B` | 2560 | yes (`=1024`) | yes | Truncates to 1024 via Matryoshka. L4 GPU. |

If you go to a model with a different native dim (e.g. Qwen3-8B at 4096),
either set `OPENAI_EMBEDDING_TRUNCATE_TO_DIM=1024` or migrate the schema to
the new dim with a follow-up alembic revision.

## Schema requirement

The `tei` profile assumes the 1024-dim schema introduced in v2.0.0 (alembic
revision `012_vector_dim_1024`). Two cases:

**Fresh install.** No action — the schema is created at 1024-dim on first
container start. Just bring the stack up per the [TL;DR](#tldr) above.

**Upgrading from v1.x.** This is destructive — every existing 768-dim
embedding is set to `NULL` so the column can be widened to 1024-dim.
**Follow the canonical procedure in the README's
[Upgrading from v1.x](../README.md#upgrading-from-v1x) section** — it
sequences DB snapshot, opt-in env var, migration trigger, and re-embed in
the order that won't lose data.

The procedure expects you to run a stand-alone backfill CLI to re-embed
existing rows (`python -m core_storage_api.scripts.backfill_embeddings`)
after the migration completes. Both the OSS standalone CLI and the
event-driven `core-worker` task work; the README documents both. For
multi-tenant production cutovers prefer the event-driven path.

## Switching back to OpenAI hosted

Just unset `OPENAI_EMBEDDING_BASE_URL` (and the three other `OPENAI_EMBEDDING_*`
envs from the TL;DR) in your `.env` and restart `core-api`:

```bash
docker compose up -d --force-recreate core-api
```

You stay on the 1024-dim schema. OpenAI's `text-embedding-3-small` supports
`dimensions=1024` natively (Matryoshka — produced by truncating from the
model's native 1536-dim and L2-renormalizing on OpenAI's side). Setting
`OPENAI_EMBEDDING_SEND_DIMENSIONS=true` (the default when `BASE_URL` is
unset) tells the SDK to request 1024-dim output. So:

- **Schema dim is global** — all embeddings in the DB are 1024-dim regardless
  of which provider produced them.
- **Provider switch is just an env-var change** — no migration, no re-embed
  required (though you may want to re-embed for vector-space consistency
  if you've been writing under a different vendor — re-run
  `python -m core_storage_api.scripts.backfill_embeddings` after switching).

To stop the unused TEI container too, drop the `--profile embed-local` flag
on the next `up`:

```bash
docker compose down
docker compose up -d   # no profile = no TEI sidecar
```

## Troubleshooting

* **`tei` healthcheck fails on first boot for several minutes.** First-time
  model download can be slow (~2 GB for bge-m3). The healthcheck has 30
  retries and `start_period: 60s` to cover this. Subsequent boots reuse the
  `tei_cache` volume and come up in seconds.
* **`core-api` logs `400 Bad Request` from `/v1/embeddings`.** Check that
  `OPENAI_EMBEDDING_SEND_DIMENSIONS=false` — TEI rejects the `dimensions=`
  kwarg.
* **Per-call latency jumps from ~130 ms to ~600 ms under concurrent load.**
  Single-replica TEI queues requests in dynamic batching. Add a second
  replica behind nginx/LB.
* **`413 Payload Too Large` on long writes.** The compose `command:` includes
  `--auto-truncate` so this should not happen with bge-m3 (8K context).
  If it does, you've swapped to a 512-ctx model — pick something else or
  pre-chunk in the application layer.
* **`up -d` succeeds but TEI is silent on every embed call.** `core-api`
  is hitting OpenAI, not TEI. The four `OPENAI_EMBEDDING_*` envs aren't
  set — re-check your `.env` (or the host shell — `docker compose config`
  shows the resolved values).
* **GPU image runs at CPU speed.** The `deploy:` block in `docker-compose.yml`
  is still commented out. Uncomment it (or use `docker-compose.override.yml`)
  per [GPU step 4](#4-uncomment-the-deploy-block-in-docker-composeyml).
* **`exec tei nvidia-smi` errors with "command not found".** The CPU image
  is running. Check `TEI_IMAGE_TAG` in `.env` matches a GPU tag from the
  table.
* **TEI container starts but errors on first embed call: `CUDA error`.**
  Host driver is too old. Verify with `nvidia-smi` on the host; upgrade to
  535+.

## Compatibility

* **Any OpenAI-compatible embedding endpoint works at the `OPENAI_EMBEDDING_BASE_URL`
  slot** — vLLM's OpenAI-compat server, llama.cpp's `server` binary,
  OpenRouter (for hosted models), Together AI, etc. The flags documented
  above (`SEND_DIMENSIONS`, `MODEL`, `QUERY_INSTRUCTION`, `TRUNCATE_TO_DIM`)
  cover most differences between back-ends. TEI is the recommended default
  because it's the most mature open-source option for the bge-m3 default.
* **The OpenClaw plugin works transparently** regardless of which embedder
  is configured — it talks to MemClaw's REST API, which doesn't expose
  the embedder choice. No plugin-side configuration needed.
