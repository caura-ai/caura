#!/usr/bin/env python3
"""Trust-level MCP matrix test — READ paths (recall / read / stats / list).

Drives core-api directly on :8000 with an ``X-API-Key`` (the tenant is resolved
from the key). Provisions fleeted agents at trust 1/2/3 plus a *fleet-less*
trust-1 agent, seeds memories in an OWN fleet and a FOREIGN fleet, then invokes
each read tool and asserts expected-pass vs expected-deny per the trust ladder:

    own-fleet read      → trust >= 1   (spec: L1 = read within your own fleet)
    cross-fleet read    → trust >= 2
    scope='all'         → trust >= 2   (spans fleets by definition)
    fleet-less caller, scope='fleet', no fleet_id → DENIED
        (the L2 fan-out guard: no home fleet ⇒ can't prove membership)

Reads ``KEY`` and ``TENANT_ID`` from ``/tmp/e2e.env``::

    KEY=mc_...
    TENANT_ID=default
"""

import json
import urllib.error
import urllib.request
import uuid

RUN = uuid.uuid4().hex[:8]  # nonce so re-runs don't hit write-dedup


def load_env():
    env = {}
    with open("/tmp/e2e.env") as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k:
                env[k] = v
    return env


ENV = load_env()
KEY = ENV["KEY"]
TENANT = ENV.get("TENANT_ID", "default")
OWN = "wt-fleet"  # the trust-1/2/3 agents' home fleet
OTHER = "wt-other"  # a foreign fleet (cross-fleet target)
CORE = "http://localhost:8000"  # direct core-api; X-API-Key resolves the tenant


def http(method, url, body=None):
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-API-Key": KEY,
    }
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


def seed(agent_id, fleet_id):
    """Seed one memory (auto-provisions the agent at trust 0). Returns its id."""
    body = {
        "tenant_id": TENANT,
        "content": f"seed memory for {agent_id} {RUN}",
        "agent_id": agent_id,
        "memory_type": "fact",
    }
    if fleet_id:
        body["fleet_id"] = fleet_id
    _, b = http("POST", f"{CORE}/api/v1/memories", body)
    return b.get("id")


def set_trust(agent_id, level):
    s, b = http(
        "PATCH",
        f"{CORE}/api/v1/agents/{agent_id}/trust?tenant_id={TENANT}",
        {"trust_level": level},
    )
    return s, b.get("trust_level"), b.get("fleet_id")


def mcp(agent_id, tool, args):
    """Invoke an MCP read tool; classify as OK / DENIED / ERROR."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": {"tenant_id": TENANT, "agent_id": agent_id, **args},
        },
    }
    _, resp = http("POST", f"{CORE}/mcp/", body)
    if resp.get("error"):
        return "ERROR", str(resp["error"])[:120]
    result = resp.get("result", {})
    content = result.get("content", [])
    text = content[0].get("text", "") if content else ""
    denied_markers = ('"code": "FORBIDDEN"', '"code": "NOT_FOUND"', "trust_level=")
    if (
        result.get("isError")
        or text.lstrip().startswith("Error (")
        or any(m in text for m in denied_markers)
    ):
        return "DENIED", text[:120]
    return "OK", text[:80]


def provision():
    print("=== Provisioning ===")
    ids = {}
    for aid, lvl in [("wt-t1", 1), ("wt-t2", 2), ("wt-t3", 3)]:
        ids[aid] = seed(aid, OWN)
        s, t, f = set_trust(aid, lvl)
        print(f"  {aid:14s} seed={str(ids[aid])[:12]}  trust={t} fleet={f}")
    # A memory owned by a different agent in a FOREIGN fleet (cross-fleet target).
    ids["other"] = seed("wt-other-owner", OTHER)
    print(f"  {'wt-other-owner':14s} seed={str(ids['other'])[:12]}  fleet={OTHER}")
    # A registered trust-1 agent with NO home fleet (seed omits fleet_id).
    ids["wt-fl"] = seed("wt-fl", None)
    s, t, f = set_trust("wt-fl", 1)
    print(f"  {'wt-fl':14s} seed={str(ids['wt-fl'])[:12]}  trust={t} fleet={f!r} (fleet-less)")
    return ids


def build_grid(ids):
    """(label, tool, args, min_trust) — run across the fleeted wt-t1/2/3 agents."""
    return [
        ("recall own-fleet", "caura_recall", {"query": "seed", "fleet_ids": [OWN]}, 1),
        ("recall cross-fleet", "caura_recall", {"query": "seed", "fleet_ids": [OTHER]}, 2),
        ("list own-fleet", "caura_list", {"scope": "fleet", "fleet_id": OWN}, 1),
        ("list cross-fleet", "caura_list", {"scope": "fleet", "fleet_id": OTHER}, 2),
        ("list no-fleet(pin)", "caura_list", {"scope": "fleet"}, 1),
        ("list all", "caura_list", {"scope": "all"}, 2),
        ("stats own-fleet", "caura_stats", {"scope": "fleet", "fleet_id": OWN}, 1),
        ("stats cross-fleet", "caura_stats", {"scope": "fleet", "fleet_id": OTHER}, 2),
        ("stats all", "caura_stats", {"scope": "all"}, 2),
    ]
    # NOTE: caura_manage op=read's by-id fleet isolation keys off the
    # gateway-VERIFIED caller identity (authorize_memory_access uses
    # _get_agent_id(), not the agent_id argument). This gatewayless keyed
    # harness has no verified identity, so op=read runs tenant-scoped and can't
    # exercise per-agent fleet trust here — that path is covered by
    # tests/test_memory_byid_authz.py. We still liveness-check op=read below.


def run_grid(ids):
    print("\n=== Trust grid (read paths × trust 1/2/3, home fleet = wt-fleet) ===")
    print(f"{'case':22s} {'T1':12s} {'T2':12s} {'T3':12s} expect")
    print("-" * 78)
    agents = {1: "wt-t1", 2: "wt-t2", 3: "wt-t3"}
    all_ok = True
    for label, tool, args, min_trust in build_grid(ids):
        cells = []
        for lvl in (1, 2, 3):
            status, _ = mcp(agents[lvl], tool, args)
            want_ok = lvl >= min_trust
            good = (status == "OK") if want_ok else (status in ("DENIED", "ERROR"))
            all_ok = all_ok and good
            cells.append(status if good else f"{status}!BAD")
        print(f"{label:22s} {cells[0]:12s} {cells[1]:12s} {cells[2]:12s} >= T{min_trust}")
    return all_ok


def run_fleetless():
    print("\n=== Fleet-less trust-1 agent (wt-fl, no home fleet) — all must DENY ===")
    print(f"{'case':40s} {'result':12s} expect")
    print("-" * 66)
    tests = [
        ("list scope=fleet, no fleet_id", "caura_list", {"scope": "fleet"}),
        ("stats scope=fleet, no fleet_id", "caura_stats", {"scope": "fleet"}),
        ("list scope=fleet, foreign fleet_id", "caura_list", {"scope": "fleet", "fleet_id": OWN}),
    ]
    all_ok = True
    for label, tool, args in tests:
        status, _ = mcp("wt-fl", tool, args)
        good = status in ("DENIED", "ERROR")
        all_ok = all_ok and good
        print(f"{label:40s} {status:12s} {'DENY' if good else 'DENY!BAD'}")
    return all_ok


def run_read_liveness(ids):
    print("\n=== op=read liveness (tenant-scoped in this harness; see note) ===")
    status, _ = mcp("wt-t1", "caura_manage", {"op": "read", "memory_id": ids["wt-t1"]})
    print(f"read own memory → {status}")
    return status == "OK"


if __name__ == "__main__":
    ids = provision()
    ok1 = run_grid(ids)
    ok2 = run_fleetless()
    ok3 = run_read_liveness(ids)
    print("\n" + ("ALL EXPECTATIONS MET" if (ok1 and ok2 and ok3) else "MISMATCHES FOUND (see !BAD)"))
