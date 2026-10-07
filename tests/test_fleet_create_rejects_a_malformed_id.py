"""POST /fleet answers a malformed fleet_id with 422, not 500 (M-31).

``FleetCreateIn.validate_fleet_id`` was a bare classmethod, so model validation
never ran it. The handler called it by hand after the auth gates, and its
``ValueError`` reached the catch-all handler: a 500 ``INTERNAL_ERROR`` naming no
field, logged as an exception on every attempt, where every other malformed body
gets a 422.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core_api.routes.fleet import FleetCreateIn
from tests.conftest import get_admin_headers

MALFORMED = ["my_fleet", "ab", "-leading", "x" * 51]


@pytest.mark.parametrize("fleet_id", MALFORMED)
async def test_a_malformed_fleet_id_is_a_422_naming_the_field(client, fleet_id):
    resp = await client.post(
        "/api/v1/fleet",
        json={"tenant_id": "default", "fleet_id": fleet_id},
        headers=get_admin_headers(),
    )
    assert resp.status_code == 422, resp.text
    assert "fleet_id" in resp.text


@pytest.mark.parametrize("fleet_id", MALFORMED)
def test_the_model_refuses_a_malformed_fleet_id(fleet_id):
    with pytest.raises(ValidationError, match="fleet_id"):
        FleetCreateIn(tenant_id="default", fleet_id=fleet_id)


@pytest.mark.parametrize("fleet_id", ["abc", "sales-eu-2", "A" * 50])
def test_a_well_formed_fleet_id_is_accepted(fleet_id):
    """Control: the rule itself is unchanged, 3-50 alphanumerics and hyphens."""
    assert FleetCreateIn(tenant_id="default", fleet_id=fleet_id).fleet_id == fleet_id
