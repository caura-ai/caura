#!/usr/bin/env python3
"""A66 — is the N>2 chain defect real, or an artifact of the first repro?

The original repro slept only 5s between the FIRST and SECOND write (55s
thereafter), so write B's detection could run before write A had finished
embedding. That alone could produce "no candidates found". This version waits
the SAME settle after EVERY write, so any remaining failure is the product's.
"""

import json
import time
import urllib.error
import urllib.request

BASE, KEY = "http://localhost:8000", "dev-admin-key"
SETTLE = 60


def call(m, p, b=None):
    r = urllib.request.Request(
        BASE + p,
        method=m,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"},
        data=json.dumps(b).encode() if b is not None else None,
    )
    try:
        with urllib.request.urlopen(r, timeout=180) as x:
            return x.status, json.loads(x.read().decode(), strict=False)
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode()[:150]}


def w(t, c):
    return call(
        "POST", "/api/v1/memories", {"tenant_id": t, "agent_id": "a66", "content": c}
    )[1]


def st(t, i):
    s, m = call("GET", f"/api/v1/memories/{i}?tenant_id={t}")
    return (m.get("status"), m.get("supersedes_id")) if s == 200 else (f"<{s}>", None)


subjects = [
    ("deploy window", ["01:00 UTC", "03:00 UTC", "05:00 UTC"]),
    ("primary oncall", ["Marta", "Tomas", "Wei"]),
    ("build agent pool size", ["4 workers", "8 workers", "16 workers"]),
]
bad = 0
for n, (subj, vals) in enumerate(subjects):
    salt = f"{int(time.time())}{n}"[-7:]
    T, ids = f"a66r{salt}", []
    for v in vals:
        ids.append(w(T, f"The {salt} {subj} is {v}.").get("id"))
        time.sleep(SETTLE)  # EQUAL settle after every write
    rows = [(lbl, *st(T, i)) for lbl, i in zip("ABC", ids)]
    print(f"\n--- run {n + 1}: {subj}  (settle={SETTLE}s after every write)")
    for lbl, s, sup in rows:
        print(f"    {lbl}: status={s:11s} supersedes={str(sup)[:8]}")
    actives = [lbl for lbl, s, _ in rows if s not in ("outdated", "conflicted")]
    ok = len(actives) == 1 and actives[0] == "C"
    bad += not ok
    print(
        f"    exactly one winner (C)? {'yes' if ok else 'NO -> ' + ','.join(actives) + ' all live'}"
    )

print(
    f"\nA66 RESULT: {bad}/{len(subjects)} runs ended with contradictory claims co-active"
)
