"""M-114: ``skills_factory.sentinel.fail_on_critical`` is gone.

It was declared in ``DEFAULT_SETTINGS`` (default ``true``), type-checked as a
bool and returned by ``GET /settings``, and read by nothing: a critical Sentinel
finding always quarantines the doc. A tenant that set it to ``false`` so critical
findings would reach the inbox still had every such skill quarantined, while the
settings kept reporting ``false``. Human triage exists per skill instead: approve
with ``override_quarantine`` releases one quarantined skill, recorded with the
reason and the approver.

Removed rather than wired (Eldad, 2026-10-06), as ``llm_tokens_per_run`` was
(oss-0922-l-05). A value a tenant stored before the removal stays in its row, so
the settings view drops it too.
"""

import uuid

import pytest

from core_api.clients.storage_client import get_storage_client
from tests.conftest import get_test_auth

_REMOVED_KEY = "fail_on_critical"


def _new_tenant() -> tuple[str, dict]:
    return get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")


@pytest.mark.unit
def test_the_settings_key_is_gone() -> None:
    from core_api.services.organization_settings import _LEAF_TYPES, DEFAULT_SETTINGS

    assert _REMOVED_KEY not in DEFAULT_SETTINGS["skills_factory"]["sentinel"]
    assert f"skills_factory.sentinel.{_REMOVED_KEY}" not in _LEAF_TYPES


async def test_a_write_to_the_removed_key_is_rejected(client) -> None:
    tenant_id, headers = _new_tenant()

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"skills_factory": {"sentinel": {_REMOVED_KEY: False}}},
        headers=headers,
    )

    assert resp.status_code == 422, resp.text
    assert _REMOVED_KEY in resp.text, "the rejection does not name the key"


async def test_a_value_stored_before_the_removal_is_not_shown(client) -> None:
    tenant_id, headers = _new_tenant()
    # The row as a write before the removal left it: storage keeps what core-api
    # validated, and core-api accepted the key then.
    await get_storage_client().update_org_settings(
        tenant_id, {"skills_factory": {"sentinel": {_REMOVED_KEY: False}}}
    )

    resp = await client.get(f"/api/v1/settings?tenant_id={tenant_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    assert _REMOVED_KEY not in resp.json()["skills_factory"]["sentinel"]


async def test_auto_promote_clean_still_writes(client) -> None:
    """The control: the rest of the sentinel block still writes and reads back."""
    tenant_id, headers = _new_tenant()

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"skills_factory": {"sentinel": {"auto_promote_clean": True}}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["skills_factory"]["sentinel"]["auto_promote_clean"] is True
