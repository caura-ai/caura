#!/usr/bin/env python3
"""End-to-end smoke for every MCP tool against a live enterprise stack.

Complements trust_matrix_e2e.py: that one proves trust gating, this one
proves each plugin-exposed tool actually performs its advertised
operation with realistic arguments. Reports per-tool PASS / FAIL / SKIP
with a one-line reason each.

Expects /tmp/e2e.env (written by the E2E register step) containing::

    TENANT_ID=...
    KEY=...
    JWT=...
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def load_env() -> dict[str, str]:
    path = Path("/tmp/e2e.env")
    if not path.exists():
        print("ERROR: /tmp/e2e.env missing — register a tenant + API key first.")
        sys.exit(2)
    return dict(
        line.strip().split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line
    )


ENV = load_env()
TENANT = ENV["TENANT_ID"]
KEY = ENV["KEY"]
FLEET = "smoke-fleet"

GATEWAY = "http://localhost"  # nginx
CORE_API = "http://localhost:8000"  # direct core-api (MCP lives here)


def http(method: str, url: str, body=None, headers=None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        h.update(headers)
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body else None,
        headers=h,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = r.read().decode() or "{}"
            try:
                return r.status, json.loads(text)
            except json.JSONDecodeError:
                return r.status, {"_raw": text[:200]}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def mcp_call(tool: str, args: dict):
    """Invoke an MCP tool and normalize the response shape."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {"tenant_id": TENANT, **args},
        },
    }
    code, resp = http(
        "POST",
        f"{CORE_API}/mcp/",
        body=body,
        headers={"X-Tenant-ID": TENANT},
    )
    if code != 200:
        return "NETWORK", f"HTTP {code}: {resp}"
    err = resp.get("error")
    if err:
        return "JSONRPC_ERR", err.get("message", str(err))
    result = resp.get("result", {})
    content = result.get("content") or []
    text = content[0].get("text", "") if content else ""
    is_error = result.get("isError") or text.lstrip().startswith("Error (")
    return ("TOOL_ERR" if is_error else "OK"), text


def seed_fleet_and_memories() -> dict:
    """Seed data the tools can actually operate on."""
    agent = "smoke-main"
    # Seed memory via gateway write (auto-provisions the agent).
    for content in [
        "We run PostgreSQL 16 with pgvector in production.",
        "Redis 7 is used for session caching.",
        "The team ships a release every two weeks on Thursdays.",
    ]:
        http(
            "POST",
            f"{GATEWAY}/api/memories",
            body={
                "tenant_id": TENANT,
                "content": content,
                "agent_id": agent,
                "fleet_id": FLEET,
                "memory_type": "fact",
            },
            headers={"X-API-Key": KEY},
        )
    # Promote agent to trust=3 so every tool is exercised at max privilege.
    http(
        "PATCH",
        f"{CORE_API}/api/v1/agents/{agent}/trust?tenant_id={TENANT}",
        body={"trust_level": 3},
        headers={"X-Tenant-ID": TENANT},
    )
    # Seed one entity so memclaw_entity_get has something to find.
    _, ent = http(
        "POST",
        f"{CORE_API}/api/v1/entities/upsert",
        body={
            "tenant_id": TENANT,
            "name": "PostgreSQL",
            "entity_type": "technology",
        },
        headers={"X-Tenant-ID": TENANT},
    )
    entity_id = ent.get("id") or ent.get("entity_id") or ""
    # Seed one doc so memclaw_doc read/query hits data.
    http(
        "POST",
        f"{CORE_API}/api/v1/documents",
        body={
            "tenant_id": TENANT,
            "collection": "smoke-docs",
            "doc_id": "hello",
            "data": {"greeting": "hi", "lang": "en"},
            "agent_id": agent,
        },
        headers={"X-Tenant-ID": TENANT},
    )
    # Capture a seeded memory_id for memclaw_manage.
    _, listing = http(
        "GET",
        f"{CORE_API}/api/v1/memories?tenant_id={TENANT}&agent_id={agent}&limit=1",
        headers={"X-Tenant-ID": TENANT},
    )
    items = listing.get("items") or []
    memory_id = items[0]["id"] if items else ""
    return {"agent": agent, "entity_id": entity_id, "memory_id": memory_id}


def main():
    print(f"Tenant: {TENANT}")
    print(f"Fleet:  {FLEET}")
    print()
    print("Seeding fleet, memories, agent (trust=3), entity, document...")
    ctx = seed_fleet_and_memories()
    agent = ctx["agent"]
    # Let enrichment + entity extraction settle a bit.
    time.sleep(4)

    tests = [
        (
            "memclaw_write",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "content": "Go 1.22 is the minimum runtime version.",
                "memory_type": "fact",
            },
        ),
        (
            "memclaw_recall",
            {"agent_id": agent, "fleet_id": FLEET, "query": "what database do we use"},
        ),
        (
            "memclaw_list",
            {"agent_id": agent, "fleet_id": FLEET, "scope": "agent", "limit": 10},
        ),
        (
            "memclaw_manage",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "op": "read",
                "memory_id": ctx["memory_id"],
            },
        ),
        (
            "memclaw_doc",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "op": "read",
                "collection": "smoke-docs",
                "doc_id": "hello",
            },
        ),
        (
            "memclaw_entity_get",
            {"agent_id": agent, "fleet_id": FLEET, "entity_id": ctx["entity_id"]},
        ),
        (
            "memclaw_tune",
            {"agent_id": agent, "fleet_id": FLEET, "op": "get"},
        ),
        (
            "memclaw_insights",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "focus": "patterns",
                "scope": "agent",
            },
        ),
        (
            "memclaw_evolve",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "outcome": "Smoke test confirmed the tool flow is reachable.",
                "outcome_type": "success",
            },
        ),
        (
            "memclaw_stats",
            {"agent_id": agent, "fleet_id": FLEET, "scope": "agent"},
        ),
        (
            "memclaw_share_skill",
            {
                "agent_id": agent,
                "name": "smoke-skill",
                "description": "Smoke-test skill — auto-published, not installed.",
                "content": "# smoke-skill\n\nProbe content.\n",
                "target_fleet_id": FLEET,
            },
        ),
        (
            "memclaw_unshare_skill",
            {"agent_id": agent, "name": "smoke-skill"},
        ),
    ]

    width = max(len(t[0]) for t in tests) + 2
    passed = failed = 0
    for tool, args in tests:
        verdict, detail = mcp_call(tool, args)
        ok = verdict == "OK"
        passed += int(ok)
        failed += int(not ok)
        snippet = detail.replace("\n", " ")[:110]
        print(
            f"  {'PASS' if ok else 'FAIL':4s}  {tool:<{width}} {verdict:<12} {snippet}"
        )

    print()
    print(f"Summary: {passed}/{len(tests)} tools passed, {failed} failed.")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
