"""The in-memory per-minute totals a service flushes to storage.

A provider records a call with a dict update under a lock (calls arrive from
the event loop and from ``asyncio.to_thread`` workers alike); a background
loop sends the totals every ``FLUSH_INTERVAL_SECONDS`` through the service's
own storage client. Each flush carries a batch id that storage claims once
(migration 066), so a batch whose reply was lost is re-sent as-is and counted
once — the property ``tenant_usage_counters`` lacks, which forces its writer
to drop such a batch instead (OSS 09/02 L-44).

Bounded: if storage stays down, at most ``MAX_PENDING_BATCHES`` batches wait,
and the oldest is dropped (logged with its row count) to make room.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from common.usage.sink import UNATTRIBUTED, ModelCall, ValueRecord

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECONDS = 15.0
MAX_PENDING_BATCHES = 20

_LlmKey = tuple[str, str, str, str, str, str, str, str]
_ValueKey = tuple[str, str, str, str, str, str]
_LLM_COUNTS = ("calls", "failures", "tokens_in", "tokens_out", "tokens_reasoning")

#: ``post(batch)`` sends one batch: ``{"batch_id", "llm": [...], "value": [...]}``.
#: Storage takes one tenant per flush, so a sender splits it with
#: :func:`per_tenant`.
PostBatch = Callable[[dict], Awaitable[object]]
#: Given the tenants in a batch, returns those that keep metering on.
AllowTenants = Callable[[set[str]], Awaitable[set[str]]]


def _minute(now: datetime | None = None) -> str:
    ts = (now or datetime.now(UTC)).astimezone(UTC)
    return ts.replace(second=0, microsecond=0).isoformat()


def per_tenant(batch: dict) -> list[tuple[str, dict]]:
    """Split one batch into a flush per tenant, as storage takes them.

    Storage binds a flush to the one tenant its ``tenant_id`` names, so no
    request can write another tenant's totals. Each tenant's batch id is
    derived from the batch's (``uuid5``): a batch re-sent after a failure,
    even one that half landed, re-sends the same ids, and storage still counts
    each tenant's part once. The rows lose their ``tenant_id``; the request
    carries it.
    """
    root = uuid.UUID(batch["batch_id"])
    parts: dict[str, dict] = {}
    for kind in ("llm", "value"):
        for row in batch.get(kind, []):
            tenant = row["tenant_id"]
            part = parts.setdefault(
                tenant,
                {"batch_id": str(uuid.uuid5(root, tenant)), "llm": [], "value": []},
            )
            part[kind].append({k: v for k, v in row.items() if k != "tenant_id"})
    return sorted(parts.items())


@dataclass
class _Batch:
    batch_id: str
    llm: list[dict] = field(default_factory=list)
    value: list[dict] = field(default_factory=list)

    def payload(self) -> dict:
        return {"batch_id": self.batch_id, "llm": self.llm, "value": self.value}

    def __len__(self) -> int:
        return len(self.llm) + len(self.value)


class UsageAggregator:
    """A :class:`common.usage.UsageSink` that adds up per minute and flushes."""

    def __init__(
        self,
        post: PostBatch,
        *,
        allow_tenants: AllowTenants | None = None,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._post = post
        self._allow_tenants = allow_tenants
        self._interval = flush_interval
        self._clock = clock
        self._lock = threading.Lock()
        self._llm: dict[_LlmKey, list[int]] = {}
        self._value: dict[_ValueKey, int] = {}
        self._pending: list[_Batch] = []
        self._task: asyncio.Task | None = None
        self.dropped_rows = 0

    # ── UsageSink ────────────────────────────────────────────────────────

    def add_model_call(self, call: ModelCall) -> None:
        s = call.scope
        key: _LlmKey = (
            _minute(self._clock() if self._clock else None),
            s.tenant_id or UNATTRIBUTED,
            s.agent_id or "",
            s.fleet_id or "",
            call.operation,
            call.kind,
            str(call.provider),
            str(call.model),
        )
        with self._lock:
            counts = self._llm.setdefault(key, [0, 0, 0, 0, 0])
            if call.ok:
                counts[0] += 1
                counts[2] += call.tokens_in
                counts[3] += call.tokens_out
                counts[4] += call.tokens_reasoning
            else:
                counts[1] += 1

    def add_value(self, record: ValueRecord) -> None:
        s = record.scope
        if not s.tenant_id:
            return
        key: _ValueKey = (
            _minute(self._clock() if self._clock else None),
            s.tenant_id,
            s.agent_id or "",
            s.fleet_id or "",
            s.operation or "",
            record.metric,
        )
        with self._lock:
            self._value[key] = self._value.get(key, 0) + record.quantity

    # ── Flushing ─────────────────────────────────────────────────────────

    def _drain(self) -> _Batch | None:
        with self._lock:
            llm, value = self._llm, self._value
            self._llm, self._value = {}, {}
        if not llm and not value:
            return None
        batch = _Batch(batch_id=str(uuid.uuid4()))
        for (
            minute,
            tenant,
            agent,
            fleet,
            op,
            kind,
            provider,
            model,
        ), counts in llm.items():
            batch.llm.append(
                {
                    "minute": minute,
                    "tenant_id": tenant,
                    "agent_id": agent,
                    "fleet_id": fleet,
                    "operation": op,
                    "kind": kind,
                    "provider": provider,
                    "model": model,
                    **dict(zip(_LLM_COUNTS, counts, strict=True)),
                }
            )
        for (minute, tenant, agent, fleet, op, metric), quantity in value.items():
            batch.value.append(
                {
                    "minute": minute,
                    "tenant_id": tenant,
                    "agent_id": agent,
                    "fleet_id": fleet,
                    "operation": op,
                    "metric": metric,
                    "quantity": quantity,
                }
            )
        return batch

    async def _filter(self, batch: _Batch) -> _Batch:
        """Drop rows for tenants that switched metering off."""
        if self._allow_tenants is None:
            return batch
        tenants = {r["tenant_id"] for r in [*batch.llm, *batch.value]}
        try:
            allowed = await self._allow_tenants(tenants)
        except Exception:
            # Can't tell: keep the rows. Metering on is the default, and a
            # count dropped by mistake can't be recovered later.
            logger.warning(
                "usage: could not resolve which tenants meter; keeping the batch",
                exc_info=True,
            )
            return batch
        allowed = allowed | {UNATTRIBUTED}
        batch.llm = [r for r in batch.llm if r["tenant_id"] in allowed]
        batch.value = [r for r in batch.value if r["tenant_id"] in allowed]
        return batch

    async def flush(self) -> int:
        """Send what waits, oldest first. Returns rows sent; a failed batch waits for the next flush."""
        drained = self._drain()
        if drained is not None:
            drained = await self._filter(drained)
            if len(drained):
                self._pending.append(drained)
        while len(self._pending) > MAX_PENDING_BATCHES:
            lost = self._pending.pop(0)
            self.dropped_rows += len(lost)
            logger.error(
                "usage: dropped a batch of %d rows after %d failed flushes (%d rows dropped since start)",
                len(lost),
                MAX_PENDING_BATCHES,
                self.dropped_rows,
            )
        sent = 0
        while self._pending:
            batch = self._pending[0]
            try:
                await self._post(batch.payload())
            except Exception:
                # Same batch id next time: storage counts it once even if this
                # attempt did land and only the reply was lost.
                logger.warning(
                    "usage: flush failed; %d batches waiting",
                    len(self._pending),
                    exc_info=True,
                )
                break
            self._pending.pop(0)
            sent += len(batch)
        return sent

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.flush()
            except Exception:  # pragma: no cover - defensive
                logger.exception("usage: flush loop error")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._loop(), name="usage-flush"
            )

    async def stop(self) -> None:
        """Stop the loop and make one last flush, so a clean shutdown loses nothing."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        try:
            await self.flush()
        except Exception:  # pragma: no cover - defensive
            logger.exception("usage: final flush failed")

    def snapshot(self) -> Iterable[tuple]:  # for tests and debugging
        with self._lock:
            return [*self._llm.items(), *self._value.items()]
