"""deployment_id / deployment_token: stored once, race-safe, rotatable."""

from __future__ import annotations

import uuid

import pytest

from core_api.heartbeat import identity
from core_api.heartbeat.identity import (
    DEPLOYMENT_ORG_ID,
    DeploymentIdentity,
    generate,
    load,
    load_or_create,
    rotate,
)

pytestmark = [pytest.mark.unit]


class _FakeOrgSettings:
    """Enough of the storage client's org-settings surface for the identity module."""

    def __init__(self, initial: dict | None = None):
        self.rows: dict[str, dict] = {}
        if initial is not None:
            self.rows[DEPLOYMENT_ORG_ID] = dict(initial)
        self.writes: list[tuple[str, dict, str | None]] = []

    async def get_org_settings(self, org_id: str) -> dict:
        return dict(self.rows.get(org_id, {}))

    async def update_org_settings(
        self, org_id: str, settings: dict, *, changed_by=None
    ) -> dict:
        self.writes.append((org_id, dict(settings), changed_by))
        merged = {**self.rows.get(org_id, {}), **settings}
        self.rows[org_id] = merged
        return {"settings": merged, "changed": True}


def test_generate_shape():
    ident = generate()
    uuid.UUID(ident.deployment_id)
    assert len(ident.deployment_token) == 64
    bytes.fromhex(ident.deployment_token)
    assert generate().deployment_id != ident.deployment_id


async def test_first_boot_creates_and_persists():
    sc = _FakeOrgSettings()
    ident = await load_or_create(sc)
    assert sc.rows[DEPLOYMENT_ORG_ID] == ident.as_settings()
    assert sc.writes[0][0] == DEPLOYMENT_ORG_ID
    assert sc.writes[0][2] == identity.CHANGED_BY
    again = await load_or_create(sc)
    assert again == ident
    assert len(sc.writes) == 1


async def test_load_returns_none_when_absent():
    assert await load(_FakeOrgSettings()) is None


@pytest.mark.parametrize(
    "stored",
    [
        {"deployment_id": "not-a-uuid", "deployment_token": "ab" * 32},
        {"deployment_id": str(uuid.uuid4()), "deployment_token": "short"},
        {"deployment_id": str(uuid.uuid4()), "deployment_token": "zz" * 32},
        {"deployment_id": str(uuid.uuid4())},
        {"deployment_token": "ab" * 32},
        {"deployment_id": 42, "deployment_token": "ab" * 32},
    ],
)
async def test_malformed_row_is_replaced(stored):
    sc = _FakeOrgSettings(stored)
    ident = await load_or_create(sc)
    assert sc.rows[DEPLOYMENT_ORG_ID]["deployment_id"] == ident.deployment_id
    assert len(sc.writes) == 1


async def test_first_boot_race_adopts_the_stored_id():
    """Two replicas write; whoever reads a different id than it wrote adopts it."""
    winner = generate()

    class _Racing(_FakeOrgSettings):
        async def update_org_settings(self, org_id, settings, *, changed_by=None):
            await super().update_org_settings(org_id, settings, changed_by=changed_by)
            # The other replica's write lands between our write and our read.
            self.rows[org_id] = winner.as_settings()
            return {"settings": self.rows[org_id], "changed": True}

    sc = _Racing()
    ident = await load_or_create(sc)
    assert ident == winner
    assert ident.deployment_id != sc.writes[0][1]["deployment_id"]


async def test_read_after_write_failure_falls_back_to_what_was_written():
    class _Unreadable(_FakeOrgSettings):
        async def get_org_settings(self, org_id):
            return {}

    sc = _Unreadable()
    ident = await load_or_create(sc)
    assert ident.as_settings() == sc.writes[0][1]


async def test_rotate_replaces_both_values():
    sc = _FakeOrgSettings()
    first = await load_or_create(sc)
    second = await rotate(sc)
    assert second.deployment_id != first.deployment_id
    assert second.deployment_token != first.deployment_token
    assert sc.rows[DEPLOYMENT_ORG_ID] == second.as_settings()
    assert await load(sc) == second


def test_identity_is_not_derived_from_the_host():
    import platform
    import socket

    ident = generate()
    for hostlike in (socket.gethostname(), platform.node()):
        if hostlike:
            assert hostlike not in ident.deployment_id
            assert hostlike not in ident.deployment_token
    assert isinstance(ident, DeploymentIdentity)
