"""The per-tick lease: one aligned tick runs once across N replicas.

Covers the guard from both ends. The scheduler must skip a tick it does
not own, and must run the tick anyway whenever the lease cannot give a
clear answer — because a coordinator outage skipping the nightly sweep
is a worse failure than the duplicate fire the lease prevents.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core_operations.scheduler import Scheduler


def _aligned(delay: float = 0.0):
    """A delay_provider that fires once immediately, then parks.

    Returning a large second value rather than looping keeps the test to
    exactly one tick without needing to cancel mid-assertion.
    """
    calls = 0

    def provider() -> float:
        nonlocal calls
        calls += 1
        return delay if calls == 1 else 3600.0

    return provider


async def _run_one_tick(s: Scheduler) -> None:
    await s.start()
    # Long enough for the 0-delay sleep, the lease round trip and fn(),
    # all of which are in-process here.
    await asyncio.sleep(0.05)
    await s.stop()


@pytest.mark.asyncio
async def test_denied_lease_skips_the_tick_entirely() -> None:
    """A replica that loses the lease must not call fn at all.

    This is the whole point: at maxScale=2 both replicas reach the same
    wall-clock second, and prod measured all nine scheduled endpoints
    firing twice per tick. The loser must do nothing, not do it later.
    """
    s = Scheduler()
    ran = 0

    async def tick() -> None:
        nonlocal ran
        ran += 1

    s.register("lifecycle-crystallize", 24 * 3600, tick, delay_provider=_aligned())
    s.set_lease(AsyncMock(return_value=False))
    await _run_one_tick(s)

    assert ran == 0, "a denied lease must skip the tick, not defer or run it"


@pytest.mark.asyncio
async def test_granted_lease_runs_the_tick() -> None:
    s = Scheduler()
    ran = 0

    async def tick() -> None:
        nonlocal ran
        ran += 1

    s.register("lifecycle-crystallize", 24 * 3600, tick, delay_provider=_aligned())
    s.set_lease(AsyncMock(return_value=True))
    await _run_one_tick(s)

    assert ran == 1


@pytest.mark.asyncio
async def test_only_one_of_two_schedulers_runs_the_same_tick() -> None:
    """Two Scheduler instances sharing one lease model two replicas.

    Asserts the end-to-end property rather than the call: whatever the
    lease is spelled like, exactly one side must do the work.
    """
    held: set[str] = set()

    async def lease(task: str, ttl_s: float) -> bool:
        if task in held:
            return False
        held.add(task)
        return True

    ran = 0

    async def tick() -> None:
        nonlocal ran
        ran += 1

    schedulers = []
    for _ in range(2):
        s = Scheduler()
        s.register("lifecycle-archive-expired", 24 * 3600, tick, delay_provider=_aligned())
        s.set_lease(lease)
        schedulers.append(s)

    await asyncio.gather(*(s.start() for s in schedulers))
    await asyncio.sleep(0.05)
    await asyncio.gather(*(s.stop() for s in schedulers))

    assert ran == 1, f"two replicas ran the same tick {ran} times; expected exactly 1"


@pytest.mark.asyncio
async def test_lease_that_raises_still_runs_the_tick() -> None:
    """Fail OPEN. An unreachable coordinator must not skip the sweep."""
    s = Scheduler()
    ran = 0

    async def tick() -> None:
        nonlocal ran
        ran += 1

    async def broken_lease(task: str, ttl_s: float) -> bool:
        raise RuntimeError("core-api unreachable")

    s.register("lifecycle-insights", 24 * 3600, tick, delay_provider=_aligned())
    s.set_lease(broken_lease)
    await _run_one_tick(s)

    assert ran == 1, "a raising lease must fail open, not silently skip the tick"


@pytest.mark.asyncio
async def test_no_lease_configured_runs_every_tick() -> None:
    """Backwards compatible: an unleased scheduler behaves as before."""
    s = Scheduler()
    ran = 0

    async def tick() -> None:
        nonlocal ran
        ran += 1

    s.register("embedding-coverage", 3600, tick, delay_provider=_aligned())
    await _run_one_tick(s)

    assert ran == 1


@pytest.mark.asyncio
async def test_lease_ttl_never_outlives_the_next_tick() -> None:
    """TTL is min(default, interval/2), so it expires before the next tick.

    A lease that outlived its own cadence would suppress REAL work rather
    than duplicate work — the one way this guard could cause an outage
    rather than prevent one. Asserted against a cadence short enough that
    the 300s default would be wrong, so a regression to a constant fails.
    """
    s = Scheduler()
    seen: list[float] = []

    async def lease(task: str, ttl_s: float) -> bool:
        seen.append(ttl_s)
        return True

    async def tick() -> None:
        return None

    s.register("fast-task", 60, tick, delay_provider=_aligned())
    s.set_lease(lease)
    await _run_one_tick(s)

    assert seen, "the lease was never consulted"
    assert seen[0] == 30.0, (
        f"TTL {seen[0]}s for a 60s cadence — a lease must expire before the "
        "task's next tick or it suppresses real work"
    )


@pytest.mark.asyncio
async def test_interval_tasks_are_not_leased() -> None:
    """Only aligned ticks contend for a shared instant.

    An interval task fires at startup then drifts by fn duration, so
    replicas never converge on one moment; leasing it by name would just
    make the first replica the permanent winner and silence the others.
    """
    s = Scheduler()
    consulted = 0

    async def lease(task: str, ttl_s: float) -> bool:
        nonlocal consulted
        consulted += 1
        return True

    async def tick() -> None:
        return None

    s.register("interval-task", 3600, tick)  # no delay_provider
    s.set_lease(lease)
    await _run_one_tick(s)

    assert consulted == 0, "interval tasks must not consult the lease"


@pytest.mark.asyncio
async def test_set_lease_after_start_is_refused() -> None:
    """Same latch as register: configuration happens before start."""
    s = Scheduler()
    await s.start()
    try:
        with pytest.raises(RuntimeError, match="after scheduler has started"):
            s.set_lease(AsyncMock(return_value=True))
    finally:
        await s.stop()


# ── the HTTP client ──────────────────────────────────────────────────


def _resp(status: int, body: object, *, raises: bool = False) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text = "body"
    if raises:
        r.json = MagicMock(side_effect=ValueError("not json"))
    else:
        r.json = MagicMock(return_value=body)
    return r


def _client_yielding(resp_or_exc) -> MagicMock:
    client = MagicMock()
    if isinstance(resp_or_exc, Exception):
        client.post = AsyncMock(side_effect=resp_or_exc)
    else:
        client.post = AsyncMock(return_value=resp_or_exc)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "outcome"),
    [
        ("transport error", httpx.ConnectError("refused")),
        ("non-2xx", _resp(503, {})),
        ("unparseable body", _resp(200, None, raises=True)),
        ("missing granted", _resp(200, {})),
        ("non-boolean granted", _resp(200, {"granted": "no"})),
    ],
)
async def test_lease_client_fails_open(label: str, outcome: object) -> None:
    """Every ambiguous answer means run.

    Parametrised rather than written five times because the property is
    one property: only an explicit ``false`` may stop a tick.
    """
    from core_operations import lease as lease_mod

    with patch.object(lease_mod.httpx, "AsyncClient", MagicMock(return_value=_client_yielding(outcome))):
        assert await lease_mod.claim_tick_lease("lifecycle-crystallize", 300.0) is True, (
            f"{label} must fail open"
        )


@pytest.mark.asyncio
async def test_lease_client_honours_an_explicit_denial() -> None:
    """The one case that returns False — otherwise the guard does nothing."""
    from core_operations import lease as lease_mod

    with patch.object(
        lease_mod.httpx,
        "AsyncClient",
        MagicMock(return_value=_client_yielding(_resp(200, {"granted": False}))),
    ):
        assert await lease_mod.claim_tick_lease("lifecycle-crystallize", 300.0) is False


@pytest.mark.asyncio
async def test_lease_client_sends_task_and_ttl() -> None:
    from core_operations import lease as lease_mod

    ctx = _client_yielding(_resp(200, {"granted": True}))
    with patch.object(lease_mod.httpx, "AsyncClient", MagicMock(return_value=ctx)):
        await lease_mod.claim_tick_lease("lifecycle-entity-link", 120.0)

    client = await ctx.__aenter__()
    body = client.post.await_args.kwargs["json"]
    assert body == {"task": "lifecycle-entity-link", "ttl_s": 120.0}
    assert client.post.await_args.args[0].endswith("/api/v1/admin/scheduler/tick-lease")
