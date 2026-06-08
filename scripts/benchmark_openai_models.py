"""Benchmark OpenAI LLMs for MemClaw enrichment + entity extraction tasks.

Tests speed, cost, and output quality across all available OpenAI models.
Requires OPENAI_API_KEY env var (or .env file).

Usage:
    python scripts/benchmark_openai_models.py
    python scripts/benchmark_openai_models.py --rounds 3 --models gpt-4o-mini gpt-4.1-nano
"""

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai

# ── Embedding models to benchmark ─────────────────────────────────────────────
# (model_id, price_per_1M_tokens, max_dimensions)
EMBEDDING_MODELS: list[tuple[str, float, int]] = [
    ("text-embedding-3-small", 0.02, 1536),
    ("text-embedding-3-large", 0.13, 3072),
]

VECTOR_DIM = 768  # MemClaw's configured dimension

# ── LLM models to benchmark ─────────────────────────────────────────────────
# (model_id, input_price_per_1M, output_price_per_1M)
MODELS: list[tuple[str, float, float]] = [
    # GPT-5.4 series
    ("gpt-5.4",        2.50,  15.00),
    ("gpt-5.4-mini",   0.75,   4.50),
    ("gpt-5.4-nano",   0.20,   1.25),
    # GPT-5
    ("gpt-5",          1.25,  10.00),
    # GPT-4.1 series
    ("gpt-4.1",        2.00,   8.00),
    ("gpt-4.1-mini",   0.40,   1.60),
    ("gpt-4.1-nano",   0.10,   0.40),
    # GPT-4o series
    ("gpt-4o",         2.50,  10.00),
    ("gpt-4o-mini",    0.15,   0.60),
    # Reasoning models
    ("o3-mini",        1.10,   4.40),
    ("o4-mini",        1.10,   4.40),
]

# ── Test prompts (same ones MemClaw actually sends) ──────────────────────────
ENRICHMENT_PROMPT = """\
You are a memory classifier for a business agent memory system.

Analyze the following memory content and return a JSON object with these fields:

1. "memory_type": one of "fact", "episode", "decision", "preference", "task", "semantic", "intention", "plan", "commitment", "action", "outcome", "cancellation"
2. "weight": float 0.0-1.0 indicating importance
3. "title": short label (max 80 chars) summarizing the memory
4. "summary": 1-2 sentence condensed version
5. "tags": array of 2-6 lowercase keyword tags
6. "status": one of "active", "pending", "confirmed"
7. "ts_valid_start": ISO 8601 datetime string or null
8. "ts_valid_end": ISO 8601 datetime string or null
9. "contains_pii": boolean
10. "pii_types": array of strings

Return ONLY valid JSON (no markdown fences):
{{"memory_type": "...", "weight": 0.0, "title": "...", "summary": "...", "tags": ["..."], "status": "active", "ts_valid_start": null, "ts_valid_end": null, "contains_pii": false, "pii_types": []}}

Content:
{content}
"""

ENTITY_EXTRACTION_PROMPT = """\
Extract named entities from the following text. Return a JSON object with an "entities" key containing an array of objects with:
- "name": canonical entity name
- "type": one of "person", "organization", "product", "technology", "location", "event", "concept"
- "confidence": float 0.0-1.0

Return ONLY valid JSON. Example: {{"entities": [{{"name": "Acme Corp", "type": "organization", "confidence": 0.95}}]}}

Text:
{content}
"""

# ── Test content samples (varying complexity) ─────────────────────────────────
TEST_SAMPLES = [
    {
        "label": "simple_fact",
        "content": "The deployment pipeline uses GitHub Actions with a 15-minute timeout.",
    },
    {
        "label": "complex_decision",
        "content": (
            "After evaluating PostgreSQL, MongoDB, and DynamoDB for the new analytics service, "
            "the team decided to go with managed Postgres. Key factors: native pgvector support "
            "for embeddings, ScaNN indexing for fast ANN search, and compatibility with existing "
            "Alembic migrations. John Smith from the platform team approved the $2,400/month budget. "
            "Migration deadline is March 30, 2026. Contact: john.smith@acme.com"
        ),
    },
    {
        "label": "task_with_deadline",
        "content": (
            "Need to upgrade the authentication middleware to support OAuth 2.1 by end of Q2 2026. "
            "Legal flagged the current session token storage as non-compliant with new EU regulations. "
            "Assigned to the security team, priority P1."
        ),
    },
    {
        "label": "episode_incident",
        "content": (
            "Production outage on 2026-03-15 at 14:32 UTC. Root cause: connection pool exhaustion "
            "in the search service due to a missing timeout on Postgres queries. 47 minutes MTTR. "
            "Affected 12% of API requests. Hotfix deployed by Sarah Chen."
        ),
    },
    {
        "label": "multi_entity",
        "content": (
            "Microsoft announced a partnership with OpenAI to integrate GPT-5 into Azure Kubernetes "
            "Service. Google Cloud responded by launching Gemini-native orchestration in GKE. "
            "Amazon Web Services is expected to counter with a Bedrock update at re:Invent 2026 in Las Vegas."
        ),
    },
]

VALID_MEMORY_TYPES = {
    "fact", "episode", "decision", "preference", "task", "semantic",
    "intention", "plan", "commitment", "action", "outcome", "cancellation",
}
VALID_STATUSES = {"active", "pending", "confirmed"}


@dataclass
class RunResult:
    model: str
    task: str  # "enrichment" or "extraction"
    sample: str
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    valid_json: bool = False
    valid_schema: bool = False
    error: str = ""


@dataclass
class ModelSummary:
    model: str
    input_price: float
    output_price: float
    runs: int = 0
    errors: int = 0
    avg_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_cost_per_call: float = 0.0
    schema_accuracy: float = 0.0
    latencies: list[float] = field(default_factory=list)


def calc_cost(input_tokens: int, output_tokens: int, input_price: float, output_price: float) -> float:
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def validate_enrichment(raw: dict) -> bool:
    """Check if enrichment output matches expected schema."""
    if raw.get("memory_type") not in VALID_MEMORY_TYPES:
        return False
    w = raw.get("weight")
    if not isinstance(w, (int, float)) or not (0.0 <= w <= 1.0):
        return False
    if not isinstance(raw.get("title"), str) or not raw["title"]:
        return False
    if not isinstance(raw.get("tags"), list):
        return False
    if raw.get("status") not in VALID_STATUSES:
        return False
    return True


def validate_extraction(raw) -> bool:
    """Check if entity extraction output is valid."""
    # Unwrap if model returned an object with a list value
    if isinstance(raw, dict):
        for key in ("entities", "results", "data", "items"):
            if key in raw and isinstance(raw[key], list):
                raw = raw[key]
                break
        else:
            # Take the first list value found
            for v in raw.values():
                if isinstance(v, list):
                    raw = v
                    break
    if not isinstance(raw, list) or len(raw) == 0:
        return False
    for entity in raw:
        if not isinstance(entity, dict):
            return False
        if not entity.get("name") or not entity.get("type"):
            return False
    return True


async def run_enrichment(client: openai.AsyncOpenAI, model_id: str, content: str, is_reasoning: bool) -> RunResult:
    """Run a single enrichment call."""
    result = RunResult(model=model_id, task="enrichment", sample="")
    prompt = ENRICHMENT_PROMPT.format(content=content, today=date.today().isoformat())

    try:
        kwargs = dict(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
        )
        # Reasoning models (o-series) don't support temperature or json_object response_format
        if not is_reasoning:
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["temperature"] = 0.0

        t0 = time.perf_counter()
        response = await client.chat.completions.create(**kwargs)
        result.latency_ms = int((time.perf_counter() - t0) * 1000)

        usage = response.usage
        result.input_tokens = usage.prompt_tokens if usage else 0
        result.output_tokens = usage.completion_tokens if usage else 0

        text = response.choices[0].message.content or ""
        # Strip markdown fences if present
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        raw = json.loads(text)
        result.valid_json = True
        result.valid_schema = validate_enrichment(raw)
    except json.JSONDecodeError:
        result.valid_json = False
        result.error = "invalid_json"
    except openai.NotFoundError:
        result.error = "model_not_found"
    except openai.BadRequestError as e:
        result.error = f"bad_request: {e.message[:80]}"
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:80]}"

    return result


async def run_extraction(client: openai.AsyncOpenAI, model_id: str, content: str, is_reasoning: bool) -> RunResult:
    """Run a single entity extraction call."""
    result = RunResult(model=model_id, task="extraction", sample="")
    prompt = ENTITY_EXTRACTION_PROMPT.format(content=content)

    try:
        kwargs = dict(
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
        )
        if not is_reasoning:
            kwargs["response_format"] = {"type": "json_object"}
            kwargs["temperature"] = 0.0

        t0 = time.perf_counter()
        response = await client.chat.completions.create(**kwargs)
        result.latency_ms = int((time.perf_counter() - t0) * 1000)

        usage = response.usage
        result.input_tokens = usage.prompt_tokens if usage else 0
        result.output_tokens = usage.completion_tokens if usage else 0

        text = response.choices[0].message.content or ""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        raw = json.loads(text)
        result.valid_json = True
        result.valid_schema = validate_extraction(raw)
    except json.JSONDecodeError:
        result.valid_json = False
        result.error = "invalid_json"
    except openai.NotFoundError:
        result.error = "model_not_found"
    except openai.BadRequestError as e:
        result.error = f"bad_request: {e.message[:80]}"
    except Exception as e:
        result.error = f"{type(e).__name__}: {str(e)[:80]}"

    return result


def is_reasoning_model(model_id: str) -> bool:
    return model_id.startswith("o1") or model_id.startswith("o3") or model_id.startswith("o4")


async def benchmark(models: list[tuple[str, float, float]], rounds: int, samples: list[dict]) -> list[ModelSummary]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        # Try .env
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.strip().startswith("OPENAI_API_KEY="):
                    api_key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set. Export it or add to .env")
        sys.exit(1)

    client = openai.AsyncOpenAI(api_key=api_key)
    summaries: dict[str, ModelSummary] = {}
    all_results: list[RunResult] = []

    total_calls = len(models) * len(samples) * 2 * rounds  # 2 tasks per sample
    completed = 0

    for model_id, in_price, out_price in models:
        summary = ModelSummary(model=model_id, input_price=in_price, output_price=out_price)
        summaries[model_id] = summary
        reasoning = is_reasoning_model(model_id)

        print(f"\n{'='*60}")
        print(f"  Model: {model_id}  (${in_price} in / ${out_price} out per 1M)")
        print(f"{'='*60}")

        for rnd in range(rounds):
            for sample in samples:
                label = sample["label"]
                content = sample["content"]

                # Run enrichment and extraction sequentially per model to avoid rate limits
                for task_fn, task_name in [(run_enrichment, "enrichment"), (run_extraction, "extraction")]:
                    result = await task_fn(client, model_id, content, reasoning)
                    result.sample = label
                    result.cost_usd = calc_cost(result.input_tokens, result.output_tokens, in_price, out_price)

                    summary.runs += 1
                    if result.error:
                        summary.errors += 1
                        status = f"ERR: {result.error}"
                    else:
                        summary.latencies.append(result.latency_ms)
                        summary.total_input_tokens += result.input_tokens
                        summary.total_output_tokens += result.output_tokens
                        summary.total_cost_usd += result.cost_usd
                        if result.valid_schema:
                            status = f"OK  {result.latency_ms:>5}ms  ${result.cost_usd:.6f}"
                        else:
                            status = f"BAD SCHEMA  {result.latency_ms:>5}ms"

                    completed += 1
                    print(f"  [{completed:>3}/{total_calls}] r{rnd+1} {task_name:<11} {label:<20} {status}")

                    all_results.append(result)

                    # If model not found, skip remaining samples
                    if result.error == "model_not_found":
                        print(f"  >>> Model {model_id} not found, skipping remaining tests")
                        # Mark remaining as errors
                        remaining = (len(samples) * 2 * rounds) - summary.runs
                        summary.errors += remaining
                        summary.runs += remaining
                        completed += remaining
                        break

                if any(r.error == "model_not_found" for r in all_results if r.model == model_id):
                    break
            if any(r.error == "model_not_found" for r in all_results if r.model == model_id):
                break

        # Compute summary stats
        if summary.latencies:
            sorted_lat = sorted(summary.latencies)
            summary.avg_latency_ms = sum(sorted_lat) / len(sorted_lat)
            summary.min_latency_ms = sorted_lat[0]
            summary.max_latency_ms = sorted_lat[-1]
            summary.p50_latency_ms = sorted_lat[len(sorted_lat) // 2]

        successful = [r for r in all_results if r.model == model_id and not r.error]
        if successful:
            summary.schema_accuracy = sum(1 for r in successful if r.valid_schema) / len(successful)
            summary.avg_cost_per_call = summary.total_cost_usd / len(successful)

    return list(summaries.values())


def print_report(summaries: list[ModelSummary]):
    print("\n")
    print("=" * 120)
    print("  BENCHMARK RESULTS")
    print("=" * 120)

    # Sort by avg cost per call
    valid = [s for s in summaries if s.latencies]
    skipped = [s for s in summaries if not s.latencies]

    valid.sort(key=lambda s: s.avg_cost_per_call)

    # Header
    print(f"\n{'Model':<18} {'Avg ms':>8} {'P50 ms':>8} {'Min ms':>8} {'Max ms':>8} "
          f"{'Avg $/call':>12} {'Total $':>10} {'Schema %':>10} {'Errors':>8} {'Runs':>6}")
    print("-" * 120)

    for s in valid:
        print(
            f"{s.model:<18} {s.avg_latency_ms:>8.0f} {s.p50_latency_ms:>8.0f} "
            f"{s.min_latency_ms:>8.0f} {s.max_latency_ms:>8.0f} "
            f"${s.avg_cost_per_call:>10.6f} ${s.total_cost_usd:>9.6f} "
            f"{s.schema_accuracy * 100:>9.1f}% {s.errors:>8} {s.runs:>6}"
        )

    if skipped:
        print()
        for s in skipped:
            print(f"{s.model:<18} {'SKIPPED (model not found or all errors)':>60}")

    # Cost projection
    print(f"\n{'─' * 120}")
    print("  COST PROJECTION (per 10,000 enrichment calls)")
    print(f"{'─' * 120}")
    print(f"{'Model':<18} {'$/10K calls':>14} {'$/100K calls':>14} {'$/1M calls':>14} {'Avg tokens/call':>16}")
    print("-" * 80)

    for s in valid:
        if s.avg_cost_per_call > 0:
            avg_tokens = (s.total_input_tokens + s.total_output_tokens) / max(len(s.latencies), 1)
            print(
                f"{s.model:<18} ${s.avg_cost_per_call * 10_000:>13.2f} "
                f"${s.avg_cost_per_call * 100_000:>13.2f} "
                f"${s.avg_cost_per_call * 1_000_000:>13.2f} "
                f"{avg_tokens:>15.0f}"
            )

    # Recommendation
    print(f"\n{'─' * 120}")
    print("  RECOMMENDATION")
    print(f"{'─' * 120}")

    if valid:
        # Best balance: lowest cost with >= 90% schema accuracy
        good = [s for s in valid if s.schema_accuracy >= 0.9]
        if good:
            cheapest = min(good, key=lambda s: s.avg_cost_per_call)
            fastest = min(good, key=lambda s: s.avg_latency_ms)
            print(f"  Cheapest (>=90% accuracy): {cheapest.model} at ${cheapest.avg_cost_per_call:.6f}/call, {cheapest.avg_latency_ms:.0f}ms avg")
            print(f"  Fastest  (>=90% accuracy): {fastest.model} at {fastest.avg_latency_ms:.0f}ms avg, ${fastest.avg_cost_per_call:.6f}/call")

            # Best value = lowest (cost * latency) product
            best = min(good, key=lambda s: s.avg_cost_per_call * s.avg_latency_ms)
            print(f"  Best value (cost*speed):   {best.model} at ${best.avg_cost_per_call:.6f}/call, {best.avg_latency_ms:.0f}ms avg")
        else:
            print("  No model achieved >= 90% schema accuracy. Review outputs manually.")

    # Current fallback comparison
    print(f"\n  Current fallback model: gpt-4o-mini")
    current = next((s for s in valid if s.model == "gpt-4o-mini"), None)
    if current:
        print(f"    -> ${current.avg_cost_per_call:.6f}/call, {current.avg_latency_ms:.0f}ms avg, {current.schema_accuracy*100:.0f}% accuracy")


# ── Embedding benchmark ───────────────────────────────────────────────────────

EMBEDDING_SAMPLES = [
    {"label": "short", "text": "The deployment uses GitHub Actions."},
    {"label": "medium", "text": (
        "After evaluating PostgreSQL, MongoDB, and DynamoDB for the new analytics service, "
        "the team decided to go with managed Postgres. Key factors: native pgvector support "
        "for embeddings, ScaNN indexing for fast ANN search, and compatibility with existing "
        "Alembic migrations."
    )},
    {"label": "long", "text": (
        "Production outage on 2026-03-15 at 14:32 UTC. Root cause: connection pool exhaustion "
        "in the search service due to a missing timeout on Postgres queries. 47 minutes MTTR. "
        "Affected 12% of API requests. Hotfix deployed by Sarah Chen. Post-mortem scheduled "
        "for Monday. Action items: add circuit breaker to connection pool, set query timeout "
        "to 5s, add monitoring alert for connection count > 80% capacity, update runbook with "
        "connection pool troubleshooting steps. The incident was escalated to the SRE team at "
        "14:45 UTC and the VP of Engineering was notified at 15:00 UTC."
    )},
    # Batch of 10 texts for batch throughput test
    {"label": "batch_10", "texts": [
        "The API uses FastAPI with async handlers.",
        "the database is deployed in me-west1 region.",
        "Memory dedup uses content_hash with SHA-256.",
        "Entity extraction runs via Gemini 2.5 Flash Lite.",
        "The search endpoint blends vector similarity with keyword FTS.",
        "Tenant isolation is enforced via API key scoping.",
        "Agent trust levels range from 0 (restricted) to 3 (admin).",
        "The crystallizer merges near-duplicate memories into atomic facts.",
        "Alembic manages database migrations for the schema.",
        "The OpenClaw plugin exposes memclaw_write, memclaw_recall, memclaw_entity_get.",
    ]},
]

# Semantic similarity test pairs: (query, positive_match, negative_match)
SIMILARITY_PAIRS = [
    (
        "database connection pool exhaustion",
        "Postgres query timeout caused connection pool to fill up",
        "The marketing team reviewed Q3 campaign results",
    ),
    (
        "authentication middleware upgrade",
        "OAuth 2.1 support needs to be added to the auth layer",
        "The deployment pipeline uses GitHub Actions with a 15-minute timeout",
    ),
    (
        "memory search and retrieval",
        "Vector similarity search with pgvector and entity boosting",
        "Sarah Chen deployed the hotfix for the production outage",
    ),
]


@dataclass
class EmbeddingResult:
    model: str
    sample: str
    latency_ms: int = 0
    tokens: int = 0
    cost_usd: float = 0.0
    dimensions: int = 0
    error: str = ""


@dataclass
class EmbeddingSummary:
    model: str
    price_per_1m: float
    runs: int = 0
    errors: int = 0
    avg_latency_ms: float = 0.0
    min_latency_ms: float = float("inf")
    max_latency_ms: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    avg_cost_per_call: float = 0.0
    batch_latency_ms: float = 0.0
    batch_tokens: int = 0
    similarity_correct: int = 0
    similarity_total: int = 0
    latencies: list[float] = field(default_factory=list)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def benchmark_embeddings(rounds: int) -> list[EmbeddingSummary]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(env_path):
            for line in open(env_path):
                if line.strip().startswith("OPENAI_API_KEY="):
                    api_key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not api_key:
        print("ERROR: OPENAI_API_KEY not set")
        return []

    client = openai.AsyncOpenAI(api_key=api_key)
    summaries: list[EmbeddingSummary] = []

    for model_id, price, max_dim in EMBEDDING_MODELS:
        summary = EmbeddingSummary(model=model_id, price_per_1m=price)

        print(f"\n{'='*60}")
        print(f"  Embedding: {model_id}  (${price}/1M tokens, dim={VECTOR_DIM})")
        print(f"{'='*60}")

        for rnd in range(rounds):
            # Single text embeddings
            for sample in EMBEDDING_SAMPLES:
                if "texts" in sample:
                    continue  # batch handled separately
                label = sample["label"]
                try:
                    t0 = time.perf_counter()
                    resp = await client.embeddings.create(
                        model=model_id,
                        input=sample["text"],
                        dimensions=VECTOR_DIM,
                    )
                    ms = int((time.perf_counter() - t0) * 1000)
                    tokens = resp.usage.total_tokens if resp.usage else 0
                    dims = len(resp.data[0].embedding)
                    cost = tokens * price / 1_000_000

                    summary.runs += 1
                    summary.latencies.append(ms)
                    summary.total_tokens += tokens
                    summary.total_cost_usd += cost
                    print(f"  r{rnd+1} single  {label:<10} OK  {ms:>5}ms  {tokens:>4} tok  dim={dims}  ${cost:.8f}")
                except Exception as e:
                    summary.runs += 1
                    summary.errors += 1
                    print(f"  r{rnd+1} single  {label:<10} ERR: {e}")

            # Batch embedding
            for sample in EMBEDDING_SAMPLES:
                if "texts" not in sample:
                    continue
                label = sample["label"]
                texts = sample["texts"]
                try:
                    t0 = time.perf_counter()
                    resp = await client.embeddings.create(
                        model=model_id,
                        input=texts,
                        dimensions=VECTOR_DIM,
                    )
                    ms = int((time.perf_counter() - t0) * 1000)
                    tokens = resp.usage.total_tokens if resp.usage else 0
                    cost = tokens * price / 1_000_000

                    summary.runs += 1
                    summary.latencies.append(ms)
                    summary.total_tokens += tokens
                    summary.total_cost_usd += cost
                    summary.batch_latency_ms += ms
                    summary.batch_tokens += tokens
                    print(f"  r{rnd+1} batch   {label:<10} OK  {ms:>5}ms  {tokens:>4} tok  {len(texts)} items  ${cost:.8f}")
                except Exception as e:
                    summary.runs += 1
                    summary.errors += 1
                    print(f"  r{rnd+1} batch   {label:<10} ERR: {e}")

        # Semantic similarity quality test (run once, not per round)
        print(f"\n  Similarity quality test:")
        for query, positive, negative in SIMILARITY_PAIRS:
            try:
                resp = await client.embeddings.create(
                    model=model_id,
                    input=[query, positive, negative],
                    dimensions=VECTOR_DIM,
                )
                vecs = [item.embedding for item in sorted(resp.data, key=lambda x: x.index)]
                sim_pos = cosine_similarity(vecs[0], vecs[1])
                sim_neg = cosine_similarity(vecs[0], vecs[2])
                correct = sim_pos > sim_neg
                summary.similarity_total += 1
                if correct:
                    summary.similarity_correct += 1
                mark = "PASS" if correct else "FAIL"
                print(f"    {mark}  pos={sim_pos:.4f}  neg={sim_neg:.4f}  gap={sim_pos-sim_neg:+.4f}  q=\"{query[:50]}\"")
            except Exception as e:
                summary.similarity_total += 1
                print(f"    ERR   q=\"{query[:50]}\"  {e}")

        # Compute stats
        if summary.latencies:
            sorted_lat = sorted(summary.latencies)
            summary.avg_latency_ms = sum(sorted_lat) / len(sorted_lat)
            summary.min_latency_ms = sorted_lat[0]
            summary.max_latency_ms = sorted_lat[-1]
        successful = summary.runs - summary.errors
        if successful > 0:
            summary.avg_cost_per_call = summary.total_cost_usd / successful

        summaries.append(summary)

    return summaries


def print_embedding_report(summaries: list[EmbeddingSummary]):
    print("\n")
    print("=" * 100)
    print("  EMBEDDING BENCHMARK RESULTS")
    print("=" * 100)

    print(f"\n{'Model':<28} {'$/1M tok':>10} {'Avg ms':>8} {'Min ms':>8} {'Max ms':>8} "
          f"{'Total tok':>10} {'Total $':>12} {'Sim %':>8}")
    print("-" * 100)

    for s in summaries:
        sim_pct = (s.similarity_correct / s.similarity_total * 100) if s.similarity_total else 0
        print(
            f"{s.model:<28} ${s.price_per_1m:>8.3f} {s.avg_latency_ms:>8.0f} "
            f"{s.min_latency_ms:>8.0f} {s.max_latency_ms:>8.0f} "
            f"{s.total_tokens:>10} ${s.total_cost_usd:>11.8f} "
            f"{sim_pct:>7.0f}%"
        )

    # Cost projection
    print(f"\n{'─' * 100}")
    print("  EMBEDDING COST PROJECTION")
    print(f"{'─' * 100}")
    print(f"{'Model':<28} {'$/10K embeds':>14} {'$/100K embeds':>14} {'$/1M embeds':>14}")
    print("-" * 75)
    for s in summaries:
        if s.avg_cost_per_call > 0:
            print(
                f"{s.model:<28} ${s.avg_cost_per_call * 10_000:>13.4f} "
                f"${s.avg_cost_per_call * 100_000:>13.4f} "
                f"${s.avg_cost_per_call * 1_000_000:>13.2f}"
            )

    # Price ratio
    if len(summaries) == 2:
        s, l = summaries[0], summaries[1]
        if s.price_per_1m < l.price_per_1m:
            ratio = l.price_per_1m / s.price_per_1m
            print(f"\n  {l.model} is {ratio:.1f}x more expensive than {s.model}")
        sim_s = s.similarity_correct / max(s.similarity_total, 1)
        sim_l = l.similarity_correct / max(l.similarity_total, 1)
        if sim_l > sim_s:
            print(f"  {l.model} has better similarity accuracy ({sim_l*100:.0f}% vs {sim_s*100:.0f}%)")
        elif sim_s > sim_l:
            print(f"  {s.model} has equal or better similarity accuracy ({sim_s*100:.0f}% vs {sim_l*100:.0f}%)")
        else:
            print(f"  Both models have equal similarity accuracy ({sim_s*100:.0f}%)")

    print(f"\n  Current model: text-embedding-3-small (${EMBEDDING_MODELS[0][1]}/1M)")
    print(f"  MemClaw vector dimension: {VECTOR_DIM}")


def main():
    parser = argparse.ArgumentParser(description="Benchmark OpenAI LLMs + Embeddings for MemClaw")
    parser.add_argument("--rounds", type=int, default=1, help="Number of rounds per model (default: 1)")
    parser.add_argument("--models", nargs="*", help="Specific LLM model IDs to test (default: all)")
    parser.add_argument("--embeddings-only", action="store_true", help="Only run embedding benchmark")
    parser.add_argument("--llm-only", action="store_true", help="Only run LLM benchmark")
    args = parser.parse_args()

    run_llm = not args.embeddings_only
    run_emb = not args.llm_only

    # LLM benchmark
    if run_llm:
        models = MODELS
        if args.models:
            model_lookup = {m[0]: m for m in MODELS}
            models = []
            for name in args.models:
                if name in model_lookup:
                    models.append(model_lookup[name])
                else:
                    print(f"WARNING: Unknown model '{name}', add pricing to MODELS dict. Skipping.")

        if models:
            print(f"MemClaw OpenAI LLM Benchmark")
            print(f"Models: {len(models)} | Samples: {len(TEST_SAMPLES)} | Rounds: {args.rounds}")
            print(f"Tasks per sample: 2 (enrichment + entity extraction)")
            print(f"Total API calls: {len(models) * len(TEST_SAMPLES) * 2 * args.rounds}")

            summaries = asyncio.run(benchmark(models, args.rounds, TEST_SAMPLES))
            print_report(summaries)

    # Embedding benchmark
    if run_emb:
        print(f"\n\n{'#' * 100}")
        print(f"  EMBEDDING BENCHMARK")
        print(f"{'#' * 100}")
        print(f"Models: {len(EMBEDDING_MODELS)} | Rounds: {args.rounds}")

        emb_summaries = asyncio.run(benchmark_embeddings(args.rounds))
        if emb_summaries:
            print_embedding_report(emb_summaries)


if __name__ == "__main__":
    main()
