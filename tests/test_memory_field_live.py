"""An open map keeps up without reloading, and the layout refits as the tenant grows.

Unit tests against the in-memory storage of ``test_memory_field_build_log``:
``since`` returns only the memories written after a time, placed with the
saved model, and every response names the saved layout's version, which
changes only when a tenant that has grown by a quarter is refitted in the
background, while reads go on serving the layout they have.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from core_api.services import memory_field
from tests.test_memory_field_build_log import SCOPE, TENANT, _row, _Storage

pytestmark = pytest.mark.unit

LATER = "2026-11-11T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_slot(*_args, **_kwargs):
        yield

    monkeypatch.setattr(memory_field, "per_tenant_slot", no_slot)
    memory_field._reset_state()
    yield
    memory_field._reset_state()


def _newest(payload: dict) -> datetime:
    return max(datetime.fromisoformat(p["created_at"]) for p in payload["points"])


async def test_since_returns_only_the_memories_written_after_it(monkeypatch):
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    full = await memory_field._get_memory_field(SCOPE, limit=20000)
    version = storage.model["model_version"]
    assert full["layout_version"] == version
    assert full["truncated"] is False

    storage.rows[:0] = [_row(i, created_at=LATER) for i in (6, 7)]
    memory_field._signature_cache.clear()  # the five-second signature TTL has elapsed
    delta = await memory_field._get_memory_field(
        SCOPE, limit=20000, since=_newest(full)
    )
    assert {p["id"] for p in delta["points"]} == {"m-6", "m-7"}
    assert all(p["cluster"] >= 0 for p in delta["points"])
    assert (delta["layout_version"], delta["truncated"], delta["clusters"]) == (
        version,
        False,
        [],
    )
    assert delta["total"] == 8
    # Placed with the saved model and saved, so the next full read finds them.
    assert ("m-6", version) in storage.positions and (
        "m-7",
        version,
    ) in storage.positions


async def test_since_says_when_it_holds_only_part_of_what_is_new(monkeypatch):
    monkeypatch.setattr(memory_field, "SINCE_MAX", 1)
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    full = await memory_field._get_memory_field(SCOPE, limit=20000)

    storage.rows[:0] = [_row(i, created_at=LATER) for i in (6, 7)]
    delta = await memory_field._get_memory_field(
        SCOPE, limit=20000, since=_newest(full)
    )
    assert len(delta["points"]) == 1
    assert delta["truncated"] is True


async def test_since_on_a_map_laid_out_on_request_asks_for_a_reload(monkeypatch):
    """Too few embeddings to fit: positions are not saved, so new points
    cannot be merged into the open map; ``truncated`` says to reload."""
    storage = _Storage([_row(i) for i in range(3)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    full = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert full["layout_version"] is None

    storage.rows[:0] = [_row(3, created_at=LATER)]
    delta = await memory_field._get_memory_field(
        SCOPE, limit=20000, since=_newest(full)
    )
    assert (delta["layout_version"], delta["truncated"], delta["points"]) == (
        None,
        True,
        [],
    )


async def test_a_tenant_grown_by_a_quarter_is_refitted_while_reads_serve_the_old_layout(
    monkeypatch,
):
    monkeypatch.setattr(memory_field, "REFIT_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(memory_field, "REFIT_MIN_NEW", 1)
    storage = _Storage([_row(i) for i in range(8)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    first = (await memory_field._get_memory_field(SCOPE, limit=20000))["layout_version"]

    storage.rows[:0] = [_row(i, created_at=LATER) for i in (8, 9)]  # 10 = 8 x 1.25
    memory_field._signature_cache.clear()
    during = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert during["layout_version"] == first
    assert len(during["points"]) == 10
    async with asyncio.timeout(10):
        await asyncio.gather(*memory_field._fits.values())
    assert storage.model["model_version"] != first
    assert storage.model["n_fit"] == 10

    memory_field._signature_cache.clear()
    after = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert after["layout_version"] == storage.model["model_version"]


async def test_growth_short_of_a_quarter_keeps_the_layout(monkeypatch):
    monkeypatch.setattr(memory_field, "REFIT_CHECK_SECONDS", 0.0)
    monkeypatch.setattr(memory_field, "REFIT_MIN_NEW", 1)
    storage = _Storage([_row(i) for i in range(8)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    first = (await memory_field._get_memory_field(SCOPE, limit=20000))["layout_version"]

    storage.rows[:0] = [_row(8, created_at=LATER)]  # 9 < 10
    memory_field._signature_cache.clear()
    after = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert after["layout_version"] == first
    assert not memory_field._fits
    assert storage.claims == 1


async def test_the_growth_check_runs_once_per_interval(monkeypatch):
    """Reading a tenant-wide count costs a signature scan, so a tenant's
    growth is checked at most once every ``REFIT_CHECK_SECONDS``."""
    storage = _Storage([_row(i) for i in range(8)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    person = {"tenant_id": TENANT, "include_scope_agent": True}
    await memory_field._get_memory_field(person, limit=20000)
    scans = storage.signatures
    for _ in range(3):
        memory_field.field_cache.clear()
        memory_field._signature_cache.clear()
        await memory_field._get_memory_field(person, limit=20000)
    # One scan of the caller's own scope per read, and no tenant-wide one.
    assert storage.signatures == scans + 3


async def test_a_busy_growth_check_does_not_fail_the_read(monkeypatch):
    monkeypatch.setattr(memory_field, "REFIT_CHECK_SECONDS", 0.0)
    storage = _Storage([_row(i) for i in range(8)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    first = await memory_field._get_memory_field(SCOPE, limit=20000)
    real = memory_field._signature

    async def busy_for_the_tenant_wide_scope(scope):
        if set(scope) == {"tenant_id"}:
            raise memory_field.FieldBusyError
        return await real(scope)

    monkeypatch.setattr(memory_field, "_signature", busy_for_the_tenant_wide_scope)
    memory_field.field_cache.clear()
    again = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert again["layout_version"] == first["layout_version"]


def _waiting(i: int, created_at: str) -> dict:
    """A memory written but not embedded yet."""
    return {**_row(i, created_at=created_at), "embedding_b64": None}


async def test_a_memory_returned_before_its_embedding_lands_comes_back_placed(
    monkeypatch,
):
    """A memory written moments before a poll usually has no embedding yet,
    so it comes back unplaced. ``next_since`` stops short of it, so the next
    poll returns it again, placed."""
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    full = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert datetime.fromisoformat(full["next_since"]) == _newest(full)

    just_now = datetime.now(UTC).isoformat()
    storage.rows[:0] = [_waiting(6, just_now)]
    memory_field._signature_cache.clear()
    delta = await memory_field._get_memory_field(
        SCOPE, limit=20000, since=datetime.fromisoformat(full["next_since"])
    )
    assert [(p["id"], p["cluster"]) for p in delta["points"]] == [("m-6", -1)]
    assert datetime.fromisoformat(delta["next_since"]) < datetime.fromisoformat(
        just_now
    )

    storage.rows[0] = _row(6, created_at=just_now)  # its embedding lands
    memory_field._signature_cache.clear()
    memory_field.field_cache.clear()
    again = await memory_field._get_memory_field(
        SCOPE, limit=20000, since=datetime.fromisoformat(delta["next_since"])
    )
    assert [p["id"] for p in again["points"]] == ["m-6"]
    assert again["points"][0]["cluster"] >= 0
    assert datetime.fromisoformat(again["next_since"]) == datetime.fromisoformat(
        just_now
    )


async def test_a_full_read_stops_short_of_a_memory_waiting_for_its_embedding(
    monkeypatch,
):
    just_now = datetime.now(UTC).isoformat()
    storage = _Storage([_waiting(6, just_now)] + [_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    full = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert full["points"][0]["cluster"] == -1
    assert datetime.fromisoformat(full["next_since"]) < datetime.fromisoformat(just_now)


async def test_a_memory_still_unplaced_after_the_wait_no_longer_holds_the_map_back(
    monkeypatch,
):
    """An embedding that never lands must not pin every poll to that memory."""
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    full = await memory_field._get_memory_field(SCOPE, limit=20000)

    long_ago = datetime.now(UTC) - timedelta(
        seconds=memory_field.UNPLACED_WAIT_SECONDS + 60
    )
    storage.rows[:0] = [_waiting(6, long_ago.isoformat())]
    memory_field._signature_cache.clear()
    delta = await memory_field._get_memory_field(
        SCOPE, limit=20000, since=_newest(full)
    )
    assert [(p["id"], p["cluster"]) for p in delta["points"]] == [("m-6", -1)]
    assert datetime.fromisoformat(delta["next_since"]) == long_ago
