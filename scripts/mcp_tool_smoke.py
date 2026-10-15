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

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ENV_FILE = Path("/tmp/e2e.env")
DEFAULT_CORE_API = (
    "http://localhost:8000"  # direct core-api (MCP lives here by default)
)


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        print(f"ERROR: {path} missing — register a tenant + API key first.")
        sys.exit(2)
    return dict(
        line.strip().split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line
    )


FLEET = "smoke-fleet"


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
    except urllib.error.URLError as e:
        # Nothing answered (refused, unresolvable, timed out). Status 0, so the
        # caller reports it like any other failure instead of a traceback
        # (L-61). ``HTTPError`` is a ``URLError``, so it is caught first.
        return 0, {"error": str(e.reason)}


def _seeded(what: str, status: int, body) -> None:
    """Stop the run when a seed did not land.

    Every tool case below reads what seeding wrote, so a failed seed would
    surface as a tool failure with the wrong cause, or a case run with an empty
    id (L-61).
    """
    if not 200 <= status < 300:
        sys.exit(f"ERROR: seeding {what} failed: HTTP {status} {body}")


def _already_seeded(status: int, body) -> bool:
    """A re-run against the same tenant writes the same seed content, which
    core-api answers 409 DUPLICATE_MEMORY. The memory is there, which is all
    seeding needs; any other 409 still stops the run."""
    error = body.get("error") if isinstance(body, dict) else None
    return (
        status == 409
        and isinstance(error, dict)
        and error.get("code") == "DUPLICATE_MEMORY"
    )


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
    """Seed data the tools can actually operate on, on the stack under test.

    Every seed goes to ``--url``. The memories went to a hardcoded
    ``http://localhost`` gateway, so any other target was seeded somewhere else,
    or not at all when nothing listened there (L-61).
    """
    agent = "smoke-main"
    # The write auto-provisions the agent.
    for content in [
        "We run PostgreSQL 16 with pgvector in production.",
        "Redis 7 is used for session caching.",
        "The team ships a release every two weeks on Thursdays.",
    ]:
        status, resp = http(
            "POST",
            f"{CORE_API}/api/v1/memories",
            body={
                "tenant_id": TENANT,
                "content": content,
                "agent_id": agent,
                "fleet_id": FLEET,
                "memory_type": "fact",
            },
            headers={"X-Tenant-ID": TENANT},
        )
        if not _already_seeded(status, resp):
            _seeded("a memory", status, resp)
    # Promote agent to trust=3 so every tool is exercised at max privilege.
    status, resp = http(
        "PATCH",
        f"{CORE_API}/api/v1/agents/{agent}/trust?tenant_id={TENANT}",
        body={"trust_level": 3},
        headers={"X-Tenant-ID": TENANT},
    )
    _seeded("the agent's trust", status, resp)
    # Seed one entity so caura_entity_get has something to find.
    status, ent = http(
        "POST",
        f"{CORE_API}/api/v1/entities/upsert",
        body={
            "tenant_id": TENANT,
            "name": "PostgreSQL",
            "entity_type": "technology",
        },
        headers={"X-Tenant-ID": TENANT},
    )
    _seeded("an entity", status, ent)
    entity_id = ent.get("id") or ent.get("entity_id") or ""
    # Seed one doc so caura_doc read/query hits data.
    status, resp = http(
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
    _seeded("a document", status, resp)
    # Capture a seeded memory_id for caura_manage.
    status, listing = http(
        "GET",
        f"{CORE_API}/api/v1/memories?tenant_id={TENANT}&agent_id={agent}&limit=1",
        headers={"X-Tenant-ID": TENANT},
    )
    _seeded("the memory listing", status, listing)
    items = listing.get("items") or []
    if not items:
        sys.exit("ERROR: seeding wrote memories the listing does not return")
    memory_id = items[0]["id"]
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
            "caura_write",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "content": "Go 1.22 is the minimum runtime version.",
                "memory_type": "fact",
            },
        ),
        (
            "caura_recall",
            {"agent_id": agent, "fleet_id": FLEET, "query": "what database do we use"},
        ),
        (
            "caura_list",
            {"agent_id": agent, "fleet_id": FLEET, "scope": "agent", "limit": 10},
        ),
        (
            "caura_manage",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "op": "read",
                "memory_id": ctx["memory_id"],
            },
        ),
        (
            "caura_doc",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "op": "read",
                "collection": "smoke-docs",
                "doc_id": "hello",
            },
        ),
        (
            "caura_entity_get",
            {"agent_id": agent, "fleet_id": FLEET, "entity_id": ctx["entity_id"]},
        ),
        (
            "caura_tune",
            {"agent_id": agent, "fleet_id": FLEET, "op": "get"},
        ),
        (
            "caura_insights",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "focus": "patterns",
                "scope": "agent",
            },
        ),
        (
            "caura_evolve",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "outcome": "Smoke test confirmed the tool flow is reachable.",
                "outcome_type": "success",
            },
        ),
        (
            "caura_stats",
            {"agent_id": agent, "fleet_id": FLEET, "scope": "agent"},
        ),
        (
            # Skills now live in the generic ``skills`` document collection.
            # Upsert via caura_doc op=write, then delete via op=delete.
            "caura_doc",
            {
                "agent_id": agent,
                "fleet_id": FLEET,
                "op": "write",
                "collection": "skills",
                "doc_id": "smoke-skill",
                "data": {
                    "name": "smoke-skill",
                    "summary": "Smoke-test skill — published via caura_doc.",
                    "content": "# smoke-skill\n\nProbe content.\n",
                },
            },
        ),
        (
            "caura_doc",
            {
                "agent_id": agent,
                "op": "delete",
                "collection": "skills",
                "doc_id": "smoke-skill",
            },
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=DEFAULT_CORE_API,
        help=f"Direct core-api URL (default: {DEFAULT_CORE_API})",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help=f"Credentials environment file (default: {DEFAULT_ENV_FILE})",
    )
    args = parser.parse_args()

    CORE_API = args.url.rstrip("/")
    env = load_env(args.env_file)
    TENANT = env["TENANT_ID"]

    main()
