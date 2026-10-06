"""core-api's shutdown settles the event bus first (M-09).

Cloud Run allows 10 s between SIGTERM and SIGKILL. core-api hosts the lifecycle
pipeline consumers, and its bus stopped consuming only in ``event_bus.stop()``,
the sixth shutdown step, after the tracked-task drain and three flushes bounded
at 5 s each. Until then its pull loops kept taking lifecycle runs, and a run
still in flight when the SIGKILL landed was never cancelled, so its audit row
stayed claimed for the 60-minute lease.

``_shut_down`` starts ``stop_consuming()`` first, alongside the other early
steps, and waits for it before the flushes, so whatever the settled handlers
logged still reaches the audit queue.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from common.events.inprocess import InProcessEventBus
from core_api import app as core_app

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _step(order: list[str], name: str):
    async def step(*_args, **_kwargs) -> None:
        order.append(name)

    return step


class _Bus:
    """Records the bus's shutdown calls. Settling waits for the tracked-task
    drain to begin, so a shutdown that ran the two one after the other would
    never finish."""

    def __init__(self, order: list[str], tasks_started: asyncio.Event) -> None:
        self.order = order
        self.tasks_started = tasks_started
        self.consuming_started = asyncio.Event()

    async def release_broadcast_subscriptions(self) -> None:
        self.order.append("release")

    async def stop_consuming(self) -> None:
        self.order.append("stop_consuming")
        self.consuming_started.set()
        await self.tasks_started.wait()
        self.order.append("consuming_stopped")

    async def stop(self) -> None:
        self.order.append("bus.stop")


@pytest.fixture
def order(monkeypatch) -> list[str]:
    calls: list[str] = []
    storage = SimpleNamespace(close=_step(calls, "storage.close"))
    monkeypatch.setattr(core_app, "get_storage_client", lambda: storage)
    return calls


async def test_the_bus_settles_alongside_the_task_drain_and_before_the_flushes(
    order, monkeypatch
):
    shut_down = core_app._shut_down
    tasks_started = asyncio.Event()
    bus = _Bus(order, tasks_started)

    async def cancel_all_tasks() -> None:
        order.append("tasks")
        tasks_started.set()
        await bus.consuming_started.wait()
        order.append("tasks_done")

    monkeypatch.setattr(core_app, "cancel_all_tasks", cancel_all_tasks)

    await asyncio.wait_for(
        shut_down(
            bus,
            audit_queue=SimpleNamespace(stop=_step(order, "audit_queue")),
            capability_usage_agg=None,
            usage_meter=SimpleNamespace(stop=_step(order, "usage_meter")),
        ),
        timeout=2.0,
    )

    assert order.index("release") < order.index("tasks")
    assert order.index("consuming_stopped") < order.index("audit_queue")
    assert order[-4:] == ["audit_queue", "usage_meter", "bus.stop", "storage.close"]


async def test_a_bus_that_fails_to_settle_does_not_skip_the_rest(order, monkeypatch):
    shut_down = core_app._shut_down

    class _Wedged(_Bus):
        async def stop_consuming(self) -> None:
            raise RuntimeError("pull loop wedged")

    monkeypatch.setattr(core_app, "cancel_all_tasks", _step(order, "tasks"))

    await asyncio.wait_for(
        shut_down(
            _Wedged(order, asyncio.Event()),
            audit_queue=None,
            capability_usage_agg=None,
            usage_meter=SimpleNamespace(stop=_step(order, "usage_meter")),
        ),
        timeout=2.0,
    )

    assert order == ["release", "tasks", "usage_meter", "bus.stop", "storage.close"]


async def test_an_in_process_bus_has_nothing_to_settle(order, monkeypatch):
    """The OSS default: the base class's no-op, so standalone shutdown is as
    before."""
    shut_down = core_app._shut_down
    monkeypatch.setattr(core_app, "cancel_all_tasks", _step(order, "tasks"))

    await asyncio.wait_for(
        shut_down(
            InProcessEventBus(),
            audit_queue=None,
            capability_usage_agg=None,
            usage_meter=SimpleNamespace(stop=_step(order, "usage_meter")),
        ),
        timeout=2.0,
    )

    assert order == ["tasks", "usage_meter", "storage.close"]
