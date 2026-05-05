#!/usr/bin/env python3
"""Trust-level MCP matrix test.

Provisions 3 agents at trust 1/2/3, then invokes each MCP tool and asserts
expected-pass vs expected-deny per the declared trust gates.
"""

import json
import os
import sys
import urllib.request
import urllib.error


def load_env():
    env = {}
    with open("/tmp/e2e.env") as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            env[k] = v
    return env


ENV = load_env()
KEY = ENV["KEY"]
TENANT = ENV["TENANT_ID"]
FLEET = "trust-fleet"

GATEWAY = "http://localhost"  # port 80 (nginx) — MCP via auth_request
CORE_API = "http://localhost:8000"  # direct core-api — uses X-Tenant-ID header


def http(method, url, body=None, headers=None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if headers:
        h.update(headers)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def provision():
    print("=== Provisioning agents ===")
    for level in (1, 2, 3):
        aid = f"trust-{level}"
        # Seed memory (auto-provisions the agent at trust_level=0)
        s, body = http(
            "POST",
            f"{GATEWAY}/api/memories",
            body={
                "tenant_id": TENANT,
                "content": f"seed memory for {aid}",
                "agent_id": aid,
                "fleet_id": FLEET,
                "memory_type": "fact",
            },
            headers={"X-API-Key": KEY},
        )
        print(
            f"  {aid} seed: {s}  id={body.get('id', '-')[:12] if body.get('id') else body.get('detail', '')}"
        )
        # Promote trust
        s, body = http(
            "PATCH",
            f"{CORE_API}/api/v1/agents/{aid}/trust?tenant_id={TENANT}",
            body={"trust_level": level},
            headers={"X-Tenant-ID": TENANT},
        )
        print(f"    PATCH trust: {s} -> trust_level={body.get('trust_level')}")


def mcp_call(agent_id, tool, args=None):
    """Invoke an MCP tool via the core-api (direct) with X-Tenant-ID + agent context."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {
                "tenant_id": TENANT,
                "agent_id": agent_id,
                "fleet_id": FLEET,
                **(args or {}),
            },
        },
    }
    s, resp = http(
        "POST", f"{CORE_API}/mcp/", body=body, headers={"X-Tenant-ID": TENANT}
    )
    # MCP returns either result (success) or result.isError (tool-level error)
    result = resp.get("result", {})
    err = resp.get("error")
    if err:
        return "ERROR", err.get("message", str(err))
    text = ""
    content = result.get("content", [])
    if content and isinstance(content, list):
        text = content[0].get("text", "")
    # MCP tools sometimes emit "Error (403): ..." as text while leaving isError=false.
    # Treat either marker as a denial/error.
    if result.get("isError") or text.lstrip().startswith("Error ("):
        return "DENIED", text[:150]
    return "OK", text[:150]


# Tool call matrix — each (tool, args) and the minimum trust we expect for success.
# Any tool call at lower trust should return DENIED (or equivalent).
TESTS = [
    # tool, args (merged with tenant/agent/fleet), min_trust_to_succeed
    (
        "memclaw_write",
        {"content": "trust-matrix test memory", "memory_type": "fact"},
        1,
    ),
    ("memclaw_recall", {"query": "trust"}, 1),
    ("memclaw_list", {"scope": "agent"}, 1),
    ("memclaw_list", {"scope": "fleet"}, 2),
    ("memclaw_list", {"scope": "all"}, 2),
    ("memclaw_manage", {"op": "count"}, 1),
    ("memclaw_doc", {"op": "read", "collection": "probes", "doc_id": "none"}, 0),
    ("memclaw_entity_get", {"name": "nobody"}, 0),
    ("memclaw_tune", {"op": "get"}, 0),
    ("memclaw_insights", {"scope": "agent"}, 1),
    ("memclaw_insights", {"scope": "fleet"}, 2),
    ("memclaw_evolve", {"op": "analyze"}, 2),
    ("memclaw_stats", {"scope": "agent"}, 1),
    ("memclaw_stats", {"scope": "fleet"}, 2),
]


def run_matrix():
    print("\n=== Trust Matrix (10 tools × 3 trust levels) ===")
    print(f"{'tool':25s} {'args':40s} {'T1':10s} {'T2':10s} {'T3':10s}")
    print("-" * 100)
    for tool, args, min_trust in TESTS:
        row_results = {}
        for level in (1, 2, 3):
            aid = f"trust-{level}"
            status, detail = mcp_call(aid, tool, args)
            row_results[level] = (status, detail)
        args_repr = json.dumps(args)[:38]
        t1 = row_results[1][0]
        t2 = row_results[2][0]
        t3 = row_results[3][0]

        # Flag if result doesn't match expectation
        def ok(actual, level):
            if level >= min_trust:
                return "ok" if actual == "OK" else actual[:6]
            else:
                return (
                    "deny" if actual in ("DENIED", "ERROR") else f"PERM!"
                )  # unexpected allow

        flags = f"{ok(t1, 1):10s} {ok(t2, 2):10s} {ok(t3, 3):10s}"
        print(f"{tool:25s} {args_repr:40s} {flags}  min={min_trust}")


if __name__ == "__main__":
    provision()
    run_matrix()
