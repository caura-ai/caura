"""Per-container coordination between uvicorn workers.

The Docker image runs ``--workers 2`` and every worker runs the lifespan, so
before this module each worker owned a full heartbeat loop: two POSTs per
cycle per container (seen in production on 2026-09-19), ``GET /telemetry``
answers that differed by worker, and a ``clients_24h`` counter split across
processes so the stored beat carried only the sending worker's half.

Coordination goes through four files in one directory
(``settings.caura_telemetry_state_dir``, default ``<tmpdir>/caura-heartbeat``,
created ``0700``). No new dependency, no shared memory, no extra process:

* ``leader.lock`` -- a non-blocking ``fcntl.flock``. The worker that holds it
  runs the send loop; the others run none. The kernel drops the lock when the
  holder dies, so a follower's slow retry (:data:`LEADER_RETRY_SECONDS`) takes
  over without anyone noticing the death.
* ``clients-<pid>.json`` -- each worker's User-Agent family counter, flushed
  every :data:`FLUSH_INTERVAL_SECONDS` and on shutdown (write-to-temp +
  atomic rename). At send time the leader sums its own live counter with
  every other live worker's file. Files whose pid is dead are pruned.
* ``epoch`` -- the timestamp of the last accepted send. A worker that sees a
  newer epoch at its next flush subtracts what it had already flushed (those
  counts went out in that beat) and keeps what accrued since.
* ``state.json`` -- what the leader last did (``last_sent_at``, ``last_status``,
  ``last_error``, ``next_send_at`` ...). A follower answers ``GET /telemetry``
  from it, so every worker reports the same values.

If the directory cannot be created or written, :func:`SharedState.open`
returns ``None`` and the sender falls back to the single-process behaviour
(one loop per worker) with a WARNING at boot. Nothing here raises past its
public methods: a coordination hiccup must never take the server down.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from core_api.heartbeat import clients

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock; coordination is skipped there
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger("core_api.heartbeat")

LOCK_FILE = "leader.lock"
EPOCH_FILE = "epoch"
STATE_FILE = "state.json"
_CLIENTS_FILE = re.compile(r"^clients-(\d+)\.json$")

# How often each worker writes its counter file, and how often a follower
# retries the leader lock. Both are slow ticks: the flush bounds how many
# counts a killed worker can lose, the retry bounds how long a container goes
# without a sender after its leader died.
FLUSH_INTERVAL_SECONDS = 30.0
LEADER_RETRY_SECONDS = 60.0

# The keys ``state.json`` carries; also the keys a follower copies into
# ``GET /telemetry``. ISO-8601 strings and small scalars only.
STATUS_KEYS: tuple[str, ...] = (
    "deployment_id",
    "last_attempt_at",
    "last_sent_at",
    "last_status",
    "last_error",
    "next_send_at",
)


def pid_alive(pid: int) -> bool:
    """Whether ``pid`` still exists. ``EPERM`` means alive but not ours."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


class SharedState:
    """One worker's handle on the container-wide state directory."""

    def __init__(self, directory: Path, *, pid: int | None = None) -> None:
        self.directory = directory
        self.pid = pid if pid is not None else os.getpid()
        self._lock_fd: int | None = None
        self._last_flushed: dict[str, int] = dict.fromkeys(clients.FAMILIES, 0)
        self._seen_epoch: float | None = self.read_epoch()

    @classmethod
    def open(cls, directory: str | os.PathLike[str] | None) -> SharedState | None:
        """Create ``directory`` (``0700``) and prove it writable; ``None`` if not.

        ``None`` is the fallback signal: the caller logs a WARNING and runs
        the single-process loop. An empty ``directory`` means the operator
        switched coordination off deliberately; that is also ``None``.
        """
        if not directory or fcntl is None:
            return None
        path = Path(directory)
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            probe = path / f".probe-{os.getpid()}"
            probe.write_text("")
            probe.unlink()
        except OSError:
            return None
        return cls(path)

    # -- leader lock -------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        return self._lock_fd is not None

    @property
    def lock_path(self) -> Path:
        return self.directory / LOCK_FILE

    def try_acquire_leader(self) -> bool:
        """Take ``leader.lock`` without blocking. Idempotent for the holder."""
        if self._lock_fd is not None:
            return True
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        try:
            # The pid inside is informational (boot line, debugging); the lock
            # itself is what elects.
            os.ftruncate(fd, 0)
            os.write(fd, str(self.pid).encode())
        except OSError:
            pass
        return True

    def leader_pid(self) -> int | None:
        """The pid written by the current or last lock holder, if readable."""
        try:
            text = self.lock_path.read_text().strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def release_leader(self) -> None:
        if self._lock_fd is None:
            return
        fd, self._lock_fd = self._lock_fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # -- files -------------------------------------------------------------

    def _write_atomic(self, path: Path, text: str) -> None:
        tmp = path.with_name(f".{path.name}.{self.pid}.tmp")
        tmp.write_text(text)
        tmp.replace(path)  # atomic rename; readers see the old or the new file, never a torn one

    def _write_json(self, path: Path, data: Mapping[str, Any]) -> None:
        self._write_atomic(path, json.dumps(data, sort_keys=True))

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # -- epoch -------------------------------------------------------------

    def read_epoch(self) -> float | None:
        try:
            text = (self.directory / EPOCH_FILE).read_text().strip()
            return float(text) if text else None
        except (OSError, ValueError):
            return None

    def mark_epoch(self, when: float | None = None) -> float:
        """Record an accepted send; every worker's next flush drops what went out."""
        epoch = time.time() if when is None else when
        try:
            self._write_atomic(self.directory / EPOCH_FILE, repr(epoch))
        except OSError:
            logger.debug("[telemetry] could not write the heartbeat epoch", exc_info=True)
        self._seen_epoch = epoch
        return epoch

    # -- client counters ---------------------------------------------------

    def clients_path(self, pid: int | None = None) -> Path:
        return self.directory / f"clients-{self.pid if pid is None else pid}.json"

    def flush(self) -> None:
        """Write this worker's live counter to its file, honouring the epoch.

        If the leader accepted a send since the last flush, the counts that
        were in the file then have been reported; subtract them from the live
        counter first so only what accrued since is carried forward.
        """
        epoch = self.read_epoch()
        if epoch is not None and (self._seen_epoch is None or epoch > self._seen_epoch):
            clients.subtract(self._last_flushed)
            self._seen_epoch = epoch
        snap = clients.snapshot()
        try:
            self._write_json(
                self.clients_path(),
                {"pid": self.pid, "flushed_at": time.time(), "counts": snap},
            )
        except OSError:
            logger.debug("[telemetry] could not flush the client counter", exc_info=True)
            return
        self._last_flushed = snap

    def commit_send(self) -> None:
        """After an accepted send: zero the live counter, advance the epoch, re-flush."""
        clients.reset()
        self._last_flushed = dict.fromkeys(clients.FAMILIES, 0)
        self.mark_epoch()
        self.flush()

    def prune_dead(self) -> None:
        """Remove counter files left by workers that no longer exist."""
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return
        for path in entries:
            m = _CLIENTS_FILE.match(path.name)
            if not m:
                continue
            pid = int(m.group(1))
            if pid == self.pid or pid_alive(pid):
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def aggregate(self, live: Mapping[str, int] | None = None) -> dict[str, int]:
        """Sum this worker's live counter with every other live worker's file.

        Files flushed before the current epoch were already reported and are
        skipped; files of dead pids are pruned on the way.
        """
        base = clients.snapshot() if live is None else live
        total = {family: int(base.get(family, 0)) for family in clients.FAMILIES}
        self.prune_dead()
        epoch = self.read_epoch()
        try:
            entries = list(self.directory.iterdir())
        except OSError:
            return total
        for path in entries:
            m = _CLIENTS_FILE.match(path.name)
            if not m or int(m.group(1)) == self.pid:
                continue
            data = self._read_json(path)
            if data is None:
                continue
            try:
                flushed_at = float(data.get("flushed_at", 0))
            except (TypeError, ValueError):
                flushed_at = 0.0
            if epoch is not None and flushed_at < epoch:
                continue
            counts = data.get("counts")
            if not isinstance(counts, dict):
                continue
            for family in clients.FAMILIES:
                try:
                    total[family] += max(0, int(counts.get(family, 0)))
                except (TypeError, ValueError):
                    continue
        return total

    # -- leader status -----------------------------------------------------

    def write_status(self, status: Mapping[str, Any]) -> None:
        """The leader's view after each cycle step, for followers' ``GET``."""
        data = {key: status.get(key) for key in STATUS_KEYS}
        data["leader_pid"] = self.pid
        data["updated_at"] = time.time()
        try:
            self._write_json(self.directory / STATE_FILE, data)
        except OSError:
            logger.debug("[telemetry] could not write heartbeat state", exc_info=True)

    def read_status(self) -> dict[str, Any] | None:
        data = self._read_json(self.directory / STATE_FILE)
        if data is None:
            return None
        return {key: data.get(key) for key in STATUS_KEYS}
