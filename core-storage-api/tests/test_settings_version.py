"""A live write is refused when the settings it was decided under have changed (g2.8).

core-api decides whether to hold a write from settings it caches per process, so
a write can be decided under settings another process has already changed. The
write says which settings it was decided under (``SETTINGS_VERSION_KEY``) and
storage compares that with the settings row in the transaction that inserts it
(``common.settings_version``).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from common.settings_version import NO_SETTINGS, SETTINGS_CHANGED, SETTINGS_VERSION_KEY
from core_storage_api.services import postgres_service
from core_storage_api.services.postgres_service import (
    PostgresService,
    SettingsChangedError,
    get_session,
)

pytestmark = pytest.mark.asyncio

_P = "/api/v1/storage"
_svc = PostgresService()


def _tenant() -> str:
    return f"t-setver-{uuid.uuid4().hex[:8]}"


def _memory(tenant: str, **extra) -> dict:
    return {
        "tenant_id": tenant,
        "agent_id": "low",
        "content": f"the deploy window moved to thursday {uuid.uuid4().hex}",
        "memory_type": "fact",
        "weight": 0.5,
        "metadata_": {},
        "status": "active",
        "visibility": "scope_team",
        "client_request_id": uuid.uuid4().hex,
        **extra,
    }


async def _hold_below(tenant: str, level: int) -> str:
    await _svc.organization_settings_update(
        org_id=tenant, new_settings={"quarantine": {"below_trust": level}}, changed_by="test"
    )
    _, version = await _svc.organization_settings_read(tenant)
    return version


async def _count(tenant: str) -> int:
    async with get_session() as session:
        return (
            await session.execute(text("SELECT count(*) FROM memories WHERE tenant_id = :t"), {"t": tenant})
        ).scalar_one()


async def _insert(path: str, row: dict) -> None:
    if path == "single":
        await _svc.memory_add(row)
    else:
        await _svc.memory_add_all([row])


# ── The version ──


async def test_a_tenant_with_no_settings_has_the_empty_version(_ensure_schema):
    assert await _svc.organization_settings_read(_tenant()) == ({}, NO_SETTINGS)


async def test_every_settings_change_gives_a_new_version(_ensure_schema):
    tenant = _tenant()
    first = await _hold_below(tenant, 2)
    second = await _hold_below(tenant, 3)

    assert NO_SETTINGS not in (first, second)
    assert first != second
    settings, version = await _svc.organization_settings_read(tenant)
    assert settings == {"quarantine": {"below_trust": 3}}
    assert version == second


async def test_the_settings_are_read_from_the_primary(_ensure_schema, monkeypatch):
    """core-api reloads through this right after a change; a replica may not have it yet.

    The writer service has a replica too (``READ_DATABASE_URL``), so asking the
    writer is not enough: this read must not take a reader session at all.
    """
    tenant = _tenant()
    version = await _hold_below(tenant, 2)

    def _no_replica():
        raise AssertionError("the settings were read from the replica")

    monkeypatch.setattr(postgres_service, "get_read_session", _no_replica)

    assert await _svc.organization_settings_read(tenant) == ({"quarantine": {"below_trust": 2}}, version)
    assert await _svc.organization_settings_get(tenant) == {"quarantine": {"below_trust": 2}}


async def test_the_route_returns_the_version(client):
    tenant = _tenant()
    version = await _hold_below(tenant, 2)

    resp = await client.get(f"{_P}/organization-settings/{tenant}")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"settings": {"quarantine": {"below_trust": 2}}, "version": version}


# ── The check ──


@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_a_write_decided_under_the_current_settings_is_written(_ensure_schema, path):
    tenant = _tenant()
    version = await _hold_below(tenant, 2)

    await _insert(path, _memory(tenant, **{SETTINGS_VERSION_KEY: version}))

    assert await _count(tenant) == 1


@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_a_write_decided_under_older_settings_is_refused(_ensure_schema, path):
    """The staging case: decided before the hold was tightened, written after."""
    tenant = _tenant()
    before = await _hold_below(tenant, 1)
    await _hold_below(tenant, 2)

    with pytest.raises(SettingsChangedError):
        await _insert(path, _memory(tenant, **{SETTINGS_VERSION_KEY: before}))

    assert await _count(tenant) == 0


@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_a_write_decided_before_any_settings_existed_is_refused(_ensure_schema, path):
    """The first settings a tenant saves must be able to hold its writes too."""
    tenant = _tenant()
    await _hold_below(tenant, 2)

    with pytest.raises(SettingsChangedError):
        await _insert(path, _memory(tenant, **{SETTINGS_VERSION_KEY: NO_SETTINGS}))

    assert await _count(tenant) == 0


@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_a_tenant_with_no_settings_writes_under_the_empty_version(_ensure_schema, path):
    tenant = _tenant()

    await _insert(path, _memory(tenant, **{SETTINGS_VERSION_KEY: NO_SETTINGS}))

    assert await _count(tenant) == 1


@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_a_write_that_claims_no_settings_is_not_checked(_ensure_schema, path):
    """A held write, the platform's own, and any write from an older core-api."""
    tenant = _tenant()
    await _hold_below(tenant, 2)

    await _insert(path, _memory(tenant))

    assert await _count(tenant) == 1


async def test_one_stale_row_refuses_the_whole_batch(_ensure_schema):
    tenant = _tenant()
    before = await _hold_below(tenant, 1)
    current = await _hold_below(tenant, 2)

    with pytest.raises(SettingsChangedError):
        await _svc.memory_add_all(
            [
                _memory(tenant, **{SETTINGS_VERSION_KEY: current}),
                _memory(tenant, **{SETTINGS_VERSION_KEY: before}),
            ]
        )

    assert await _count(tenant) == 0


@pytest.mark.parametrize("route", ["/memories", "/memories/bulk"])
async def test_the_routes_answer_409_saying_the_settings_changed(client, route):
    """A 409 that core-api can tell from a duplicate's, which is also a 409."""
    tenant = _tenant()
    before = await _hold_below(tenant, 1)
    await _hold_below(tenant, 2)
    row = _memory(tenant, **{SETTINGS_VERSION_KEY: before})

    resp = await client.post(f"{_P}{route}", json=row if route == "/memories" else [row])

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == SETTINGS_CHANGED
    assert await _count(tenant) == 0
