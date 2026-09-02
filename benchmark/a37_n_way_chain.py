#!/usr/bin/env python3
"""A37 — does a 3-step update chain leave the OLDEST claim active?

One observation could be LLM non-determinism. Run the triangle three times with
different subjects and report how often the oldest row survives as `active`
alongside the newest.
"""

import json
import time
import urllib.error
import urllib.request

BASE, KEY = "http://localhost:8000", "dev-admin-key"
SETTLE = 55


def call(method, path, body=None):
    req = urllib.request.Request(
        BASE + path,
        method=method,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode(), strict=False)
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode()[:150]}


def w(t, c):
    return call(
        "POST", "/api/v1/memories", {"tenant_id": t, "agent_id": "a37", "content": c}
    )[1]


def st(t, mid):
    s, m = call("GET", f"/api/v1/memories/{mid}?tenant_id={t}")
    return (m.get("status"), m.get("supersedes_id")) if s == 200 else (f"<{s}>", None)


subjects = [
    ("deploy window", ["01:00 UTC", "03:00 UTC", "05:00 UTC"]),
    ("primary oncall", ["Marta", "Tomas", "Wei"]),
    ("build agent pool size", ["4 workers", "8 workers", "16 workers"]),
]
stale_survives = 0
for n, (subj, vals) in enumerate(subjects):
    salt = f"{int(time.time())}{n}"[-7:]
    T = f"a37r{salt}"
    ids = []
    for i, v in enumerate(vals):
        m = w(T, f"The {salt} {subj} is {v}.")
        ids.append(m.get("id"))
        time.sleep(SETTLE if i else 5)
    rows = [(lbl, *st(T, i)) for lbl, i in zip("ABC", ids)]
    print(f"\n--- run {n + 1}: {subj}")
    for lbl, s, sup in rows:
        print(f"    {lbl}: status={s:10s} supersedes={str(sup)[:8]}")
    oldest_status = rows[0][1]
    newest_status = rows[-1][1]
    bad = oldest_status not in ("outdated", "conflicted") and newest_status == "active"
    stale_survives += bad
    print(
        f"    oldest still active alongside newest: {'YES  <-- stale claim live' if bad else 'no'}"
    )

print(f"\nA37 RESULT: oldest claim survived in {stale_survives}/{len(subjects)} runs")
