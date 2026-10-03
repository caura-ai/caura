"""Provider keys saved before encryption can be encrypted in place (M-99).

core-api encrypts ``api_keys`` on every save since caura PR #1769, but a key
saved before then stays as submitted until its tenant saves settings again.
Storage cannot encrypt, since it does not hold ``SETTINGS_ENCRYPTION_KEY``.
core-api encrypts a tenant's keys when it loads them, and storage offers the
swap: it replaces a key only while it still holds the value core-api read, so a
tenant's newer save is never overwritten with an older key.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from common.models.organization_settings import OrganizationSettingsAudit
from core_storage_api.services.postgres_service import get_session
from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio


async def _save(client: AsyncClient, org: str, settings: dict, changed_by: str = "seed") -> None:
    resp = await client.post(
        f"{PREFIX}/organization-settings/{org}", json={"settings": settings, "changed_by": changed_by}
    )
    assert resp.status_code == 200, resp.text


async def _org(client: AsyncClient, settings: dict) -> str:
    org = f"test-tenant-{uuid.uuid4().hex[:8]}"
    await _save(client, org, settings)
    return org


async def _swap(client: AsyncClient, org: str, expected: dict, encrypted: dict) -> list[str]:
    resp = await client.post(
        f"{PREFIX}/organization-settings/{org}/encrypt-api-keys",
        json={"expected": expected, "encrypted": encrypted, "changed_by": "system:test"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["swapped"]


async def _stored_keys(client: AsyncClient, org: str) -> dict:
    resp = await client.get(f"{PREFIX}/organization-settings/{org}")
    assert resp.status_code == 200, resp.text
    return resp.json()["settings"].get("api_keys", {})


async def test_a_key_is_swapped_only_while_it_still_holds_the_value_read(client: AsyncClient) -> None:
    org = await _org(client, {"api_keys": {"openai_api_key": "sk-old", "anthropic_api_key": "sk-ant"}})
    # The tenant saves a new key after core-api read the old one.
    await _save(client, org, {"api_keys": {"openai_api_key": "sk-new"}}, changed_by="tenant")

    swapped = await _swap(
        client,
        org,
        {"openai_api_key": "sk-old", "anthropic_api_key": "sk-ant"},
        {"openai_api_key": "enc:v1:old", "anthropic_api_key": "enc:v1:ant"},
    )

    assert swapped == ["anthropic_api_key"]
    assert await _stored_keys(client, org) == {"openai_api_key": "sk-new", "anthropic_api_key": "enc:v1:ant"}


async def test_the_swap_is_audited_masked_and_runs_once(client: AsyncClient) -> None:
    org = await _org(client, {"api_keys": {"openai_api_key": "sk-old"}})
    expected, encrypted = {"openai_api_key": "sk-old"}, {"openai_api_key": "enc:v1:x"}

    assert await _swap(client, org, expected, encrypted) == ["openai_api_key"]
    assert await _swap(client, org, expected, encrypted) == []

    stmt = select(OrganizationSettingsAudit.diff).where(
        OrganizationSettingsAudit.org_id == org, OrganizationSettingsAudit.changed_by == "system:test"
    )
    async with get_session() as s:
        diffs = (await s.execute(stmt)).scalars().all()
    assert list(diffs) == [{"api_keys.openai_api_key": ["****", "****"]}]


async def test_the_swap_never_stores_a_value_without_the_encrypted_prefix(client: AsyncClient) -> None:
    org = await _org(client, {"api_keys": {"openai_api_key": "sk-old"}})

    assert await _swap(client, org, {"openai_api_key": "sk-old"}, {"openai_api_key": "sk-other"}) == []

    assert await _stored_keys(client, org) == {"openai_api_key": "sk-old"}
