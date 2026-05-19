#!/usr/bin/env python3
"""F3 Phase 0 observer — write a memory + capture observable state.

Driven by ``scripts/f3_wet_test_matrix.sh``. Reads two args:
    cell_name  — "inline" | "deferred"
    output_dir — path to write the JSON record to

Assumes the local stack is up, ``/tmp/local-dev.token`` exists, and
the core-api container is the one named below.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
import sys
import time
import urllib.request

CONTAINER_CORE_API = "caura-memclaw-enterprise-core-api-1"
CONTAINER_POSTGRES = "caura-memclaw-enterprise-postgres-1"
TENANT = "dev-41fddf"


def write_memory(content: str, agent: str, mode: str = "strong") -> dict:
    with open("/tmp/local-dev.token") as f:
        token = f.read().strip()
    req = urllib.request.Request(
        "http://localhost/api/v1/memories",
        data=json.dumps(
            {
                "content": content,
                "tenant_id": TENANT,
                "agent_id": agent,
                "write_mode": mode,
            }
        ).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def psql(sql: str) -> str:
    """Run a `-tAc` query against the local Postgres container."""
    out = subprocess.run(
        [
            "docker",
            "exec",
            CONTAINER_POSTGRES,
            "psql",
            "-U",
            "memclaw",
            "-d",
            "memclaw",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def grep_logs_for(memory_id: str) -> list[str]:
    out = subprocess.run(
        ["docker", "logs", CONTAINER_CORE_API, "--since", "3m"],
        capture_output=True,
        text=True,
    )
    keep = []
    for line in (out.stdout + out.stderr).splitlines():
        if memory_id in line and any(
            marker in line
            for marker in ("EMBED_REQUESTED", "ENRICH_REQUESTED", "publish_memory")
        ):
            keep.append(line.strip())
    return keep[:20]


def env_inside_container(var: str) -> str:
    out = subprocess.run(
        ["docker", "exec", CONTAINER_CORE_API, "sh", "-c", f'echo "${{{var}:-unset}}"'],
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: _f3_wet_test_observer.py <cell_name> <output_dir> [mode]")
        return 2
    cell = sys.argv[1]
    out_dir = sys.argv[2]
    mode = sys.argv[3] if len(sys.argv) > 3 else "strong"
    os.makedirs(out_dir, exist_ok=True)

    embed_env = env_inside_container("EMBED_ON_HOT_PATH")
    enrich_env = env_inside_container("ENRICH_ON_HOT_PATH")
    print(
        f"=== CELL: {cell}  in-container: EMBED_ON_HOT_PATH={embed_env}  "
        f"ENRICH_ON_HOT_PATH={enrich_env} ==="
    )

    # uuid in content so prior runs don't 409 us out via semantic dedup
    # (the inline cell embeds + checks for near-duplicates inline).
    nonce = uuid.uuid4().hex[:10]
    content = (
        f"F3 Phase 0 {cell}-{mode} {nonce}: Alma Reyes shipped Helios-{nonce} "
        f"to the Aurora squad on May {nonce}."
    )
    print(f"writing memory (mode={mode})...")
    resp = write_memory(content, f"f3-{cell}-{mode}", mode)
    mid = resp.get("id")
    if not mid:
        print(f"WRITE FAILED: {resp}")
        return 1
    print(f"wrote memory {mid}; sleeping 20s for async settle")
    time.sleep(20)

    record: dict = {
        "cell": cell,
        "mode": mode,
        "memory_id": mid,
        "env": {"EMBED_ON_HOT_PATH": embed_env, "ENRICH_ON_HOT_PATH": enrich_env},
        "response": {
            "title": resp.get("title"),
            "memory_type": resp.get("memory_type"),
            "weight": resp.get("weight"),
            "status": resp.get("status"),
            "summary_present": bool((resp.get("metadata") or {}).get("summary")),
            "tags_present": bool((resp.get("metadata") or {}).get("tags")),
        },
        "db": {
            "embedding_is_null": psql(
                f"SELECT embedding IS NULL FROM memories WHERE id='{mid}';"
            )
            == "t",
            "title": psql(f"SELECT title FROM memories WHERE id='{mid}';") or None,
            "memory_type": psql(f"SELECT memory_type FROM memories WHERE id='{mid}';")
            or None,
            "status": psql(f"SELECT status FROM memories WHERE id='{mid}';") or None,
            "summary_present": psql(
                f"SELECT (metadata->>'summary') IS NOT NULL "
                f"FROM memories WHERE id='{mid}';"
            )
            == "t",
            "tags_present": psql(
                f"SELECT json_array_length(COALESCE(metadata->'tags','[]'::json)) > 0 "
                f"FROM memories WHERE id='{mid}';"
            )
            == "t",
        },
        "publishes_observed_in_logs": grep_logs_for(mid),
    }

    out_path = os.path.join(out_dir, f"{cell}-{mode}.json")
    with open(out_path, "w") as f:
        json.dump(record, f, indent=2, default=str)

    print(f"\n=== observed: {out_path} ===")
    print(json.dumps(record, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
