"""Per-container coordination: leader lock, counter files, epoch reset, state.json.

Everything here runs in one process. ``flock`` locks belong to the open file
description, not the process, so two ``SharedState`` objects on one directory
contend exactly like two uvicorn workers do. The family counter is
process-global, so the "follower" half of the counter tests drives the real
``clients`` module and the "leader" half passes its live counts explicitly.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from core_api.heartbeat import clients
from core_api.heartbeat.state import STATUS_KEYS, SharedState, pid_alive

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _counter():
    clients.disable()
    yield
    clients.disable()


def _dead_pid() -> int:
    """A pid that existed a moment ago and is gone now."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    assert proc.wait(timeout=30) == 0
    assert not pid_alive(proc.pid)
    return proc.pid


# -- open() --------------------------------------------------------------------


def test_open_creates_the_directory_private(tmp_path):
    directory = tmp_path / "nested" / "caura-heartbeat"
    shared = SharedState.open(directory)
    assert shared is not None
    assert directory.is_dir()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert list(directory.iterdir()) == []  # the probe file is gone


def test_open_returns_none_when_unusable(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("")
    assert SharedState.open(blocker / "under-a-file") is None
    assert SharedState.open("") is None
    assert SharedState.open(None) is None


# -- leader lock ---------------------------------------------------------------


def test_lock_is_exclusive_and_hands_over_on_release(tmp_path):
    a = SharedState(tmp_path)
    b = SharedState(tmp_path, pid=os.getppid())
    assert a.try_acquire_leader() is True
    assert a.try_acquire_leader() is True  # idempotent for the holder
    assert a.is_leader and not b.is_leader
    assert b.try_acquire_leader() is False
    assert b.leader_pid() == a.pid

    a.release_leader()
    assert not a.is_leader
    assert b.try_acquire_leader() is True
    assert b.is_leader
    assert a.try_acquire_leader() is False
    assert a.leader_pid() == b.pid
    b.release_leader()


def test_lock_survives_the_holder_only_while_it_lives(tmp_path):
    """The kernel drops the lock with the process: a follower can take over."""
    lock = tmp_path / "leader.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
            "print('locked', flush=True)\n"
            "time.sleep(30)\n",
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        follower = SharedState(tmp_path)
        assert follower.try_acquire_leader() is False
        holder.kill()
        holder.wait(timeout=10)
        deadline = time.monotonic() + 5
        while not follower.try_acquire_leader():
            assert time.monotonic() < deadline, (
                "lock not released after the holder died"
            )
            time.sleep(0.05)
        assert follower.is_leader
        follower.release_leader()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


# -- counter files and the epoch -----------------------------------------------


def test_aggregate_sums_live_counts_with_other_workers_files(tmp_path):
    leader = SharedState(tmp_path)
    follower = SharedState(tmp_path, pid=os.getppid())
    clients.enable()
    for _ in range(3):
        clients.record("caura-client-python/1.0.2")
    follower.flush()
    written = json.loads(follower.clients_path().read_text())
    assert written["pid"] == follower.pid
    assert written["counts"]["caura-client-python"] == 3

    total = leader.aggregate(live={"mcp": 2, "caura-client-python": 1})
    assert total["caura-client-python"] == 4
    assert total["mcp"] == 2
    assert set(total) == set(clients.FAMILIES)


def test_aggregate_skips_the_workers_own_file(tmp_path):
    me = SharedState(tmp_path)
    clients.enable()
    clients.record_mcp()
    me.flush()
    # Live counter says 1; the file says 1 too, but it is ours: not doubled.
    assert me.aggregate()["mcp"] == 1


def test_commit_send_advances_the_epoch_and_followers_drop_reported_counts(tmp_path):
    leader = SharedState(tmp_path)
    follower = SharedState(tmp_path, pid=os.getppid())
    clients.enable()
    for _ in range(3):
        clients.record("caura-client-python/1.0.2")
    follower.flush()  # the file the leader will sum: 3

    assert leader.read_epoch() is None
    leader.commit_send()  # the leader's own live counter is zeroed ...
    assert all(v == 0 for v in clients.snapshot().values())
    epoch = leader.read_epoch()
    assert epoch is not None
    assert (
        json.loads(leader.clients_path().read_text())["counts"]["caura-client-python"]
        == 0
    )

    # ... and the follower, whose in-process counter has meanwhile grown to 4
    # (3 reported + 1 new), keeps only the 1 that accrued after its last flush.
    for _ in range(4):
        clients.record("caura-client-python/1.0.2")
    follower.flush()
    assert clients.snapshot()["caura-client-python"] == 1
    assert (
        json.loads(follower.clients_path().read_text())["counts"]["caura-client-python"]
        == 1
    )
    assert leader.aggregate(live={})["caura-client-python"] == 1

    # A second flush with no new epoch subtracts nothing.
    follower.flush()
    assert clients.snapshot()["caura-client-python"] == 1


def test_aggregate_ignores_files_flushed_before_the_epoch(tmp_path):
    """A worker that has not flushed since the last send was already counted."""
    leader = SharedState(tmp_path)
    stale = SharedState(tmp_path, pid=os.getppid())
    clients.enable()
    clients.record("caura-rail-node/1.0.1")
    stale.flush()
    time.sleep(0.01)
    leader.mark_epoch()
    assert leader.aggregate(live={})["caura-rail-node"] == 0
    # Once the worker flushes again (post-epoch) what accrued since counts:
    # its counter grew to 2, the 1 it had flushed before the epoch is dropped.
    clients.record("caura-rail-node/1.0.1")
    stale.flush()
    assert clients.snapshot()["caura-rail-node"] == 1
    assert leader.aggregate(live={})["caura-rail-node"] == 1


def test_dead_workers_files_are_pruned(tmp_path):
    leader = SharedState(tmp_path)
    dead = SharedState(tmp_path, pid=_dead_pid())
    dead.flush()
    assert dead.clients_path().exists()
    alive = SharedState(tmp_path, pid=os.getppid())
    alive.flush()
    leader.aggregate()
    assert not dead.clients_path().exists()
    assert alive.clients_path().exists()


def test_malformed_files_are_ignored(tmp_path):
    leader = SharedState(tmp_path)
    (tmp_path / f"clients-{os.getppid()}.json").write_text("{not json")
    (tmp_path / "epoch").write_text("garbage")
    assert leader.read_epoch() is None
    assert leader.aggregate(live={"mcp": 1}) == {
        **dict.fromkeys(clients.FAMILIES, 0),
        "mcp": 1,
    }


def test_subtract_clamps_at_zero():
    clients.enable()
    clients.record_mcp()
    clients.subtract({"mcp": 5, "other": 1})
    assert clients.snapshot()["mcp"] == 0
    assert clients.snapshot()["other"] == 0


# -- state.json ----------------------------------------------------------------


def test_status_roundtrip(tmp_path):
    leader = SharedState(tmp_path)
    follower = SharedState(tmp_path, pid=os.getppid())
    assert follower.read_status() is None
    leader.write_status(
        {
            "deployment_id": "6f0a3c2e-1b4d-4e8f-9a7c-2d5e8b1f4a90",
            "last_attempt_at": "2026-09-19T16:23:52Z",
            "last_sent_at": "2026-09-19T16:23:52Z",
            "last_status": 202,
            "last_error": None,
            "next_send_at": "2026-09-20T16:20:00Z",
            "extra": "dropped",
        }
    )
    stored = follower.read_status()
    assert stored is not None
    assert set(stored) == set(STATUS_KEYS)
    assert stored["last_status"] == 202
    assert stored["next_send_at"] == "2026-09-20T16:20:00Z"
    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["leader_pid"] == leader.pid
    assert "extra" not in raw


def test_status_missing_keys_read_as_none(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"last_status": 500}))
    stored = SharedState(tmp_path).read_status()
    assert stored is not None
    assert stored["last_status"] == 500
    assert stored["deployment_id"] is None


def test_corrupt_status_reads_as_none(tmp_path):
    (tmp_path / "state.json").write_text("[]")
    assert SharedState(tmp_path).read_status() is None
    (tmp_path / "state.json").write_text("{")
    assert SharedState(tmp_path).read_status() is None
