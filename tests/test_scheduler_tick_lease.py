"""The tick-lease endpoint: first caller wins, everyone else is told so.

The scheduler side of this guard lives in core-operations and is covered
by ``core-operations/tests/test_scheduler_lease.py``. These cover the
half core-api owns: the decision itself, the validation that keeps a
task name from escaping its Redis namespace, and the TTL arithmetic that
stops a lease outliving the tick it guards.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


def _request(body: object, *, malformed: bool = False) -> MagicMock:
    req = MagicMock()
    if malformed:
        req.json = AsyncMock(side_effect=ValueError("not json"))
    else:
        req.json = AsyncMock(return_value=body)
    return req


def _auth() -> MagicMock:
    auth = MagicMock()
    auth.enforce_admin = MagicMock(return_value=None)
    return auth


@pytest.mark.unit
@pytest.mark.asyncio
async def test_first_caller_is_granted_and_second_is_denied() -> None:
    """The whole contract, exercised against one shared fake Redis.

    Asserts on the two answers rather than on the call, so it holds
    however the key is spelled.
    """
    from core_api.routes import scheduler_lease

    held: set[str] = set()

    async def fake_set_nx(key: str, value: str, ttl: int) -> bool:
        if key in held:
            return False
        held.add(key)
        return True

    with patch.object(scheduler_lease, "cache_set_nx", fake_set_nx):
        first = await scheduler_lease.claim_tick_lease(
            _request({"task": "lifecycle-crystallize", "ttl_s": 300}), auth=_auth()
        )
        second = await scheduler_lease.claim_tick_lease(
            _request({"task": "lifecycle-crystallize", "ttl_s": 300}), auth=_auth()
        )

    assert first == {"task": "lifecycle-crystallize", "granted": True}
    assert second == {"task": "lifecycle-crystallize", "granted": False}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_different_task_is_not_blocked_by_its_neighbour() -> None:
    """Leases are per task. Six actions share the 02:00 slot in prod; a
    key that collided across them would silence five real sweeps."""
    from core_api.routes import scheduler_lease

    held: set[str] = set()

    async def fake_set_nx(key: str, value: str, ttl: int) -> bool:
        if key in held:
            return False
        held.add(key)
        return True

    with patch.object(scheduler_lease, "cache_set_nx", fake_set_nx):
        a = await scheduler_lease.claim_tick_lease(
            _request({"task": "lifecycle-crystallize", "ttl_s": 300}), auth=_auth()
        )
        b = await scheduler_lease.claim_tick_lease(
            _request({"task": "lifecycle-entity-link", "ttl_s": 300}), auth=_auth()
        )

    assert a["granted"] is True
    assert b["granted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_admin_is_enforced_before_anything_else() -> None:
    """A lease is a write to shared state; it must not be open."""
    from core_api.routes import scheduler_lease

    auth = _auth()
    auth.enforce_admin = MagicMock(side_effect=HTTPException(status_code=403))

    with patch.object(
        scheduler_lease, "cache_set_nx", AsyncMock(return_value=True)
    ) as nx:
        with pytest.raises(HTTPException) as exc:
            await scheduler_lease.claim_tick_lease(
                _request({"task": "lifecycle-crystallize", "ttl_s": 300}), auth=auth
            )

    assert exc.value.status_code == 403
    nx.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("missing task", {"ttl_s": 300}),
        ("non-string task", {"task": 7, "ttl_s": 300}),
        ("uppercase task", {"task": "Lifecycle-Crystallize", "ttl_s": 300}),
        ("namespace escape", {"task": "a:b", "ttl_s": 300}),
        ("whitespace in task", {"task": "a b", "ttl_s": 300}),
        ("newline in task", {"task": "a\nb", "ttl_s": 300}),
        ("missing ttl", {"task": "lifecycle-crystallize"}),
        ("string ttl", {"task": "lifecycle-crystallize", "ttl_s": "300"}),
        ("bool ttl", {"task": "lifecycle-crystallize", "ttl_s": True}),
        ("zero ttl", {"task": "lifecycle-crystallize", "ttl_s": 0}),
        ("negative ttl", {"task": "lifecycle-crystallize", "ttl_s": -5}),
        ("ttl over the ceiling", {"task": "lifecycle-crystallize", "ttl_s": 901}),
    ],
)
async def test_bad_input_is_refused_without_touching_redis(
    label: str, body: dict
) -> None:
    """422, and crucially no write.

    The task name is concatenated into a Redis key, so a name carrying a
    colon, a space or a newline would write outside this endpoint's
    namespace or log as a different key than it wrote. ``bool`` is called
    out separately because it is an ``int`` subclass — ``True`` would
    otherwise sail through as a 1-second TTL rather than be rejected.
    """
    from core_api.routes import scheduler_lease

    with patch.object(
        scheduler_lease, "cache_set_nx", AsyncMock(return_value=True)
    ) as nx:
        with pytest.raises(HTTPException) as exc:
            await scheduler_lease.claim_tick_lease(_request(body), auth=_auth())

    assert exc.value.status_code == 422, label
    nx.assert_not_awaited(), f"{label} must be refused before any Redis write"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_json_is_422_not_500() -> None:
    from core_api.routes import scheduler_lease

    with patch.object(scheduler_lease, "cache_set_nx", AsyncMock(return_value=True)):
        with pytest.raises(HTTPException) as exc:
            await scheduler_lease.claim_tick_lease(
                _request(None, malformed=True), auth=_auth()
            )

    assert exc.value.status_code == 422


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], None, 42, "lifecycle-crystallize", True])
async def test_valid_json_that_is_not_an_object_is_422_not_500(body: object) -> None:
    """Valid JSON is not necessarily a JSON *object*.

    ``[]``, ``null``, ``42``, ``"x"`` and ``true`` all parse cleanly, so
    the malformed-body guard never fires; reading ``.get`` off any of them
    is an ``AttributeError``. That is the same 500 the guard exists to
    prevent, arriving one line later by a different route.
    """
    from core_api.routes import scheduler_lease

    with patch.object(
        scheduler_lease, "cache_set_nx", AsyncMock(return_value=True)
    ) as nx:
        with pytest.raises(HTTPException) as exc:
            await scheduler_lease.claim_tick_lease(_request(body), auth=_auth())

    assert exc.value.status_code == 422
    nx.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fractional_ttl_rounds_up_never_down() -> None:
    """Redis SETEX takes whole seconds.

    Rounding 1.2 down to 1 would expire the lease early and re-admit the
    duplicate this endpoint exists to block, so the rounding direction is
    load-bearing rather than cosmetic.
    """
    from core_api.routes import scheduler_lease

    nx = AsyncMock(return_value=True)
    with patch.object(scheduler_lease, "cache_set_nx", nx):
        await scheduler_lease.claim_tick_lease(
            _request({"task": "fast-task", "ttl_s": 1.2}), auth=_auth()
        )

    ttl = nx.await_args.args[2]
    assert ttl == 2, f"1.2s became {ttl}s; a rounded-down lease expires early"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_key_is_namespaced_per_task() -> None:
    """Guards against a future edit keying on something global."""
    from core_api.routes import scheduler_lease

    nx = AsyncMock(return_value=True)
    with patch.object(scheduler_lease, "cache_set_nx", nx):
        await scheduler_lease.claim_tick_lease(
            _request({"task": "agent-digest-weekly", "ttl_s": 300}), auth=_auth()
        )

    key = nx.await_args.args[0]
    assert key.endswith("agent-digest-weekly")
    assert key != "agent-digest-weekly", "the key must carry a namespace prefix"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redis_outage_grants_the_lease() -> None:
    """Fail-open reaches the caller, not just ``cache_set_nx``.

    ``cache_set_nx`` already returns True when Redis is down; this asserts
    the endpoint passes that through as ``granted`` rather than reading it
    as a denial somewhere in between. Denying on outage would let one
    Redis blip silently stop every nightly sweep in the estate — strictly
    worse than the duplicate fire this prevents.
    """
    from core_api.routes import scheduler_lease

    with patch.object(scheduler_lease, "cache_set_nx", AsyncMock(return_value=True)):
        out = await scheduler_lease.claim_tick_lease(
            _request({"task": "lifecycle-archive-expired", "ttl_s": 300}), auth=_auth()
        )

    assert out["granted"] is True
