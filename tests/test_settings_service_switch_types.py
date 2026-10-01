"""Per-service on/off switches must be type-checked on a settings write.

``enrichment.enabled``, ``recall.enabled``, ``recall.premise_guard``,
``entity_extraction.enabled`` and the ``agent_digest`` leaves were absent from
``_LEAF_TYPES``, so a string ``"false"`` was stored as-is, rendered back to the
tenant as off, and resolved TRUTHY in every consumer: an off switch that did
not switch anything off.
"""

import pytest

from core_api.services.organization_settings import (
    DEFAULT_SETTINGS,
    _check_keys,
    _validate_leaf_types,
)

_SWITCHES = [
    ("enrichment", "enabled"),
    ("recall", "enabled"),
    ("recall", "premise_guard"),
    ("entity_extraction", "enabled"),
    ("agent_digest", "enabled"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("section", "key"), _SWITCHES)
@pytest.mark.parametrize("bad", ["false", "true", 0, 1])
def test_a_non_bool_switch_value_is_rejected(section, key, bad):
    payload = {section: {key: bad}}
    _check_keys(payload, DEFAULT_SETTINGS)

    with pytest.raises(ValueError, match=f"{section}.{key}"):
        _validate_leaf_types(payload)


@pytest.mark.unit
@pytest.mark.parametrize(("section", "key"), _SWITCHES)
@pytest.mark.parametrize("good", [True, False, None])
def test_bool_and_reset_values_are_accepted(section, key, good):
    _validate_leaf_types({section: {key: good}})


@pytest.mark.unit
@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("top_n", "25"),
        ("max_cost_per_run_usd", "2.0"),
        ("retention_days", True),
        ("cadence", 1),
    ],
)
def test_agent_digest_leaves_are_type_checked(key, bad):
    with pytest.raises(ValueError, match=f"agent_digest.{key}"):
        _validate_leaf_types({"agent_digest": {key: bad}})


@pytest.mark.unit
def test_agent_digest_defaults_pass_their_own_validation():
    """The table must agree with the shipped defaults, or a tenant echoing
    them back would be refused."""
    _validate_leaf_types({"agent_digest": DEFAULT_SETTINGS["agent_digest"]})


async def test_a_string_false_switch_is_refused_by_the_settings_put(client):
    from tests.conftest import get_test_auth
    from tests.conftest import uid as _uid

    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"enrichment": {"enabled": "false"}},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["enrichment"]["enabled"] is None
