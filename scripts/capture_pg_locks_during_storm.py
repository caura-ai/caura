#!/usr/bin/env python3
"""Poll core-storage-api's /_debug/pg_locks endpoint during a loadtest storm.

CAURA-686 toolkit. The endpoint is on the writer's private VPC IP, so
this script assumes a local proxy is forwarding it — typically::

    gcloud run services proxy staging-memclaw-core-storage-writer \\
        --port 8080 --region us-central1

Then::

    python3 scripts/capture_pg_locks_during_storm.py \\
        --base http://localhost:8080 \\
        --duration 90 \\
        --out /tmp/pg-locks-storm.jsonl

The script writes one JSON line per snapshot (one per second by default),
each containing a timestamp + the endpoint payload. Post-process with
``jq`` to filter the storm window, group by ``wait_event``, or chase
``blocked_by_pids`` chains.

The endpoint is intentionally separate from the gateway so it's
reachable only via this proxy or from inside the VPC; the script is a
thin polling loop on top of it, not a permanent surface.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import urllib.error
import urllib.request


def _snapshot(base: str, timeout: float = 5.0) -> dict:
    """Fetch one /_debug/pg_locks payload."""
    url = f"{base.rstrip('/')}/api/v1/storage/_debug/pg_locks"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base",
        default="http://localhost:8080",
        help="Base URL of the local proxy to storage-api (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=90,
        help="Total seconds to poll (default: 90 — covers baseline + storm + tail)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Seconds between snapshots (default: 1.0)",
    )
    parser.add_argument(
        "--out",
        default="-",
        help="Output JSONL path (default: stdout)",
    )
    args = parser.parse_args()

    # ``contextlib.nullcontext(sys.stdout)`` keeps the ``with`` shape
    # consistent across stdout and a real file, and inlining ``open()``
    # in the ``with``'s expression means the file is acquired as part
    # of the same context — no risk of a half-open handle from a
    # ``ctx = open(...)`` assignment outside the ``with``.
    deadline = time.monotonic() + args.duration
    with (
        open(args.out, "w", buffering=1)
        if args.out != "-"
        else contextlib.nullcontext(sys.stdout)
    ) as sink:
        n_ok = 0
        n_err = 0
        try:
            while time.monotonic() < deadline:
                t0 = time.monotonic()
                try:
                    payload = _snapshot(args.base)
                    sink.write(
                        json.dumps(
                            {
                                "polled_at": time.time(),
                                "rows": payload.get("rows", []),
                                "captured_at": payload.get("captured_at"),
                            }
                        )
                        + "\n"
                    )
                    n_ok += 1
                except (
                    urllib.error.URLError,
                    urllib.error.HTTPError,
                    TimeoutError,
                    # ``json.JSONDecodeError`` inherits from
                    # ``ValueError``; covers the case where the
                    # endpoint serves a partial / malformed response
                    # during a cold-start or proxy hiccup.
                    ValueError,
                ) as exc:
                    # Endpoint may briefly fail during a cold-start or
                    # while the proxy is reconnecting. Log to stderr
                    # and keep going — the storm window is short and
                    # a missed sample isn't fatal.
                    print(f"[poll error] {exc}", file=sys.stderr)
                    n_err += 1
                elapsed = time.monotonic() - t0
                sleep = args.interval - elapsed
                if sleep > 0:
                    time.sleep(sleep)
        finally:
            print(
                f"[done] ok={n_ok} err={n_err} duration={args.duration}s",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
