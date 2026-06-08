#!/usr/bin/env python3
"""MemClaw latency benchmark — measures write, search, and recall latency.

Usage:
    python scripts/latency_test.py                              # default: localhost:8000
    python scripts/latency_test.py --url http://localhost:8000
    python scripts/latency_test.py --url http://localhost:8000 --runs 20
    python scripts/latency_test.py --api-key mc_xxx --fleet-id eng
"""

import argparse
import statistics
import sys
import time
import uuid

import httpx

DEFAULT_URL = "http://localhost:8000"
DEFAULT_RUNS = 10
TIMEOUT = 30.0


WRITE_SAMPLES = [
    "The engineering team decided to migrate from RabbitMQ to Kafka for all event streaming by Q3 2026.",
    "Customer Acme Corp reported a 15% increase in API latency after the March 18 deploy. Root cause was an unindexed query on the orders table.",
    "Sprint retrospective: the team agreed to adopt trunk-based development and drop long-lived feature branches.",
    "Legal flagged that our EU data processing agreement expires April 30. Renewal must be signed by April 15.",
    "John completed the performance audit — P95 latency dropped from 450ms to 120ms after adding the ScaNN index.",
    "Product decided to sunset the legacy REST API by June 2026. All clients must migrate to GraphQL.",
    "Incident: payment service went down for 12 minutes on March 20 due to a certificate expiration. Automated renewal is now in place.",
    "The ML team trained a new embedding model — recall@10 improved from 0.82 to 0.91 on the internal benchmark.",
    "Budget approved: $24K/month for managed Postgres Enterprise tier starting April 1.",
    "Customer Beta Inc requested SOC 2 Type II compliance documentation. Deadline is March 28.",
    "Architecture decision: all new microservices must use gRPC for inter-service communication.",
    "QA found a race condition in the dedup logic — two concurrent writes with identical content can both succeed.",
    "The support team resolved 147 tickets last week, down from 183 the week before. Top category: authentication issues.",
    "DevOps migrated CI/CD from GitHub Actions to Cloud Build. Build times dropped from 8 minutes to 3 minutes.",
    "Sarah presented the Q1 roadmap: focus areas are memory crystallization, Slack integration, and enterprise SSO.",
    "Database backup verification passed — point-in-time recovery tested successfully to 5 minutes ago.",
    "The onboarding flow conversion rate increased from 23% to 31% after removing the email verification step.",
    "Security audit complete: no critical findings. Two medium-severity items flagged (CORS policy, rate limiting).",
    "Partner integration with Salesforce CRM is live. Bidirectional sync runs every 15 minutes.",
    "The entity extraction pipeline now processes 500 memories per minute, up from 120 after the batch optimization.",
]

SEARCH_QUERIES = [
    "database migration plans and timeline",
    "customer complaints about latency",
    "security audit findings",
    "team decisions about architecture",
    "budget approvals for infrastructure",
    "incident reports and resolutions",
    "performance benchmarks and improvements",
    "product roadmap and priorities",
    "compliance and legal deadlines",
    "CI/CD pipeline changes",
    "onboarding funnel metrics",
    "partner integrations status",
    "sprint retrospective action items",
    "ML model training results",
    "API deprecation timeline",
    "certificate management and automation",
    "support ticket trends",
    "data backup verification",
    "entity extraction throughput",
    "SOC 2 compliance documentation",
]


def percentile(data: list[float], p: float) -> float:
    """Calculate the p-th percentile of a sorted list."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[f]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def measure(func, *args, **kwargs) -> tuple:
    """Call func, return (result, elapsed_ms)."""
    t0 = time.perf_counter()
    result = func(*args, **kwargs)
    ms = round((time.perf_counter() - t0) * 1000)
    return result, ms


def run_benchmark(base_url: str, api_key: str | None, runs: int, fleet_id: str | None):
    tenant = f"latency-bench-{uuid.uuid4().hex[:8]}"
    api = f"{base_url.rstrip('/')}/api"
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key

    client = httpx.Client(headers=headers, timeout=TIMEOUT)

    # ── Health check ──
    r = client.get(f"{api}/health")
    if r.status_code != 200:
        print(f"  Health check failed: {r.status_code}")
        sys.exit(1)

    print(f"  Target:  {base_url}")
    print(f"  Tenant:  {tenant}")
    print(f"  Fleet:   {fleet_id or '(none)'}")
    print(f"  Runs:    {runs}")
    print()

    write_times: list[float] = []
    search_times: list[float] = []
    recall_times: list[float] = []
    memory_ids: list[str] = []
    server_write_times: list[float] = []
    server_search_times: list[float] = []

    # ── WRITE benchmark ──
    print(f"  Writing {runs} memories...")
    for i in range(runs):
        content = WRITE_SAMPLES[i % len(WRITE_SAMPLES)]
        # Add unique suffix to avoid dedup
        content += f" [bench-{uuid.uuid4().hex[:6]}]"

        body: dict = {
            "tenant_id": tenant,
            "agent_id": "latency-bench",
            "content": content,
        }
        if fleet_id:
            body["fleet_id"] = fleet_id

        r, ms = measure(client.post, f"{api}/memories", json=body)
        write_times.append(ms)

        if r.status_code in (200, 201):
            data = r.json()
            memory_ids.append(data["id"])
            # Capture server-side write latency from metadata
            meta = data.get("metadata", {})
            if meta.get("write_latency_ms"):
                server_write_times.append(meta["write_latency_ms"])
            # Capture server response time header
            srv = r.headers.get("X-Response-Time")
            if srv:
                server_write_times.append(int(srv))
        else:
            print(f"    Write {i+1} failed: {r.status_code} {r.text[:100]}")

        # Progress
        if (i + 1) % 5 == 0 or i == runs - 1:
            print(f"    {i+1}/{runs} done — last: {ms}ms")

    print()

    # Small pause to let embeddings settle
    time.sleep(1)

    # ── SEARCH benchmark ──
    print(f"  Running {runs} searches...")
    for i in range(runs):
        query = SEARCH_QUERIES[i % len(SEARCH_QUERIES)]
        body = {"tenant_id": tenant, "query": query}
        if fleet_id:
            body["fleet_ids"] = [fleet_id]

        r, ms = measure(client.post, f"{api}/search", json=body)
        search_times.append(ms)

        if r.status_code == 200:
            srv = r.headers.get("X-Response-Time")
            if srv:
                server_search_times.append(int(srv))
        else:
            print(f"    Search {i+1} failed: {r.status_code}")

        if (i + 1) % 5 == 0 or i == runs - 1:
            print(f"    {i+1}/{runs} done — last: {ms}ms")

    print()

    # ── RECALL benchmark ──
    print(f"  Running {runs} recalls...")
    for i in range(runs):
        query = SEARCH_QUERIES[i % len(SEARCH_QUERIES)]
        body = {"tenant_id": tenant, "query": query}
        if fleet_id:
            body["fleet_ids"] = [fleet_id]

        r, ms = measure(client.post, f"{api}/recall", json=body)
        recall_times.append(ms)

        if r.status_code != 200:
            print(f"    Recall {i+1} failed: {r.status_code}")

        if (i + 1) % 5 == 0 or i == runs - 1:
            print(f"    {i+1}/{runs} done — last: {ms}ms")

    print()

    # ── Cleanup ──
    client.delete(f"{api}/memories", params={"tenant_id": tenant})
    client.delete(f"{api}/admin/tenants/{tenant}")

    # ── Report ──
    print("=" * 64)
    print("  MEMCLAW LATENCY REPORT")
    print("=" * 64)
    print(f"  Target:   {base_url}")
    print(f"  Runs:     {runs} per tool")
    print(f"  Memories: {len(memory_ids)} written")
    print()

    def report_section(name: str, times: list[float], server_times: list[float] | None = None):
        if not times:
            print(f"  {name}: no data")
            return
        print(f"  {name} (client round-trip)")
        print(f"    Min:     {min(times):>7.0f} ms")
        print(f"    Max:     {max(times):>7.0f} ms")
        print(f"    Mean:    {statistics.mean(times):>7.0f} ms")
        print(f"    Median:  {statistics.median(times):>7.0f} ms")
        print(f"    P90:     {percentile(times, 90):>7.0f} ms")
        print(f"    P95:     {percentile(times, 95):>7.0f} ms")
        print(f"    P99:     {percentile(times, 99):>7.0f} ms")
        print(f"    Stdev:   {statistics.stdev(times):>7.0f} ms" if len(times) > 1 else "")
        if server_times:
            print(f"  {name} (server-side)")
            print(f"    Mean:    {statistics.mean(server_times):>7.0f} ms")
            print(f"    P95:     {percentile(server_times, 95):>7.0f} ms")
        print()

    report_section("WRITE (memclaw_write)", write_times, server_write_times)
    report_section("RECALL (memclaw_recall)", search_times, server_search_times)
    report_section("RECALL+BRIEF (memclaw_recall, include_brief=true)", recall_times)

    # ── Summary table ──
    print("  " + "-" * 60)
    print(f"  {'Tool':<25} {'Mean':>8} {'P50':>8} {'P95':>8} {'P99':>8}")
    print("  " + "-" * 60)
    for name, times in [("memclaw_write", write_times), ("memclaw_recall", search_times), ("memclaw_recall+brief", recall_times)]:
        if times:
            print(f"  {name:<25} {statistics.mean(times):>7.0f}ms {statistics.median(times):>7.0f}ms {percentile(times, 95):>7.0f}ms {percentile(times, 99):>7.0f}ms")
    print("  " + "-" * 60)
    print()


def main():
    parser = argparse.ArgumentParser(description="MemClaw latency benchmark")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default: {DEFAULT_URL})")
    parser.add_argument("--api-key", default=None, help="API key (admin or tenant-scoped)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"Number of runs per tool (default: {DEFAULT_RUNS})")
    parser.add_argument("--fleet-id", default=None, help="Optional fleet ID")
    args = parser.parse_args()

    print()
    print("  MemClaw Latency Benchmark")
    print()

    run_benchmark(args.url, args.api_key, args.runs, args.fleet_id)


if __name__ == "__main__":
    main()
