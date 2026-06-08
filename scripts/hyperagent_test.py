"""
HyperAgent Search Benchmark — Per-agent search profile optimization.

Seeds hundreds of memories, then systematically explores search profiles
via memclaw_tune over MCP to find optimal retrieval configurations.

Usage:
    python scripts/hyperagent_test.py --universe acme --api-key mc_2026
    python scripts/hyperagent_test.py --universe wiki --api-key mc_2026
    python scripts/hyperagent_test.py --universe mixed --api-key mc_2026 --skip-seed
"""

import argparse
import json
import os
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone

import httpx

# ── Config ──

DEFAULT_URL = "http://localhost:8000"
BENCHMARK_AGENT = "hyperagent"
BULK_BATCH_SIZE = 100
TIMEOUT = 60.0


# ══════════════════════════════════════════════════════════════════════════════
# MCP Client — Minimal Streamable HTTP client for tool calls
# ══════════════════════════════════════════════════════════════════════════════


class MCPClient:
    """Speaks JSON-RPC 2.0 over MCP Streamable HTTP."""

    def __init__(self, base_url: str, api_key: str):
        self.url = f"{base_url.rstrip('/')}/mcp/"
        self.headers = {
            "X-API-Key": api_key,
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self.client = httpx.Client(timeout=TIMEOUT)
        self.session_id: str | None = None
        self._id_counter = 0

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _post(self, body: dict) -> httpx.Response:
        headers = {**self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return self.client.post(self.url, json=body, headers=headers)

    def initialize(self):
        """Initialize MCP session."""
        r = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "hyperagent-bench", "version": "1.0"},
            },
        })
        # Extract session ID from header or SSE body
        sid = r.headers.get("Mcp-Session-Id")
        if not sid:
            # Parse SSE for session ID in response headers
            for line in r.text.split("\n"):
                if line.startswith("data: "):
                    try:
                        obj = json.loads(line[6:])
                        if obj.get("result", {}).get("protocolVersion"):
                            # Session ID may be in a separate header event
                            pass
                    except json.JSONDecodeError:
                        pass
            # Try from response headers again (some implementations send it late)
            sid = r.headers.get("mcp-session-id") or r.headers.get("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        # Send initialized notification
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def call_tool(self, name: str, arguments: dict) -> str:
        """Call an MCP tool, return the text content of the result."""
        r = self._post({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return self._parse_response(r)

    def _parse_response(self, r: httpx.Response) -> str:
        """Parse JSON-RPC or SSE response, extract tool result text."""
        ct = r.headers.get("content-type", "")
        if "text/event-stream" in ct:
            # SSE: find the last data line with a JSON-RPC result
            text = ""
            for line in r.text.split("\n"):
                if line.startswith("data: "):
                    data = line[6:]
                    try:
                        obj = json.loads(data)
                        if "result" in obj:
                            content = obj["result"].get("content", [])
                            for c in content:
                                if c.get("type") == "text":
                                    text = c["text"]
                    except json.JSONDecodeError:
                        pass
            return text
        else:
            # Direct JSON response
            try:
                obj = r.json()
                if "result" in obj:
                    content = obj["result"].get("content", [])
                    for c in content:
                        if c.get("type") == "text":
                            return c["text"]
                return r.text
            except Exception:
                return r.text

    def search(self, query: str, agent_id: str | None = None, **kwargs) -> list[dict]:
        """Call memclaw_recall (without brief), return parsed memory list."""
        args = {"query": query, **kwargs}
        if agent_id:
            args["agent_id"] = agent_id
        text = self.call_tool("memclaw_recall", args)
        # Strip latency suffix if present
        text = re.sub(r'\n\n_latency_ms: \d+$', '', text)
        if text.startswith("No memories found"):
            return []
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return []

    def tune(self, agent_id: str, **profile) -> dict:
        """Call memclaw_tune, return parsed response."""
        args = {"agent_id": agent_id, **profile}
        text = self.call_tool("memclaw_tune", args)
        text = re.sub(r'\n\n_latency_ms: \d+$', '', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    def brief(self, query: str, **kwargs) -> dict:
        """Call memclaw_recall with include_brief=True, return parsed response."""
        args = {"query": query, "include_brief": True, **kwargs}
        text = self.call_tool("memclaw_recall", args)
        text = re.sub(r'\n\n_latency_ms: \d+$', '', text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}


# ══════════════════════════════════════════════════════════════════════════════
# Universe 1: ACME CORP — Synthetic company
# ══════════════════════════════════════════════════════════════════════════════

ACME_MEMORIES = [
    # ── Engineering ──
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "fact", "content": "The payments service uses Stripe API v2023-10-16. All webhook handlers are in payments/webhooks.py. The signing secret is rotated quarterly.", "weight": 0.9, "tag": "acme-payments-stripe"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "fact", "content": "PostgreSQL 16 is the primary database. Connection pooling via PgBouncer on the same host. Max pool size is 50 connections.", "weight": 0.95, "tag": "acme-db-postgres"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "decision", "content": "Decided to migrate from Redis Sentinel to Redis Cluster for the session store. The main driver is horizontal scalability — current setup maxes out at 32GB and we're at 28GB. Migration planned for Q2.", "weight": 0.85, "tag": "acme-redis-migration"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "fact", "content": "User authentication uses OAuth2 with PKCE. Identity provider is Auth0. Session tokens expire after 24 hours, refresh tokens after 30 days.", "weight": 0.9, "tag": "acme-auth-oauth2"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "episode", "content": "Discovered a race condition in the order processing pipeline on March 3rd. Two concurrent requests could claim the same inventory item. Fixed by adding a distributed lock using Redis SETNX with a 30-second TTL.", "weight": 0.8, "tag": "acme-race-condition"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "fact", "content": "The search service uses Elasticsearch 8.12 with a custom analyzer for product names. Index aliases rotate weekly. Cluster has 3 data nodes with 500GB SSD each.", "weight": 0.85, "tag": "acme-elasticsearch"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "decision", "content": "Chose gRPC over REST for internal service-to-service communication between order service and inventory service. Reason: strict schema enforcement via protobuf and 3x throughput improvement.", "weight": 0.9, "tag": "acme-grpc-decision"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "fact", "content": "The notification service processes 2.3 million emails per day through Amazon SES. Bounce rate is 0.4%. Complaint rate is 0.02%. Dedicated IP pool.", "weight": 0.8, "tag": "acme-email-ses"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "episode", "content": "Database migration 047_add_audit_columns failed in staging on March 5th. Root cause: added NOT NULL columns without defaults to a 12M row table. Fix: split into add nullable, backfill, then alter to NOT NULL.", "weight": 0.85, "tag": "acme-migration-failure"},
    {"fleet_id": "engineering", "agent_id": "backend-dev", "memory_type": "fact", "content": "The API rate limiter uses a sliding window algorithm with Redis sorted sets. Default limit is 1000 requests per minute per API key. Enterprise customers get 10,000 RPM.", "weight": 0.8, "tag": "acme-rate-limiter"},
    # Frontend
    {"fleet_id": "engineering", "agent_id": "frontend-dev", "memory_type": "fact", "content": "The web app uses Next.js 14 with the App Router. Pages are mostly server components. Client components are in /components/interactive.", "weight": 0.9, "tag": "acme-nextjs"},
    {"fleet_id": "engineering", "agent_id": "frontend-dev", "memory_type": "decision", "content": "Switched from styled-components to Tailwind CSS v3. Reasons: faster build times, smaller bundle, design team uses Tailwind Figma tokens.", "weight": 0.8, "tag": "acme-tailwind"},
    {"fleet_id": "engineering", "agent_id": "frontend-dev", "memory_type": "fact", "content": "Mobile app is React Native 0.73. Shares 60% of business logic via @acme/core package. State management is Zustand.", "weight": 0.85, "tag": "acme-react-native"},
    {"fleet_id": "engineering", "agent_id": "frontend-dev", "memory_type": "episode", "content": "Lighthouse score dropped from 92 to 67 after dashboard redesign on March 1st. Root cause: unoptimized SVGs (800KB) and synchronous charting library. Fixed by lazy-loading and PNG conversion.", "weight": 0.75, "tag": "acme-lighthouse-drop"},
    {"fleet_id": "engineering", "agent_id": "frontend-dev", "memory_type": "preference", "content": "Design system uses a 4px grid. All spacing must be multiples of 4. Colors from @acme/design-tokens. Never use raw hex values.", "weight": 0.9, "tag": "acme-design-system"},
    {"fleet_id": "engineering", "agent_id": "frontend-dev", "memory_type": "fact", "content": "E2E tests use Playwright with 3 CI shards. Average run time 4 minutes. Chrome and Firefox only — Safari tested manually.", "weight": 0.8, "tag": "acme-playwright"},
    # DevOps
    {"fleet_id": "engineering", "agent_id": "devops", "memory_type": "fact", "content": "Production runs on GKE Autopilot in us-east1. 14 microservices as Kubernetes Deployments. Helm charts in infrastructure/charts.", "weight": 0.95, "tag": "acme-gke"},
    {"fleet_id": "engineering", "agent_id": "devops", "memory_type": "fact", "content": "CI/CD uses GitHub Actions. Pipeline builds Docker images, pushes to Artifact Registry, deploys via ArgoCD. Average deploy: 6 minutes. Auto-rollback on health check failure.", "weight": 0.9, "tag": "acme-cicd"},
    {"fleet_id": "engineering", "agent_id": "devops", "memory_type": "decision", "content": "Migrated monitoring from Datadog to Grafana Cloud. Cost dropped from $18K/month to $6K/month. Alert parity achieved by March 8th. On-call still uses PagerDuty.", "weight": 0.85, "tag": "acme-grafana-migration"},
    {"fleet_id": "engineering", "agent_id": "devops", "memory_type": "episode", "content": "Production outage on March 7th, 14:23 UTC. API gateway ran out of file descriptors. Duration: 23 minutes. Root cause: connection leak in WebSocket handler v2.4.1. Fixed in v2.4.2.", "weight": 1.0, "tag": "acme-outage-march7"},
    {"fleet_id": "engineering", "agent_id": "devops", "memory_type": "fact", "content": "Database backups run every 6 hours via Cloud SQL automated backups. Point-in-time recovery enabled. 30-day retention. Monthly restore tests on the 15th.", "weight": 0.9, "tag": "acme-backups"},
    {"fleet_id": "engineering", "agent_id": "devops", "memory_type": "decision", "content": "Adopted Terraform Cloud for infrastructure state management. Previously S3 bucket with manual locking. Reduces drift risk, adds policy-as-code via Sentinel.", "weight": 0.85, "tag": "acme-terraform"},
    # QA
    {"fleet_id": "engineering", "agent_id": "qa", "memory_type": "fact", "content": "Test coverage at 78% (target 85%). Lowest modules: payments/refunds.py (42%), integrations/erp_sync.py (38%).", "weight": 0.8, "tag": "acme-test-coverage"},
    {"fleet_id": "engineering", "agent_id": "qa", "memory_type": "episode", "content": "Load test on March 6th: product catalog degrades above 800 req/s. P99 jumped from 120ms to 2.8s. Bottleneck: missing composite index on (category_id, status, created_at).", "weight": 0.85, "tag": "acme-load-test"},
    {"fleet_id": "engineering", "agent_id": "qa", "memory_type": "fact", "content": "API contract tests use Pact. Provider verification on every PR. 47 pact interactions from frontend and mobile.", "weight": 0.75, "tag": "acme-pact-tests"},
    # Eng Lead
    {"fleet_id": "engineering", "agent_id": "eng-lead", "memory_type": "decision", "content": "Approved microservices split for the billing module. Target: end of Q2. Team: 2 backend devs for 6 weeks. Unblocks the pricing overhaul sales needs.", "weight": 0.95, "tag": "acme-billing-split", "visibility": "scope_org"},
    {"fleet_id": "engineering", "agent_id": "eng-lead", "memory_type": "fact", "content": "Team: 5 backend, 3 frontend, 2 DevOps, 2 QA, 1 PM. Hiring a senior platform engineer — 3 candidates in final rounds.", "weight": 0.7, "tag": "acme-team-composition"},
    {"fleet_id": "engineering", "agent_id": "eng-lead", "memory_type": "episode", "content": "Sprint retro on March 10th: recurring theme is too many context switches from support escalations. Action: implement weekly support rotation.", "weight": 0.7, "tag": "acme-retro-march10"},
    {"fleet_id": "engineering", "agent_id": "eng-lead", "memory_type": "fact", "content": "Technical debt score (SonarQube): B overall. 23 security hotspots, 142 code smells in legacy order module, 8 medium-severity bugs.", "weight": 0.75, "tag": "acme-tech-debt"},

    # ── Sales ──
    {"fleet_id": "sales", "agent_id": "account-exec", "memory_type": "fact", "content": "Q1 pipeline at $2.4M, up 18% from Q4. Largest deals: Globex Corp ($450K), Initech ($320K), Umbrella Industries ($280K). Weighted pipeline: $1.1M.", "weight": 0.95, "tag": "acme-pipeline-q1", "visibility": "scope_org"},
    {"fleet_id": "sales", "agent_id": "account-exec", "memory_type": "decision", "content": "Raised enterprise tier minimum from $50K to $75K ARR. Deals under $75K have 3x support cost and 40% lower renewal. SDRs redirect smaller prospects to self-serve.", "weight": 0.9, "tag": "acme-enterprise-min"},
    {"fleet_id": "sales", "agent_id": "account-exec", "memory_type": "fact", "content": "Globex Corp uses Salesforce for CRM and Workday for HR. IT team of 12. Decision maker: Sarah Chen (VP Eng). Evaluator: Marcus Rodriguez (Staff Engineer).", "weight": 0.9, "tag": "acme-globex-contacts"},
    {"fleet_id": "sales", "agent_id": "account-exec", "memory_type": "episode", "content": "Demo with Globex Corp on March 8th. Sarah Chen impressed by API-first approach. Main concern: EU data residency for GDPR. Marcus wants Kafka POC.", "weight": 0.85, "tag": "acme-globex-demo"},
    {"fleet_id": "sales", "agent_id": "account-exec", "memory_type": "fact", "content": "Initech evaluating us vs DataSync Pro and FlowHub. Use case: real-time inventory sync across 200+ retail locations. Budget: $400K approved.", "weight": 0.9, "tag": "acme-initech-eval"},
    {"fleet_id": "sales", "agent_id": "account-exec", "memory_type": "episode", "content": "Lost Wayne Enterprises deal on March 4th. They chose a competitor due to missing SOC2 Type II certification. Our audit is April. Third deal lost to compliance this quarter.", "weight": 0.9, "tag": "acme-wayne-lost"},
    {"fleet_id": "sales", "agent_id": "sdr", "memory_type": "episode", "content": "Cold outreach to Stark Industries resulted in meeting with CTO Tony Stark on March 15th. Building AI supply chain platform, needs data layer. Est. deal: $500K+.", "weight": 0.9, "tag": "acme-stark-meeting"},
    {"fleet_id": "sales", "agent_id": "sdr", "memory_type": "fact", "content": "Average sales cycle: 47 days mid-market ($50K-$200K), 92 days enterprise ($200K+). Win rate: 28% mid-market, 22% enterprise.", "weight": 0.8, "tag": "acme-sales-cycle"},
    {"fleet_id": "sales", "agent_id": "solutions-eng", "memory_type": "fact", "content": "Technical requirements for Globex Corp POC: Kafka Connect integration, sub-100ms latency, EU data residency (Frankfurt region), SSO via SAML 2.0.", "weight": 0.85, "tag": "acme-globex-requirements"},
    {"fleet_id": "sales", "agent_id": "solutions-eng", "memory_type": "episode", "content": "Completed Initech technical evaluation on March 12th. Successfully synced 50K products across 10 test stores in 4.2 seconds. They were impressed with the webhook reliability.", "weight": 0.8, "tag": "acme-initech-poc"},

    # ── Product ──
    {"fleet_id": "product", "agent_id": "pm", "memory_type": "decision", "content": "Q2 roadmap prioritizes: 1) Billing module split (eng dependency), 2) Self-serve analytics dashboard, 3) Mobile push notifications for order status. Deferred: AI-powered recommendations.", "weight": 0.95, "tag": "acme-q2-roadmap", "visibility": "scope_org"},
    {"fleet_id": "product", "agent_id": "pm", "memory_type": "plan", "content": "Self-serve analytics dashboard: target beta by June 15. Features: custom date ranges, CSV export, saved views, team sharing. Tech: Metabase embedded via iframe.", "weight": 0.85, "tag": "acme-analytics-plan"},
    {"fleet_id": "product", "agent_id": "pm", "memory_type": "intention", "content": "Planning to deprecate the legacy reporting API (v1) by end of Q3. 23 active integrations still using it. Migration guide needed by May.", "weight": 0.8, "tag": "acme-deprecate-v1"},
    {"fleet_id": "product", "agent_id": "pm", "memory_type": "fact", "content": "NPS score is 42 (up from 38 last quarter). Top complaints: slow dashboard loading (34%), missing bulk operations (28%), and confusing pricing page (19%).", "weight": 0.8, "tag": "acme-nps-score"},
    {"fleet_id": "product", "agent_id": "designer", "memory_type": "decision", "content": "New dashboard redesign uses a sidebar navigation pattern instead of top tabs. User testing showed 23% faster task completion. Dark mode will be the default.", "weight": 0.8, "tag": "acme-dashboard-redesign"},
    {"fleet_id": "product", "agent_id": "designer", "memory_type": "preference", "content": "All new features must include keyboard shortcuts. Accessibility score must be AA minimum. No animations longer than 300ms.", "weight": 0.75, "tag": "acme-ux-standards"},
    {"fleet_id": "product", "agent_id": "analyst", "memory_type": "fact", "content": "Monthly active users: 12,400. Daily active: 3,200. Retention rate: 82% at 30 days, 64% at 90 days. Highest churn segment: solo users on free plan.", "weight": 0.85, "tag": "acme-user-metrics"},
    {"fleet_id": "product", "agent_id": "analyst", "memory_type": "fact", "content": "Feature usage ranking: 1) Search (89% of sessions), 2) Dashboard (76%), 3) Integrations (54%), 4) Reports (41%), 5) API playground (23%).", "weight": 0.8, "tag": "acme-feature-usage"},

    # ── Operations ──
    {"fleet_id": "operations", "agent_id": "finance", "memory_type": "fact", "content": "Monthly burn rate: $280K. Runway: 14 months at current rate. Revenue: $180K MRR growing 12% month-over-month. Break-even target: Q1 next year.", "weight": 0.95, "tag": "acme-burn-rate", "visibility": "scope_org"},
    {"fleet_id": "operations", "agent_id": "finance", "memory_type": "decision", "content": "Approved $45K budget for SOC2 Type II audit with Vanta. Audit starts April 1st. Expected completion: 8 weeks. This unblocks 3 enterprise deals worth $1M+.", "weight": 0.9, "tag": "acme-soc2-budget"},
    {"fleet_id": "operations", "agent_id": "finance", "memory_type": "commitment", "content": "Board approved Series A terms: $8M at $40M pre-money valuation. Lead investor: Sequoia. Closing expected by April 30th.", "weight": 1.0, "tag": "acme-series-a"},
    {"fleet_id": "operations", "agent_id": "hr", "memory_type": "fact", "content": "Current headcount: 32. Open positions: 4 (senior platform eng, 2 SDRs, product marketing manager). Target headcount by Q3: 42.", "weight": 0.7, "tag": "acme-headcount"},
    {"fleet_id": "operations", "agent_id": "hr", "memory_type": "decision", "content": "Switching from Gusto to Rippling for HRIS. Rippling handles international payroll for the 4 remote employees in EU. Migration by April 15th.", "weight": 0.75, "tag": "acme-hris-switch"},
    {"fleet_id": "operations", "agent_id": "legal", "memory_type": "fact", "content": "GDPR DPA template updated on March 1st. All new enterprise contracts must include the updated DPA. 12 existing customers need DPA amendments.", "weight": 0.85, "tag": "acme-gdpr-dpa"},
    {"fleet_id": "operations", "agent_id": "legal", "memory_type": "commitment", "content": "SOC2 Type II audit engagement letter signed with Vanta on March 10th. Scope: trust services criteria (security, availability, confidentiality). 60-day observation window.", "weight": 0.9, "tag": "acme-soc2-signed"},
    {"fleet_id": "operations", "agent_id": "legal", "memory_type": "fact", "content": "IP assignment agreements completed for all 32 employees. Patent application filed for the real-time sync algorithm on February 28th. Application number: US-2024-0128.", "weight": 0.8, "tag": "acme-ip-patent"},
]

ACME_QUERIES = [
    # Precision — specific, 1-2 answers
    {"query": "What database does the payments service use?", "expected": ["acme-payments-stripe", "acme-db-postgres"], "category": "precision"},
    {"query": "What is the production Kubernetes setup?", "expected": ["acme-gke", "acme-cicd"], "category": "precision"},
    {"query": "What authentication provider does Acme use?", "expected": ["acme-auth-oauth2"], "category": "precision"},
    {"query": "What is the current NPS score?", "expected": ["acme-nps-score"], "category": "precision"},
    {"query": "What is the monthly burn rate and runway?", "expected": ["acme-burn-rate"], "category": "precision"},
    {"query": "What testing framework is used for end-to-end tests?", "expected": ["acme-playwright"], "category": "precision"},
    # Recall — broad topic, trimmed to top 2-3
    {"query": "Tell me about the Globex Corp deal", "expected": ["acme-globex-contacts", "acme-globex-demo", "acme-globex-requirements"], "category": "recall"},
    {"query": "What infrastructure decisions were made recently?", "expected": ["acme-redis-migration", "acme-grafana-migration", "acme-terraform"], "category": "recall"},
    {"query": "What are the current engineering team challenges?", "expected": ["acme-tech-debt", "acme-test-coverage"], "category": "recall"},
    {"query": "What compliance and legal matters are pending?", "expected": ["acme-soc2-budget", "acme-soc2-signed"], "category": "recall"},
    # Temporal — recent events
    {"query": "What happened in the first week of March?", "expected": ["acme-race-condition", "acme-migration-failure"], "category": "temporal"},
    {"query": "What production incidents occurred recently?", "expected": ["acme-outage-march7", "acme-race-condition"], "category": "temporal"},
    # Cross-fleet
    {"query": "What is blocking enterprise sales deals?", "expected": ["acme-wayne-lost", "acme-soc2-budget"], "category": "cross-fleet"},
    {"query": "What is the Q2 company roadmap and priorities?", "expected": ["acme-q2-roadmap", "acme-billing-split"], "category": "cross-fleet"},
    {"query": "What are the hiring and team growth plans?", "expected": ["acme-headcount", "acme-team-composition"], "category": "cross-fleet"},
    # Entity — names, companies
    {"query": "Who is Sarah Chen and what is her role?", "expected": ["acme-globex-contacts", "acme-globex-demo"], "category": "entity"},
    {"query": "What do we know about Initech?", "expected": ["acme-initech-eval", "acme-initech-poc"], "category": "entity"},
    {"query": "What technologies does the frontend team use?", "expected": ["acme-nextjs", "acme-tailwind", "acme-react-native"], "category": "entity"},
    {"query": "What is the status of the SOC2 certification?", "expected": ["acme-soc2-budget", "acme-soc2-signed"], "category": "entity"},
    {"query": "Tell me about Redis usage and plans", "expected": ["acme-redis-migration", "acme-race-condition"], "category": "entity"},
]


# ══════════════════════════════════════════════════════════════════════════════
# Universe 2: WIKI — Knowledge base facts
# ══════════════════════════════════════════════════════════════════════════════

WIKI_MEMORIES = [
    # Science
    {"content": "The speed of light in a vacuum is approximately 299,792,458 meters per second. This is a fundamental constant in physics denoted by 'c'.", "tag": "wiki-speed-of-light"},
    {"content": "DNA (deoxyribonucleic acid) is a double helix molecule that carries genetic instructions for the development and functioning of all known living organisms.", "tag": "wiki-dna"},
    {"content": "The human body contains approximately 37.2 trillion cells. Red blood cells are the most common type, numbering about 70% of all cells.", "tag": "wiki-human-cells"},
    {"content": "Black holes are regions of spacetime where gravity is so strong that nothing, not even light, can escape. They are formed when massive stars collapse.", "tag": "wiki-black-holes"},
    {"content": "Photosynthesis converts carbon dioxide and water into glucose and oxygen using sunlight. The chemical equation is 6CO2 + 6H2O → C6H12O6 + 6O2.", "tag": "wiki-photosynthesis"},
    {"content": "The periodic table has 118 confirmed elements. The most recently added are nihonium (113), moscovium (115), tennessine (117), and oganesson (118).", "tag": "wiki-periodic-table"},
    {"content": "Quantum entanglement is a phenomenon where two particles become correlated such that the quantum state of one instantly influences the other, regardless of distance.", "tag": "wiki-quantum-entanglement"},
    {"content": "The Milky Way galaxy contains between 100 and 400 billion stars. Our solar system orbits the galactic center at about 230 km/s.", "tag": "wiki-milky-way"},
    {"content": "The theory of general relativity, published by Albert Einstein in 1915, describes gravity as a curvature of spacetime caused by mass and energy.", "tag": "wiki-general-relativity"},
    {"content": "Water has a boiling point of 100°C at standard atmospheric pressure. It is the only common substance found naturally in all three states of matter on Earth.", "tag": "wiki-water-properties"},
    {"content": "CRISPR-Cas9 is a gene editing technology that allows scientists to modify DNA sequences. It was adapted from a bacterial immune defense mechanism.", "tag": "wiki-crispr"},
    {"content": "Mitochondria are organelles found in eukaryotic cells that generate ATP through oxidative phosphorylation. They have their own circular DNA.", "tag": "wiki-mitochondria"},
    # History
    {"content": "World War II lasted from 1939 to 1945 and involved most of the world's nations. It resulted in an estimated 70-85 million deaths.", "tag": "wiki-ww2"},
    {"content": "The Roman Empire at its greatest extent under Trajan (117 AD) spanned from Britain to Mesopotamia, covering 5 million square kilometers.", "tag": "wiki-roman-empire"},
    {"content": "The Apollo 11 mission landed the first humans on the Moon on July 20, 1969. Neil Armstrong and Buzz Aldrin walked on the lunar surface.", "tag": "wiki-apollo-11"},
    {"content": "The printing press was invented by Johannes Gutenberg around 1440. It revolutionized the spread of knowledge and is considered one of the most important inventions in history.", "tag": "wiki-printing-press"},
    {"content": "The French Revolution began in 1789 with the storming of the Bastille. It led to the end of the monarchy and the rise of Napoleon Bonaparte.", "tag": "wiki-french-revolution"},
    {"content": "The Industrial Revolution began in Britain in the late 18th century, transforming manufacturing from hand production to machine processes.", "tag": "wiki-industrial-revolution"},
    {"content": "The Berlin Wall fell on November 9, 1989, leading to German reunification in 1990. It had divided East and West Berlin for 28 years.", "tag": "wiki-berlin-wall"},
    {"content": "Alexander the Great created one of the largest empires by age 30, stretching from Greece to northwestern India. He never lost a battle.", "tag": "wiki-alexander"},
    {"content": "The Renaissance was a cultural movement from the 14th to 17th century, beginning in Italy. Key figures include Leonardo da Vinci, Michelangelo, and Raphael.", "tag": "wiki-renaissance"},
    {"content": "The Magna Carta was signed in 1215 by King John of England. It established that the king was subject to the law and is a foundational document for constitutional governance.", "tag": "wiki-magna-carta"},
    # Technology
    {"content": "Python was created by Guido van Rossum and first released in 1991. It emphasizes code readability and supports multiple programming paradigms.", "tag": "wiki-python"},
    {"content": "The TCP/IP protocol suite is the foundation of the internet. TCP handles reliable data delivery while IP handles addressing and routing.", "tag": "wiki-tcp-ip"},
    {"content": "Machine learning is a subset of artificial intelligence where systems learn from data without being explicitly programmed. Key approaches: supervised, unsupervised, reinforcement.", "tag": "wiki-machine-learning"},
    {"content": "Git was created by Linus Torvalds in 2005 for Linux kernel development. It is a distributed version control system used by 90%+ of developers.", "tag": "wiki-git"},
    {"content": "SQL (Structured Query Language) was developed at IBM in the 1970s. It remains the standard language for relational database management.", "tag": "wiki-sql"},
    {"content": "The transformer architecture, introduced in 'Attention Is All You Need' (2017), revolutionized NLP. It uses self-attention mechanisms instead of recurrence.", "tag": "wiki-transformers"},
    {"content": "Kubernetes was originally designed by Google and released in 2014. It automates container orchestration, scaling, and management.", "tag": "wiki-kubernetes"},
    {"content": "PostgreSQL is an open-source relational database first released in 1996. It supports JSON, full-text search, and custom extensions like pgvector.", "tag": "wiki-postgresql"},
    {"content": "The World Wide Web was invented by Tim Berners-Lee at CERN in 1989. The first website went live on August 6, 1991.", "tag": "wiki-www"},
    {"content": "Moore's Law observes that the number of transistors on a microchip doubles approximately every two years. It was formulated by Gordon Moore in 1965.", "tag": "wiki-moores-law"},
    {"content": "RSA encryption is an asymmetric cryptographic algorithm published in 1977. It relies on the difficulty of factoring the product of two large prime numbers.", "tag": "wiki-rsa"},
    {"content": "Docker containers package applications with their dependencies into standardized units. Docker was released in 2013 and popularized containerization.", "tag": "wiki-docker"},
    # Geography
    {"content": "The Amazon River is the largest river by discharge volume, flowing through South America for 6,400 km. The Amazon rainforest produces 20% of world's oxygen.", "tag": "wiki-amazon-river"},
    {"content": "Mount Everest is the tallest mountain above sea level at 8,849 meters. It sits on the border of Nepal and Tibet in the Himalayas.", "tag": "wiki-everest"},
    {"content": "The Sahara Desert is the largest hot desert, covering 9.2 million km² across North Africa. Temperatures can exceed 50°C.", "tag": "wiki-sahara"},
    {"content": "Japan consists of 6,852 islands with a population of 125 million. Tokyo is the most populous metropolitan area in the world with 37 million people.", "tag": "wiki-japan"},
    {"content": "The Pacific Ocean is the largest and deepest ocean, covering 165.25 million km². The Mariana Trench at 10,994 meters is its deepest point.", "tag": "wiki-pacific-ocean"},
    {"content": "The Great Barrier Reef off Australia's coast is the world's largest coral reef system, stretching 2,300 km. It's visible from space.", "tag": "wiki-great-barrier-reef"},
    {"content": "Iceland sits on the Mid-Atlantic Ridge where the Eurasian and North American tectonic plates meet. It has 130 active and inactive volcanoes.", "tag": "wiki-iceland"},
    {"content": "The Nile River at 6,650 km is traditionally considered the longest river. It flows through 11 countries in northeastern Africa.", "tag": "wiki-nile"},
    # Culture
    {"content": "The Mona Lisa was painted by Leonardo da Vinci between 1503-1519. It hangs in the Louvre Museum in Paris and attracts 10 million visitors annually.", "tag": "wiki-mona-lisa"},
    {"content": "Beethoven's Symphony No. 9 was completed in 1824 while he was almost completely deaf. Its 'Ode to Joy' melody became the European anthem.", "tag": "wiki-beethoven-9th"},
    {"content": "Shakespeare wrote approximately 39 plays, 154 sonnets, and several longer poems. His works have been translated into every major language.", "tag": "wiki-shakespeare"},
    {"content": "The Olympic Games originated in ancient Greece around 776 BC. The modern Olympics were revived in 1896 in Athens by Pierre de Coubertin.", "tag": "wiki-olympics"},
    {"content": "Stoicism is a school of philosophy founded in Athens by Zeno of Citium around 300 BC. Core tenet: virtue is the highest good, achieved through reason.", "tag": "wiki-stoicism"},
    {"content": "The novel 'One Hundred Years of Solitude' by Gabriel García Márquez (1967) is a landmark of magical realism. It has sold over 50 million copies.", "tag": "wiki-marquez"},
    {"content": "Jazz originated in New Orleans in the late 19th century, blending African rhythms, blues, and European harmony. Key pioneers: Louis Armstrong, Duke Ellington.", "tag": "wiki-jazz"},
    {"content": "The Rosetta Stone, discovered in 1799, contains a decree in three scripts: hieroglyphic, Demotic, and Greek. It was key to deciphering Egyptian hieroglyphs.", "tag": "wiki-rosetta-stone"},
]

WIKI_QUERIES = [
    {"query": "How fast does light travel?", "expected": ["wiki-speed-of-light"], "category": "precision"},
    {"query": "What is DNA and how does it work?", "expected": ["wiki-dna", "wiki-crispr"], "category": "precision"},
    {"query": "When did humans first land on the moon?", "expected": ["wiki-apollo-11"], "category": "precision"},
    {"query": "What programming language did Guido van Rossum create?", "expected": ["wiki-python"], "category": "precision"},
    {"query": "What is the tallest mountain in the world?", "expected": ["wiki-everest"], "category": "precision"},
    {"query": "Tell me about quantum physics concepts", "expected": ["wiki-quantum-entanglement", "wiki-general-relativity"], "category": "recall"},
    {"query": "Major events in European history", "expected": ["wiki-french-revolution", "wiki-berlin-wall", "wiki-renaissance"], "category": "recall"},
    {"query": "Database technologies and SQL", "expected": ["wiki-sql", "wiki-postgresql"], "category": "recall"},
    {"query": "Rivers and bodies of water around the world", "expected": ["wiki-amazon-river", "wiki-nile"], "category": "recall"},
    {"query": "Famous artworks and cultural landmarks", "expected": ["wiki-mona-lisa", "wiki-beethoven-9th"], "category": "recall"},
    {"query": "Container and cloud orchestration technologies", "expected": ["wiki-kubernetes", "wiki-docker"], "category": "precision"},
    {"query": "Cryptography and internet security", "expected": ["wiki-rsa", "wiki-tcp-ip"], "category": "precision"},
    {"query": "What is machine learning and modern AI?", "expected": ["wiki-machine-learning", "wiki-transformers"], "category": "precision"},
    {"query": "Ancient civilizations and empires", "expected": ["wiki-roman-empire", "wiki-alexander"], "category": "recall"},
    {"query": "Cell biology and genetics", "expected": ["wiki-dna", "wiki-human-cells", "wiki-mitochondria"], "category": "recall"},
]


# ══════════════════════════════════════════════════════════════════════════════
# Search Profiles
# ══════════════════════════════════════════════════════════════════════════════

PROFILES = [
    {"name": "default", "params": {"top_k": 3, "min_similarity": 0.4, "fts_weight": 0.3, "similarity_blend": 0.75, "freshness_floor": 0.7, "freshness_decay_days": 90, "recall_boost_cap": 1.5, "recall_decay_window_days": 90, "graph_max_hops": 2}},
    {"name": "precision", "params": {"top_k": 3, "min_similarity": 0.6, "similarity_blend": 0.85, "graph_max_hops": 1}},
    {"name": "wide-net", "params": {"top_k": 15, "min_similarity": 0.2, "graph_max_hops": 2, "freshness_floor": 0.5}},
    {"name": "keyword-heavy", "params": {"top_k": 5, "min_similarity": 0.4, "fts_weight": 0.7, "graph_max_hops": 1}},
    {"name": "semantic-pure", "params": {"top_k": 5, "min_similarity": 0.4, "fts_weight": 0.0, "graph_max_hops": 0, "similarity_blend": 0.9}},
    {"name": "graph-deep", "params": {"top_k": 10, "min_similarity": 0.3, "graph_max_hops": 3}},
    {"name": "fresh-only", "params": {"top_k": 5, "min_similarity": 0.4, "freshness_floor": 0.95}},
    {"name": "legacy-friendly", "params": {"top_k": 5, "min_similarity": 0.3, "freshness_floor": 0.3}},
    {"name": "balanced-10", "params": {"top_k": 10, "min_similarity": 0.35, "fts_weight": 0.4, "similarity_blend": 0.7, "graph_max_hops": 2}},
    {"name": "weight-heavy", "params": {"top_k": 5, "min_similarity": 0.3, "similarity_blend": 0.5}},
    # Grid variants — varying one param at a time
    {"name": "top_k-1", "params": {"top_k": 1}},
    {"name": "top_k-5", "params": {"top_k": 5}},
    {"name": "top_k-10", "params": {"top_k": 10}},
    {"name": "top_k-20", "params": {"top_k": 20}},
    {"name": "min_sim-0.2", "params": {"min_similarity": 0.2}},
    {"name": "min_sim-0.3", "params": {"min_similarity": 0.3}},
    {"name": "min_sim-0.5", "params": {"min_similarity": 0.5}},
    {"name": "min_sim-0.7", "params": {"min_similarity": 0.7}},
    {"name": "fts-0.0", "params": {"fts_weight": 0.0}},
    {"name": "fts-0.5", "params": {"fts_weight": 0.5}},
    {"name": "fts-0.8", "params": {"fts_weight": 0.8}},
    {"name": "hops-0", "params": {"graph_max_hops": 0}},
    {"name": "hops-1", "params": {"graph_max_hops": 1}},
    {"name": "hops-3", "params": {"graph_max_hops": 3}},
    {"name": "blend-0.5", "params": {"similarity_blend": 0.5}},
    {"name": "blend-0.9", "params": {"similarity_blend": 0.9}},
    {"name": "floor-0.3", "params": {"freshness_floor": 0.3}},
    {"name": "floor-0.9", "params": {"freshness_floor": 0.9}},
    {"name": "recall-boost-1.0", "params": {"recall_boost_cap": 1.0}},
    {"name": "recall-boost-3.0", "params": {"recall_boost_cap": 3.0}},
]


# ══════════════════════════════════════════════════════════════════════════════
# Phases
# ══════════════════════════════════════════════════════════════════════════════


def get_universe(name: str):
    """Return (memories, queries) for a universe."""
    if name == "acme":
        return ACME_MEMORIES, ACME_QUERIES
    elif name == "wiki":
        # Wiki memories need default fields
        mems = []
        for m in WIKI_MEMORIES:
            mems.append({**m, "fleet_id": "knowledge", "agent_id": "curator", "memory_type": "fact", "weight": 0.5})
        return mems, WIKI_QUERIES
    elif name == "mixed":
        wiki_mems = [
            {**m, "fleet_id": "knowledge", "agent_id": "curator", "memory_type": "fact", "weight": 0.5}
            for m in WIKI_MEMORIES
        ]
        return ACME_MEMORIES + wiki_mems, ACME_QUERIES + WIKI_QUERIES
    else:
        raise ValueError(f"Unknown universe: {name}")


def seed_memories(base_url: str, api_key: str, tenant: str, memories: list[dict]) -> dict[str, str]:
    """Bulk-write memories via REST. Returns {tag: memory_id} mapping."""
    client = httpx.Client(timeout=TIMEOUT, headers={"X-API-Key": api_key})
    api = f"{base_url.rstrip('/')}/api"
    tag_to_id: dict[str, str] = {}
    total = len(memories)

    print(f"\n  Seeding {total} memories into tenant '{tenant}'...")

    for batch_start in range(0, total, BULK_BATCH_SIZE):
        batch = memories[batch_start:batch_start + BULK_BATCH_SIZE]
        items = []
        batch_tags = []
        bulk_exclude = {"fleet_id", "agent_id", "tag", "visibility"}
        for m in batch:
            tag = m.get("tag")
            batch_tags.append(tag)
            item = {k: v for k, v in m.items() if k not in bulk_exclude}
            items.append(item)

        # Use first item's fleet_id/agent_id for the batch (bulk endpoint uses shared values)
        # Since our batches may have mixed fleets, write individually for mixed batches
        fleets = set(m.get("fleet_id") for m in batch)
        agents = set(m.get("agent_id") for m in batch)

        if len(fleets) == 1 and len(agents) == 1:
            # Uniform batch — use bulk endpoint
            body = {
                "tenant_id": tenant,
                "fleet_id": batch[0].get("fleet_id"),
                "agent_id": batch[0].get("agent_id"),
                "items": items,
            }
            r = client.post(f"{api}/memories/bulk", json=body)
            if r.status_code == 200:
                data = r.json()
                for i, result in enumerate(data.get("results", [])):
                    mid = result.get("id") or result.get("duplicate_of")
                    if mid and batch_tags[i]:
                        tag_to_id[batch_tags[i]] = mid
            else:
                print(f"    Bulk write failed: {r.status_code} {r.text[:200]}")
        else:
            # Mixed batch — write individually
            for i, m in enumerate(batch):
                body = {"tenant_id": tenant, **{k: v for k, v in m.items() if k != "tag"}}
                r = client.post(f"{api}/memories", json=body)
                if r.status_code in (200, 201):
                    mid = r.json().get("id")
                    if mid and batch_tags[i]:
                        tag_to_id[batch_tags[i]] = mid
                elif r.status_code == 409:
                    # Extract existing memory ID from duplicate response
                    dup_detail = r.json().get("detail", "")
                    dup_match = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", dup_detail)
                    if dup_match and batch_tags[i]:
                        tag_to_id[batch_tags[i]] = dup_match.group(0)
                else:
                    print(f"    Write failed: {r.status_code}")

        done = min(batch_start + BULK_BATCH_SIZE, total)
        print(f"    {done}/{total} seeded ({len(tag_to_id)} tagged)")

    print(f"  Seed complete: {len(tag_to_id)} tagged memories\n")
    return tag_to_id


def run_benchmark(mcp: MCPClient, agent_id: str, queries: list[dict], tag_to_id: dict[str, str]) -> list[dict]:
    """Run all queries, score results against ground truth."""
    results = []
    for q in queries:
        expected_ids = set()
        for tag in q["expected"]:
            if tag in tag_to_id:
                expected_ids.add(tag_to_id[tag])

        t0 = time.perf_counter()
        memories = mcp.search(q["query"], agent_id=agent_id)
        latency_ms = round((time.perf_counter() - t0) * 1000)

        found_ids = set(m.get("id", "") for m in memories)
        hits = found_ids & expected_ids

        recall = len(hits) / len(expected_ids) if expected_ids else 0
        precision = len(hits) / len(found_ids) if found_ids else 0

        results.append({
            "query": q["query"],
            "category": q["category"],
            "expected_count": len(expected_ids),
            "found_count": len(found_ids),
            "hits": len(hits),
            "recall": round(recall, 3),
            "precision": round(precision, 3),
            "latency_ms": latency_ms,
        })
    return results


def explore_profiles(mcp: MCPClient, agent_id: str, profiles: list[dict],
                     queries: list[dict], tag_to_id: dict[str, str],
                     rest_client: httpx.Client, api_url: str, tenant: str) -> list[dict]:
    """Try each profile, benchmark, collect scores."""
    all_results = []
    total = len(profiles)

    for i, profile in enumerate(profiles):
        name = profile["name"]
        params = profile["params"]

        # Reset profile first
        rest_client.patch(
            f"{api_url}/agents/{agent_id}/tune",
            params={"tenant_id": tenant, "reset": "true"},
            json={},
        )

        # Apply new profile (skip for default)
        if params:
            mcp.tune(agent_id, **params)

        # Run benchmark
        query_results = run_benchmark(mcp, agent_id, queries, tag_to_id)

        # Aggregate scores
        recalls = [r["recall"] for r in query_results]
        precisions = [r["precision"] for r in query_results]
        latencies = [r["latency_ms"] for r in query_results]

        avg_recall = statistics.mean(recalls) if recalls else 0
        avg_precision = statistics.mean(precisions) if precisions else 0
        avg_latency = statistics.mean(latencies) if latencies else 0
        quality = 0.6 * avg_recall + 0.4 * avg_precision

        # Per-category scores
        category_scores = {}
        for cat in set(r["category"] for r in query_results):
            cat_results = [r for r in query_results if r["category"] == cat]
            cat_recall = statistics.mean(r["recall"] for r in cat_results)
            cat_precision = statistics.mean(r["precision"] for r in cat_results)
            category_scores[cat] = {
                "recall": round(cat_recall, 3),
                "precision": round(cat_precision, 3),
                "quality": round(0.6 * cat_recall + 0.4 * cat_precision, 3),
            }

        result = {
            "profile": name,
            "params": params,
            "avg_recall": round(avg_recall, 3),
            "avg_precision": round(avg_precision, 3),
            "quality": round(quality, 3),
            "avg_latency_ms": round(avg_latency),
            "category_scores": category_scores,
            "per_query": query_results,
        }
        all_results.append(result)

        print(f"    [{i+1}/{total}] {name:20s}  quality={quality:.3f}  recall={avg_recall:.3f}  precision={avg_precision:.3f}  avg={avg_latency:.0f}ms")

    return all_results


# ══════════════════════════════════════════════════════════════════════════════
# Report Generator
# ══════════════════════════════════════════════════════════════════════════════


def generate_report(results: list[dict], universe: str, tenant: str,
                    memory_count: int, query_count: int, output_dir: str):
    """Generate markdown + JSON reports."""
    os.makedirs(output_dir, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    base = f"hyperagent-{universe}-{date_str}"

    # Sort by quality descending
    ranked = sorted(results, key=lambda r: (r["quality"], -r["avg_latency_ms"]), reverse=True)

    # ── Markdown ──
    lines = [
        f"# HyperAgent Benchmark Report",
        f"",
        f"**Universe:** {universe} | **Tenant:** {tenant}",
        f"**Memories:** {memory_count} | **Queries:** {query_count} | **Profiles:** {len(results)}",
        f"**Date:** {date_str}",
        f"",
        f"## Leaderboard",
        f"",
        f"| # | Profile | Quality | Recall | Precision | Avg ms | Key Settings |",
        f"|---|---------|---------|--------|-----------|--------|-------------|",
    ]
    for i, r in enumerate(ranked):
        params_str = ", ".join(f"{k}={v}" for k, v in r["params"].items()) if r["params"] else "(defaults)"
        lines.append(
            f"| {i+1} | **{r['profile']}** | {r['quality']:.3f} | {r['avg_recall']:.3f} | "
            f"{r['avg_precision']:.3f} | {r['avg_latency_ms']}ms | {params_str} |"
        )

    # Best by category
    categories = set()
    for r in results:
        categories.update(r["category_scores"].keys())

    lines += [
        "", "## Best Profile by Category", "",
        "| Category | Winner | Quality | Recall | Precision |",
        "|----------|--------|---------|--------|-----------|",
    ]
    for cat in sorted(categories):
        best = max(results, key=lambda r: r["category_scores"].get(cat, {}).get("quality", 0))
        cs = best["category_scores"].get(cat, {})
        lines.append(f"| {cat} | **{best['profile']}** | {cs.get('quality', 0):.3f} | {cs.get('recall', 0):.3f} | {cs.get('precision', 0):.3f} |")

    # Top 3 detailed
    lines += ["", "## Top 3 — Per Query Breakdown", ""]
    for i, r in enumerate(ranked[:3]):
        lines.append(f"### #{i+1} — {r['profile']}")
        lines.append(f"")
        lines.append(f"| Query | Category | Recall | Precision | Hits/Expected | ms |")
        lines.append(f"|-------|----------|--------|-----------|--------------|-----|")
        for q in r["per_query"]:
            lines.append(
                f"| {q['query'][:60]}{'...' if len(q['query']) > 60 else ''} | {q['category']} | "
                f"{q['recall']:.2f} | {q['precision']:.2f} | {q['hits']}/{q['expected_count']} | {q['latency_ms']} |"
            )
        lines.append("")

    md_path = os.path.join(output_dir, f"{base}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    # ── JSON ──
    json_path = os.path.join(output_dir, f"{base}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({
            "universe": universe,
            "tenant": tenant,
            "date": date_str,
            "memory_count": memory_count,
            "query_count": query_count,
            "profiles_tested": len(results),
            "leaderboard": [
                {"rank": i+1, "profile": r["profile"], "quality": r["quality"],
                 "recall": r["avg_recall"], "precision": r["avg_precision"],
                 "avg_latency_ms": r["avg_latency_ms"], "params": r["params"]}
                for i, r in enumerate(ranked)
            ],
            "full_results": ranked,
        }, f, indent=2, default=str)

    print(f"\n  Reports saved:")
    print(f"    {md_path}")
    print(f"    {json_path}")

    return md_path


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════


def main():
    parser = argparse.ArgumentParser(description="HyperAgent Search Benchmark")
    parser.add_argument("--universe", required=True, choices=["acme", "wiki", "mixed"],
                        help="Test universe to use")
    parser.add_argument("--api-key", required=True, help="MemClaw API key")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Base URL (default: {DEFAULT_URL})")
    parser.add_argument("--profiles", type=int, default=len(PROFILES),
                        help=f"Number of profiles to test (default: {len(PROFILES)})")
    parser.add_argument("--skip-seed", action="store_true", help="Skip seeding (reuse existing tenant)")
    parser.add_argument("--tenant", default=None, help="Custom tenant ID (default: auto-generated)")
    parser.add_argument("--output", default="reports", help="Output directory for reports")
    args = parser.parse_args()

    date_str = datetime.now().strftime("%Y%m%d")
    tenant = args.tenant or f"hyperagent-{args.universe}-{date_str}"
    memories, queries = get_universe(args.universe)
    profiles = PROFILES[:args.profiles]

    print(f"\n{'='*70}")
    print(f"  HyperAgent Search Benchmark")
    print(f"  Universe: {args.universe} | Tenant: {tenant}")
    print(f"  Memories: {len(memories)} | Queries: {len(queries)} | Profiles: {len(profiles)}")
    print(f"  URL: {args.url}")
    print(f"{'='*70}")

    api_url = f"{args.url.rstrip('/')}/api"
    admin_headers = {"X-API-Key": args.api_key}

    # Phase 1: SEED (REST bulk with admin key)
    tag_to_id = {}
    if not args.skip_seed:
        import copy
        seed_mems = copy.deepcopy(memories)
        tag_to_id = seed_memories(args.url, args.api_key, tenant, seed_mems)
        if not tag_to_id:
            print("  ERROR: No memories seeded. Check API key and server.")
            sys.exit(1)
        print("  Waiting 3s for embeddings to settle...")
        time.sleep(3)
    else:
        print("\n  Skipping seed (--skip-seed). Queries will score against found results only.")

    # Provision a tenant-scoped API key for MCP (MCP rejects admin keys)
    print("\n  Provisioning tenant-scoped API key for MCP...")
    r = httpx.post(f"{api_url}/admin/keys",
                   json={"tenant_id": tenant, "label": "hyperagent-bench"},
                   headers=admin_headers, timeout=TIMEOUT)
    if r.status_code != 200:
        print(f"  ERROR: Failed to create tenant key: {r.status_code} {r.text[:200]}")
        sys.exit(1)
    tenant_key = r.json().get("raw_key")
    print(f"  Tenant key created: {tenant_key[:12]}...")

    # Phase 2+3: BASELINE + EXPLORE (MCP with tenant key)
    print("\n  Initializing MCP session...")
    mcp = MCPClient(args.url, tenant_key)
    mcp.initialize()
    print(f"  MCP session established (session_id={mcp.session_id or 'stateless'}).\n")

    # Register benchmark agent (auto-created on first write)
    print(f"  Registering benchmark agent '{BENCHMARK_AGENT}'...")
    rest_admin = httpx.Client(timeout=TIMEOUT, headers=admin_headers)
    rest_admin.post(f"{api_url}/memories", json={
        "tenant_id": tenant, "agent_id": BENCHMARK_AGENT,
        "content": f"HyperAgent benchmark initialized at {datetime.now(timezone.utc).isoformat()}",
        "memory_type": "fact", "weight": 0.1,
    })

    # REST client for profile reset (uses admin key)
    rest = rest_admin

    print(f"  Exploring {len(profiles)} profiles × {len(queries)} queries = {len(profiles) * len(queries)} searches\n")
    results = explore_profiles(mcp, BENCHMARK_AGENT, profiles, queries, tag_to_id, rest, api_url, tenant)

    # Phase 4: REPORT
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}")

    ranked = sorted(results, key=lambda r: (r["quality"], -r["avg_latency_ms"]), reverse=True)
    print(f"\n  Top 5:")
    for i, r in enumerate(ranked[:5]):
        params_str = ", ".join(f"{k}={v}" for k, v in r["params"].items()) if r["params"] else "(defaults)"
        print(f"    #{i+1}  {r['profile']:20s}  quality={r['quality']:.3f}  recall={r['avg_recall']:.3f}  "
              f"precision={r['avg_precision']:.3f}  {r['avg_latency_ms']}ms  [{params_str}]")

    generate_report(results, args.universe, tenant, len(memories), len(queries), args.output)

    print(f"\n  View memories in Prism: {args.url}/ui/prism.html (select tenant: {tenant})")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
