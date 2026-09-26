"""Two worker processes, one state directory, one beat per cycle.

The production defect: ``uvicorn --workers 2`` ran two independent heartbeat
loops per container (two POSTs per cycle, ``GET /telemetry`` differing by
worker, the client counter split in half). This test spawns two real
processes the way uvicorn does, each with its own ``HeartbeatSender`` on a
shared state directory, points them at a fake collector in this process, and
asserts: exactly one POST per cycle, the beat carries the SUM of both
workers' client counts, and both workers report identical status.

Intervals are injected through the sender's test overrides so the whole
thing takes a few seconds.
"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any

import pytest

from core_api.heartbeat import clients
from core_api.heartbeat.identity import DeploymentIdentity
from core_api.heartbeat.payload import Counts
from core_api.heartbeat.sender import HeartbeatSender
from core_api.heartbeat.state import SharedState

pytestmark = [pytest.mark.unit]

IDENT = DeploymentIdentity(
    deployment_id="6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90", deployment_token="cd" * 32
)

FIRST_DELAY = 0.8
INTERVAL = 1.2
FLUSH = 0.15
HOLD = 2.5  # covers two sends (0.8 s, 2.0 s) and ends before the third (3.2 s)


class _CannedSender(HeartbeatSender):
    """The real loop, lock, counter files and POST; storage replaced by constants."""

    async def _read_storage(self):
        return IDENT, Counts(memories=7, agents=1, tenants=1)


def _settings(url: str) -> SimpleNamespace:
    return SimpleNamespace(
        caura_telemetry_url=url,
        is_standalone=False,
        embedding_provider="openai",
        entity_extraction_provider="none",
        redis_url="",
        sentry_dsn="",
    )


async def _worker_main(
    state_dir: str, url: str, records: int, barrier: Any, results: Any
) -> None:
    clients.disable()
    shared = SharedState.open(state_dir)
    assert shared is not None
    sender = _CannedSender(
        _settings(url),
        shared=shared,
        version="v3.17.0",
        first_delay=FIRST_DELAY,
        interval=INTERVAL,
        flush_interval=FLUSH,
        leader_retry=FLUSH,
    )
    barrier.wait(timeout=60)  # both workers start their loops together
    sender.start()
    for _ in range(records):
        clients.record("caura-client-python/1.0.2")
    await asyncio.sleep(HOLD)
    preview = await sender.preview()
    results.put(
        {
            "pid": os.getpid(),
            "role": sender.role,
            "status": sender.status(),
            "preview_clients": preview["clients_24h"],
        }
    )
    # Shutdown as the lifespan does it: cancel the tracked tasks. The sync
    # task's cancellation handler flushes the counter and releases the lock.
    tasks = [t for t in (sender._task, sender._sync_task) if t is not None]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _worker(state_dir: str, url: str, records: int, barrier: Any, results: Any) -> None:
    asyncio.run(_worker_main(state_dir, url, records, barrier, results))


class _Receiver(BaseHTTPRequestHandler):
    beats: list[dict[str, Any]] = []
    lock = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        with self.lock:
            self.beats.append(
                {
                    "body": body,
                    "auth": self.headers.get("Authorization"),
                    "ua": self.headers.get("User-Agent"),
                }
            )
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *_args: Any) -> None:
        pass


@pytest.fixture
def receiver():
    _Receiver.beats = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/api/telemetry/heartbeat"
    server.shutdown()
    server.server_close()


def test_two_workers_send_one_beat_per_cycle_and_agree(tmp_path, receiver):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    results = ctx.Queue()
    state_dir = str(tmp_path / "caura-heartbeat")
    workers = [
        ctx.Process(
            target=_worker, args=(state_dir, receiver, records, barrier, results)
        )
        for records in (1, 1)
    ]
    for w in workers:
        w.start()
    reports = [results.get(timeout=90) for _ in workers]
    for w in workers:
        w.join(timeout=30)
        assert w.exitcode == 0, f"worker {w.pid} exited {w.exitcode}"

    roles = sorted(r["role"] for r in reports)
    assert roles == ["follower", "leader"], reports

    # Exactly one POST per cycle: two cycles happened inside HOLD.
    beats = list(_Receiver.beats)
    assert len(beats) == 2, [b["body"]["sent_at"] for b in beats]
    assert all(b["auth"] == f"Bearer {IDENT.deployment_token}" for b in beats)
    assert all(b["ua"] == "caura-server/3.17.0" for b in beats)  # leading "v" stripped
    assert all(b["body"]["version"] == "3.17.0" for b in beats)

    # The first beat carries BOTH workers' counts (1 + 1 -> "2-5"; either
    # alone would be "1"); the second, after the epoch reset, carries none.
    assert beats[0]["body"]["clients_24h"]["caura-client-python"] == "2-5"
    assert beats[1]["body"]["clients_24h"]["caura-client-python"] == "0"

    # Both workers answer GET the same way: the follower reads state.json.
    leader, follower = (
        next(r for r in reports if r["role"] == "leader"),
        next(r for r in reports if r["role"] == "follower"),
    )
    assert leader["status"] == follower["status"], reports
    assert leader["status"]["last_status"] == 202
    assert leader["status"]["last_error"] is None
    assert leader["status"]["last_sent_at"] is not None
    assert leader["status"]["last_attempt_at"] is not None
    assert leader["status"]["next_send_at"] is not None
    assert leader["status"]["deployment_id"] == IDENT.deployment_id
    # And the follower's own preview uses the summed (now reset) counters.
    assert follower["preview_clients"] == leader["preview_clients"]

    # Shutdown released the lock and left no orphaned counter files.
    assert SharedState(tmp_path / "caura-heartbeat").try_acquire_leader() is True
