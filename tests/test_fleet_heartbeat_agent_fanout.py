"""The per-heartbeat agent refresh must not oversubscribe core-storage-api.

OSS 08/14 L-36. The refresh upserts one row per reported agent, and those
upserts were awaited one at a time — so a node reporting 40 agents paid 40
sequential storage round-trips on the highest-frequency call the API takes.

Making them concurrent is the easy half. The half that matters is WHERE the
budget lives: a semaphore constructed inside the handler gives every in-flight
heartbeat a fresh one and bounds nothing in aggregate, which is the shape
``routes/lifecycle.py`` documents as the cause of its 2026-09-15 incident.
core-storage-api runs a ten-connection pool with no reader/writer split, so
these refreshes compete with live writes for the same slots — see
``fleet._HEARTBEAT_AGENT_CONCURRENCY`` for the sizing argument and for what
this budget deliberately does NOT bound.

The budget and the list cap are one mechanism, not two: a shared budget with
no bound on the work a single caller may enqueue is a queue, not a cap.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core_api.routes import fleet

# ``asyncio_mode = auto`` in pytest.ini marks the coroutines; the sync reload
# test below must not inherit an asyncio marker.
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_budget():
    """Drop any per-loop budget before and after every test in this module.

    The event loop is session-scoped, so a budget patched down to 2 and left
    behind would be inherited by every later heartbeat test — failing
    somewhere else entirely. Autouse so a new test cannot forget it.

    Deliberately tolerant of the store being absent. Referencing a symbol the
    fix introduces would make these tests pass-or-AttributeError against the
    pre-fix shape, and an AttributeError proves only that a name exists — not
    that the calls are bounded, de-duplicated or capped. Tolerating its
    absence is what lets the SAME assertions run against both shapes and fail
    on the number.
    """
    from core_api.services import agent_service

    real_upsert = agent_service.get_or_create_agent

    cache = getattr(fleet, "_HEARTBEAT_AGENT_SEMAPHORES", None)
    if cache is not None:
        cache.clear()
    yield
    if cache is not None:
        cache.clear()

    # Fail HERE if a stub escaped, rather than in whatever unrelated file
    # happens to run next. A concurrent double-patch of one attribute once
    # leaked a fake ``get_or_create_agent`` out of this module and broke 113
    # tests across fourteen files, none of which mention the fleet.
    assert agent_service.get_or_create_agent is real_upsert, (
        "a patched get_or_create_agent escaped this test — see ``_patched``"
    )


def _body(agents: list[dict], node_name: str = "node-1") -> fleet.HeartbeatIn:
    return fleet.HeartbeatIn(tenant_id="t-1", node_name=node_name, agents=agents)


def _storage() -> MagicMock:
    sc = MagicMock()
    sc.upsert_node = AsyncMock(return_value={"id": "n-1", "node_name": "node-1"})
    sc.get_pending_commands = AsyncMock(return_value=[])
    sc.ack_commands = AsyncMock(return_value=None)
    return sc


@contextmanager
def _patched(upsert, sc: MagicMock | None = None):
    """Stub storage and the agent upsert for the duration of the block.

    Separate from ``_call`` on purpose. ``mock.patch`` is not safe to enter
    CONCURRENTLY on one attribute: two overlapping patches of the same target
    make the second save the FIRST one's stub as its "original", so unwinding
    in the other order restores the stub and leaks it past the test. Driving
    two heartbeats at once with a patch inside each is exactly that shape, and
    it left a fake ``get_or_create_agent`` installed in ``agent_service`` for
    the rest of the session — every test ordered after this file failed in
    ``resolve_write_agent``. Patch ONCE around the gather instead.
    """
    with (
        patch.object(
            fleet, "get_storage_client", MagicMock(return_value=sc or _storage())
        ),
        patch.object(fleet, "log_action", AsyncMock(return_value=None)),
        patch("core_api.services.agent_service.get_or_create_agent", upsert),
    ):
        yield


async def _call(body: fleet.HeartbeatIn):
    """Invoke the handler. Patch separately — see ``_patched``."""
    return await fleet.heartbeat(body, auth=MagicMock())


async def _run(body: fleet.HeartbeatIn, upsert, sc: MagicMock | None = None):
    """Single-heartbeat convenience: patch, then call."""
    with _patched(upsert, sc):
        return await _call(body)


def _tracker(delay: float = 0.01):
    """An upsert stub that records peak simultaneous in-flight calls."""
    state: dict = {"in_flight": 0, "peak": 0, "keys": [], "names": {}}

    async def _upsert(**kwargs: object) -> dict:
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        key = kwargs.get("agent_id")
        state["keys"].append(key)
        state["names"][key] = kwargs.get("display_name")
        try:
            await asyncio.sleep(delay)
            return {"agent_id": key}
        finally:
            state["in_flight"] -= 1

    return state, _upsert


# ── the fan-out, and where its budget lives ──────────────────────────────────


async def test_the_agent_upserts_actually_run_concurrently() -> None:
    """Sequential is the bug. One heartbeat's agents must overlap.

    Asserts PEAK simultaneous upserts is above one, which is the only number
    that separates a fan-out from the ``for`` loop this replaces — a budget
    that is merely present proves nothing if the calls are still awaited in
    series.
    """
    state, upsert = _tracker()
    agents = [{"agentId": f"a-{i}"} for i in range(6)]

    with patch.object(fleet, "_HEARTBEAT_AGENT_CONCURRENCY", 4, create=True):
        await _run(_body(agents), upsert)

    assert len(state["keys"]) == 6, "every reported agent must still be refreshed"
    assert state["peak"] > 1, (
        f"peak simultaneous upserts was {state['peak']} — the agents are still "
        "being awaited one at a time, so a node with 40 agents still pays 40 "
        "sequential round-trips per tick"
    )


async def test_the_agent_budget_is_shared_across_concurrent_heartbeats() -> None:
    """Two heartbeats in one worker share ONE budget, not one each.

    Heartbeats arrive continuously from every node, so the ceiling storage
    actually sees is the aggregate across everything in flight. With the
    budget built inside the handler that ceiling was
    ``in-flight heartbeats x _HEARTBEAT_AGENT_CONCURRENCY``, and the comment
    above it reasoned about a single request.

    Asserts on PEAK rather than on the semaphore object, so it fails for a
    per-request budget however that budget is spelled. Verified against that
    shape directly: a per-request ``Semaphore`` reaches 4 here.
    """
    state, upsert = _tracker()
    a = _body([{"agentId": f"a-{i}"} for i in range(6)], node_name="node-a")
    b = _body([{"agentId": f"b-{i}"} for i in range(6)], node_name="node-b")

    with (
        patch.object(fleet, "_HEARTBEAT_AGENT_CONCURRENCY", 2, create=True),
        _patched(upsert),
    ):
        await asyncio.gather(_call(a), _call(b))

    assert len(state["keys"]) == 12
    assert state["peak"] <= 2, (
        f"two concurrent heartbeats reached {state['peak']} simultaneous upserts "
        "against a budget of 2 — the cap is per-request, so N nodes reporting at "
        "once still oversubscribe a ten-connection pool by N times"
    )


async def test_one_failing_agent_does_not_drop_its_neighbours() -> None:
    """Per-agent isolation survives the fan-out.

    The node and command channel is the heartbeat's contract; the row refresh
    is best-effort. One bad agent must not take the other rows with it, and
    must not fail the request. (Preservation test — this held before the
    fan-out too, and must keep holding after it.)
    """
    seen: list[str] = []

    async def _upsert(**kwargs: object) -> dict:
        key = str(kwargs.get("agent_id"))
        if key == "a-2":
            raise RuntimeError("storage said no")
        seen.append(key)
        return {"agent_id": key}

    out = await _run(_body([{"agentId": f"a-{i}"} for i in range(4)]), _upsert)

    assert out is not None, "one failed agent must not fail the heartbeat"
    assert sorted(seen) == ["a-0", "a-1", "a-3"], (
        "the surviving agents must still have been refreshed"
    )


# ── de-duplication ───────────────────────────────────────────────────────────


async def test_a_repeated_agent_id_is_upserted_once() -> None:
    """A key repeated in one payload must not become two racing upserts.

    Sequentially this was self-correcting — the second pass read the row the
    first had just written. Concurrently it is two in-flight upserts of one
    row, each holding a slot from the shared budget and serialising on the
    same ``with_for_update`` re-SELECT at the far end.
    """
    state, upsert = _tracker(delay=0)
    agents = [
        {"agentId": "dup", "display_name": "first"},
        {"agentId": "other"},
        {"agentId": "dup", "display_name": "second"},
    ]

    await _run(_body(agents), upsert)

    assert sorted(state["keys"]) == ["dup", "other"], (
        f"expected one upsert per distinct agent, got {state['keys']}"
    )
    assert state["names"]["dup"] == "second", "a later name must still win"


async def test_a_repeat_without_a_name_keeps_the_one_already_sent() -> None:
    """De-duplication must not quietly drop a display_name.

    ``get_or_create_agent`` refreshes ``display_name`` only when the argument
    is not None, so the sequential loop applied "box" and then left it alone
    for the bare repeat. Collapsing to the LAST value would hand it None and
    erase the name — a behaviour change hidden inside a de-dup that is
    supposed to be transparent.
    """
    state, upsert = _tracker(delay=0)
    agents = [{"agentId": "a", "display_name": "box"}, {"agentId": "a"}]

    await _run(_body(agents), upsert)

    assert state["keys"] == ["a"]
    assert state["names"]["a"] == "box", (
        "the bare repeat overwrote a real display_name with None — the "
        "sequential loop it replaces would have kept it"
    )


# ── the list cap, which lives on the model ───────────────────────────────────


def test_an_oversized_agent_list_is_capped_on_the_model() -> None:
    """The bound belongs where the other free-form caps live.

    ``recall_metrics`` and ``reconcile`` are bounded by field_validators
    because an unbounded blob balloons the node row (eToro 2026-06-28);
    ``agents`` was the third such field and the only one with no bound.
    Truncating rather than dropping to a marker, because a marker would lose
    every agent instead of the overflow.
    """
    with patch.object(fleet, "_HEARTBEAT_AGENT_MAX", 4, create=True):
        hb = fleet.HeartbeatIn(
            tenant_id="t-1",
            node_name="n",
            agents=[{"agentId": f"a-{i}"} for i in range(10)],
        )

    assert hb.agents is not None
    assert len(hb.agents) == 4, f"the model must bound the list, got {len(hb.agents)}"


def test_a_normal_agent_list_passes_through_untouched() -> None:
    """The cap must not disturb the overwhelmingly common payload.

    The plugin synthesises a single ``main-{installId}`` agent unless an
    operator configures an explicit list, so N=1 is the dominant case.
    """
    agents = [{"agentId": "main-abc", "display_name": "laptop"}]
    hb = fleet.HeartbeatIn(tenant_id="t-1", node_name="n", agents=agents)
    assert hb.agents == agents


async def test_the_cap_also_bounds_what_is_written_to_the_node_row() -> None:
    """Capping only the work list would leave the stored row unbounded.

    ``body.agents`` is written wholesale to ``nodes.agents_json`` and read
    back out to the fleet view, so a cap that lived in the handler would bound
    the storage fan-out while the 10,000-entry payload still landed in the row
    every tick. Bounding at the model covers all three — fan-out, row, and the
    response built from it — with one mechanism.
    """
    _state, upsert = _tracker(delay=0)
    sc = _storage()

    with patch.object(fleet, "_HEARTBEAT_AGENT_MAX", 4, create=True):
        await _run(_body([{"agentId": f"a-{i}"} for i in range(10)]), upsert, sc=sc)

    written = sc.upsert_node.await_args.kwargs.get("agents_json")
    if written is None:  # positional call shape
        written = sc.upsert_node.await_args.args[0].get("agents_json")
    assert written is not None and len(written) == 4, (
        f"the node row stored {written if written is None else len(written)} "
        "agents — the cap bounds the fan-out but not the row it is written to"
    )


def test_a_bad_concurrency_value_does_not_take_down_the_api() -> None:
    """A mistyped tunable must degrade to the default, not kill core-api.

    ``core_api.app`` imports this module unconditionally, so an exception at
    import time here stops every route serving — memories, search, auth — over
    one fleet knob. The parse happens at module scope, so reloading under a
    junk value is the only way to reach it.

    Only the import-time property is asserted here. ``read_int_env``'s own
    fallback semantics (junk, zero, and the ``minimum`` floor) are covered by
    ``tests/test_env_utils.py``; re-asserting them per call site would give
    one helper three test homes.
    """
    try:
        with patch.dict(os.environ, {"FLEET_HEARTBEAT_AGENT_CONCURRENCY": "8o"}):
            importlib.reload(fleet)
        assert fleet._HEARTBEAT_AGENT_CONCURRENCY == 8, (
            "a junk value must fall back to the default, not propagate"
        )
    finally:
        os.environ.pop("FLEET_HEARTBEAT_AGENT_CONCURRENCY", None)
        importlib.reload(fleet)
