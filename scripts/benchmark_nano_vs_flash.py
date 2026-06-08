#!/usr/bin/env python3
"""Quick benchmark: GPT-5.4-nano vs Gemini 3.1 Flash Lite.

Measures latency, output quality, and token throughput on MemClaw-relevant tasks:
  1. Memory classification (type + title from content)
  2. Entity extraction
  3. Summarization / recall synthesis

Usage:
    python scripts/benchmark_nano_vs_flash.py
"""

import asyncio
import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
GOOGLE_API_KEY = os.environ["GOOGLE_API_KEY"]

RUNS = 3  # repeat each task N times for averaging

# ── Pricing (per 1M tokens, USD) ──
# GPT-5.4-nano: https://openai.com/api/pricing/
# Gemini 3.1 Flash Lite: https://ai.google.dev/pricing

PRICING = {
    "gpt": {"input": 0.10, "output": 0.40, "label": "GPT-5.4-nano"},
    "gemini": {"input": 0.025, "output": 0.15, "label": "Gemini 3.1 Flash Lite"},
}

# ── Prompts (MemClaw-relevant tasks) ──

TASKS = [
    {
        "name": "Memory classification",
        "prompt": (
            "Classify this memory and return JSON with keys: memory_type, title, tags, weight (0-1).\n"
            "Memory types: fact, episode, decision, preference, task, plan, intention, commitment, action, outcome, cancellation, rule.\n\n"
            "Content: \"We decided to migrate from MySQL to PostgreSQL 16 for the analytics service because of "
            "better JSON support and pgvector for embeddings. Migration deadline is June 15, 2026.\"\n\n"
            "Return ONLY valid JSON, no markdown."
        ),
    },
    {
        "name": "Entity extraction",
        "prompt": (
            "Extract all entities (people, organizations, technologies, projects) and their relations from this text. "
            "Return JSON with keys: entities (list of {name, type}), relations (list of {from, relation, to}).\n\n"
            "Text: \"Sarah Chen from the Platform team led the Redis cluster migration for Project Atlas. "
            "She coordinated with DevOps lead Mike Torres to set up the new 3-node cluster on AWS. "
            "The migration was approved by CTO Lisa Park.\"\n\n"
            "Return ONLY valid JSON, no markdown."
        ),
    },
    {
        "name": "Recall synthesis",
        "prompt": (
            "Synthesize a concise context paragraph (3-4 sentences) from these memories:\n\n"
            "1. [decision] PostgreSQL 16 chosen over MySQL for analytics — pgvector support\n"
            "2. [task] Migration deadline June 15, 2026\n"
            "3. [episode] Redis cluster migration completed March 10, 2026 — 3-node setup on AWS\n"
            "4. [fact] Platform team owns analytics infrastructure\n"
            "5. [outcome] Redis migration reduced cache latency from 12ms to 3ms\n\n"
            "Query: What's the status of our infrastructure migrations?"
        ),
    },
]

# ── API calls ──

async def call_openai(client: httpx.AsyncClient, prompt: str) -> tuple[str, float, int, int]:
    """Returns (response_text, latency_ms, input_tokens, output_tokens)."""
    t0 = time.perf_counter()
    r = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        json={
            "model": "gpt-5.4-nano",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_completion_tokens": 512,
        },
        timeout=60,
    )
    latency = (time.perf_counter() - t0) * 1000
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return text, latency, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


async def call_gemini(client: httpx.AsyncClient, prompt: str) -> tuple[str, float, int, int]:
    """Returns (response_text, latency_ms, input_tokens, output_tokens)."""
    t0 = time.perf_counter()
    r = await client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={GOOGLE_API_KEY}",
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 512},
        },
        timeout=60,
    )
    latency = (time.perf_counter() - t0) * 1000
    data = r.json()
    if "candidates" not in data:
        raise RuntimeError(f"Gemini API error: {json.dumps(data, indent=2)}")
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    usage = data.get("usageMetadata", {})
    return text, latency, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)


# ── Runner ──

async def benchmark():
    print()
    print("  GPT-5.4-nano vs Gemini 3.1 Flash Lite — MemClaw Task Benchmark")
    print("  " + "=" * 62)
    print(f"  Runs per task: {RUNS}")
    print()

    async with httpx.AsyncClient() as client:
        # Warm up both APIs (generous timeout for cold starts)
        warm_client = httpx.AsyncClient(timeout=60)
        try:
            await call_openai(warm_client, "Say hi")
            await call_gemini(warm_client, "Say hi")
        finally:
            await warm_client.aclose()

        def calc_cost(provider: str, input_tok: float, output_tok: float) -> float:
            """Cost in USD for a single call."""
            p = PRICING[provider]
            return (input_tok * p["input"] + output_tok * p["output"]) / 1_000_000

        results = []

        for task in TASKS:
            gpt_latencies = []
            gem_latencies = []
            gpt_out_tokens = []
            gem_out_tokens = []
            gpt_in_tokens = []
            gem_in_tokens = []
            gpt_last = ""
            gem_last = ""

            for _ in range(RUNS):
                text, lat, i_tok, o_tok = await call_openai(client, task["prompt"])
                gpt_latencies.append(lat)
                gpt_in_tokens.append(i_tok)
                gpt_out_tokens.append(o_tok)
                gpt_last = text

                text, lat, i_tok, o_tok = await call_gemini(client, task["prompt"])
                gem_latencies.append(lat)
                gem_in_tokens.append(i_tok)
                gem_out_tokens.append(o_tok)
                gem_last = text

            gpt_avg = sum(gpt_latencies) / len(gpt_latencies)
            gem_avg = sum(gem_latencies) / len(gem_latencies)
            gpt_out_avg = sum(gpt_out_tokens) / len(gpt_out_tokens)
            gem_out_avg = sum(gem_out_tokens) / len(gem_out_tokens)
            gpt_in_avg = sum(gpt_in_tokens) / len(gpt_in_tokens)
            gem_in_avg = sum(gem_in_tokens) / len(gem_in_tokens)
            gpt_tps = (gpt_out_avg / gpt_avg * 1000) if gpt_avg > 0 else 0
            gem_tps = (gem_out_avg / gem_avg * 1000) if gem_avg > 0 else 0
            gpt_cost = calc_cost("gpt", gpt_in_avg, gpt_out_avg)
            gem_cost = calc_cost("gemini", gem_in_avg, gem_out_avg)

            winner_speed = "GPT" if gpt_avg < gem_avg else "Gemini"
            diff = abs(gpt_avg - gem_avg) / max(gpt_avg, gem_avg) * 100

            results.append({
                "task": task["name"],
                "gpt_avg_ms": round(gpt_avg),
                "gem_avg_ms": round(gem_avg),
                "gpt_tok_s": round(gpt_tps, 1),
                "gem_tok_s": round(gem_tps, 1),
                "gpt_cost": gpt_cost,
                "gem_cost": gem_cost,
                "winner_speed": winner_speed,
                "diff_pct": round(diff, 1),
            })

            print(f"  {task['name']}")
            print(f"    GPT-5.4-nano:           {gpt_avg:7.0f}ms  ({gpt_tps:5.1f} tok/s)  ${gpt_cost:.6f}/call")
            print(f"    Gemini 3.1 Flash Lite:  {gem_avg:7.0f}ms  ({gem_tps:5.1f} tok/s)  ${gem_cost:.6f}/call")
            print(f"    Speed: {winner_speed} ({diff:.1f}% faster)  |  Cost: {'GPT' if gpt_cost < gem_cost else 'Gemini'} ({abs(gpt_cost - gem_cost) / max(gpt_cost, gem_cost) * 100:.0f}% cheaper)")
            print()
            print(f"    GPT sample output:")
            for line in gpt_last[:200].split("\n"):
                print(f"      {line}")
            print(f"    Gemini sample output:")
            for line in gem_last[:200].split("\n"):
                print(f"      {line}")
            print()

        # Summary table
        w = 78
        print("  " + "=" * w)
        print(f"  {'Task':<25} {'GPT (ms)':>8} {'Gem (ms)':>9} {'GPT $/call':>11} {'Gem $/call':>11} {'Speed':>7} {'Cost':>7}")
        print("  " + "-" * w)
        for r in results:
            print(f"  {r['task']:<25} {r['gpt_avg_ms']:>8} {r['gem_avg_ms']:>9} {r['gpt_cost']:>11.6f} {r['gem_cost']:>11.6f} {'GPT' if r['gpt_avg_ms'] < r['gem_avg_ms'] else 'Gem':>7} {'GPT' if r['gpt_cost'] < r['gem_cost'] else 'Gem':>7}")
        print("  " + "=" * w)

        gpt_total_ms = sum(r["gpt_avg_ms"] for r in results)
        gem_total_ms = sum(r["gem_avg_ms"] for r in results)
        gpt_total_cost = sum(r["gpt_cost"] for r in results)
        gem_total_cost = sum(r["gem_cost"] for r in results)

        print(f"  Latency:  GPT {gpt_total_ms}ms  vs  Gemini {gem_total_ms}ms  ->  {'GPT-5.4-nano' if gpt_total_ms < gem_total_ms else 'Gemini 3.1 Flash Lite'} faster")
        print(f"  Cost:     GPT ${gpt_total_cost:.6f}  vs  Gemini ${gem_total_cost:.6f}  ->  {'GPT-5.4-nano' if gpt_total_cost < gem_total_cost else 'Gemini 3.1 Flash Lite'} cheaper")
        cost_per_1k = {"gpt": gpt_total_cost * 1000, "gem": gem_total_cost * 1000}
        print(f"  At 1K calls:  GPT ${cost_per_1k['gpt']:.2f}  vs  Gemini ${cost_per_1k['gem']:.2f}")
        print()


if __name__ == "__main__":
    asyncio.run(benchmark())
