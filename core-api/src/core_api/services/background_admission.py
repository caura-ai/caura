"""Bounded, round-robin admission across background tenants on one event loop."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque

from common.background_work import BackgroundWorkDeferred


class Permit:
    def __init__(self, gate: FairGate, tenant: str) -> None:
        self.gate, self.tenant, self.released = gate, tenant, False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.gate.active -= 1
            left = self.gate.by_tenant[self.tenant] - 1
            if left:
                self.gate.by_tenant[self.tenant] = left
            else:
                del self.gate.by_tenant[self.tenant]
            self.gate.dispatch()


class FairGate:
    def __init__(self, cap: int, per_tenant: int, tenant_backlog: int, global_backlog: int) -> None:
        self.cap, self.per_tenant = cap, per_tenant
        self.tenant_backlog, self.global_backlog = tenant_backlog, global_backlog
        self.active = 0
        self.by_tenant: dict[str, int] = {}
        self.pending: OrderedDict[str, deque[asyncio.Future[Permit]]] = OrderedDict()
        self.queued = 0

    def take(self, tenant: str) -> Permit:
        self.active += 1
        self.by_tenant[tenant] = self.by_tenant.get(tenant, 0) + 1
        return Permit(self, tenant)

    async def acquire(self, tenant: str) -> Permit:
        # Serve eligible queued tenants first. If capacity remains, the queued
        # tenants are at their own caps; they must not block a fresh tenant.
        self.dispatch()
        if self.active < self.cap and self.by_tenant.get(tenant, 0) < self.per_tenant:
            return self.take(tenant)
        if self.queued >= self.global_backlog or len(self.pending.get(tenant, ())) >= self.tenant_backlog:
            raise BackgroundWorkDeferred("backlog_full")
        future: asyncio.Future[Permit] = asyncio.get_running_loop().create_future()
        self.pending.setdefault(tenant, deque()).append(future)
        self.queued += 1
        self.dispatch()
        try:
            return await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                future.result().release()
            else:
                queue = self.pending.get(tenant)
                if queue is not None and future in queue:
                    queue.remove(future)
                    self.queued -= 1
                    if not queue:
                        del self.pending[tenant]
                self.dispatch()
            raise

    def dispatch(self) -> None:
        while self.active < self.cap and self.pending:
            admitted = False
            for tenant in list(self.pending):
                if self.active >= self.cap:
                    break
                if self.by_tenant.get(tenant, 0) >= self.per_tenant:
                    self.pending.move_to_end(tenant)
                    continue
                queue = self.pending[tenant]
                future = queue.popleft()
                self.queued -= 1
                if queue:
                    self.pending.move_to_end(tenant)
                else:
                    del self.pending[tenant]
                if not future.cancelled():
                    future.set_result(self.take(tenant))
                    admitted = True
            if not admitted:
                break
