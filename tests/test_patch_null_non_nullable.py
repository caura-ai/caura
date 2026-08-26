"""``PATCH /memories/{id}`` with an explicit ``null`` on a NOT NULL column.

Every ``MemoryUpdate`` field is typed ``X | None`` — that is how an optional
PATCH field is spelled — so ``{"weight": null}`` passes validation and reaches
the service indistinguishable from a real value. The field-level constraints
(``min_length``, ``ge``/``le``, ``pattern``) bind only the non-None branch of
the union, so none of them rejects it either.

For the nine fields whose column is nullable that is a supported operation:
null clears the column, and the ``simple_fields`` loop writes the NULL
deliberately. For the five whose column is NOT NULL there is no way to honour
it, and before the guard each produced a 500 rather than a 4xx:

    content      TypeError: 'NoneType' object is not subscriptable
                 (``data.content[:200]`` while building the audit diff)
    memory_type  asyncpg NotNullViolationError
    weight       asyncpg NotNullViolationError
    status       asyncpg NotNullViolationError
    visibility   asyncpg NotNullViolationError

Reachable from any caller that serialises the whole schema rather than only the
fields it set; the plugin's update tool skips ``undefined`` but forwards
``null`` verbatim (``plugin/src/tool-definitions.ts``).

Nothing reported it: mypy flags the ``content`` one as
``Value of type "str | None" is not indexable  [index]``, but
``core-api/pyproject.toml`` carries ``ignore_errors = true`` for
``core_api.services.*``. The other four are runtime-only.
"""

import pytest

from common.models import Memory
from core_api.schemas import MemoryUpdate
from core_api.services.memory_service import NON_NULLABLE_UPDATE_FIELDS
from tests.conftest import get_test_auth, uid as _uid

# ``asyncio_mode = auto`` (pytest.ini) runs the async tests below without an
# explicit asyncio mark; adding one would also land on the sync drift test.
pytestmark = [pytest.mark.unit]


# Fields whose column IS nullable: an explicit null legitimately clears them and
# must keep working, so the guard cannot simply reject every null.
NULLABLE_UPDATE_FIELDS = (
    "title",
    "source_uri",
    "subject_entity_id",
    "predicate",
    "object_value",
    "ts_valid_start",
    "ts_valid_end",
    "expires_at",
)


async def _write_memory(client, tenant_id: str, headers: dict) -> dict:
    tag = _uid()
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant_id,
            "content": f"null-patch guard fixture [{tag}]",
            "agent_id": f"npg-agent-{tag}",
            "fleet_id": f"npg-fleet-{tag}",
            "memory_type": "fact",
        },
        headers=headers,
    )
    assert resp.status_code == 201, f"seed write failed: {resp.text}"
    return resp.json()


@pytest.mark.parametrize("field", NON_NULLABLE_UPDATE_FIELDS)
async def test_explicit_null_on_non_nullable_field_is_400(client, tenant_id, field):
    """400, not a 500 and not a silent no-op.

    Without the guard this does not merely return the wrong status — it raises
    out of the handler (TypeError for ``content``, NotNullViolationError for the
    other four), which is a 500 to the caller.
    """
    _, headers = get_test_auth(tenant_id)
    mem = await _write_memory(client, tenant_id, headers)

    resp = await client.patch(
        f"/api/v1/memories/{mem['id']}?tenant_id={tenant_id}",
        json={field: None},
        headers=headers,
    )

    assert resp.status_code == 400, f"{field}: expected 400, got {resp.status_code} {resp.text}"
    assert field in resp.json()["detail"], f"{field}: message should name the field — {resp.text}"


async def test_null_on_several_non_nullable_fields_names_all_of_them(client, tenant_id):
    """One request, one 400, every offending field named.

    A caller serialising the whole schema sends every null at once; reporting
    only the first would take them through the fix one round-trip at a time.
    """
    _, headers = get_test_auth(tenant_id)
    mem = await _write_memory(client, tenant_id, headers)

    resp = await client.patch(
        f"/api/v1/memories/{mem['id']}?tenant_id={tenant_id}",
        json={"content": None, "status": None, "weight": None},
        headers=headers,
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    for field in ("content", "status", "weight"):
        assert field in detail, f"{field} missing from {detail!r}"


@pytest.mark.parametrize("field", NULLABLE_UPDATE_FIELDS)
async def test_explicit_null_still_clears_a_nullable_field(client, tenant_id, field):
    """The guard must not over-reach: null-as-clear is the contract here."""
    _, headers = get_test_auth(tenant_id)
    mem = await _write_memory(client, tenant_id, headers)

    resp = await client.patch(
        f"/api/v1/memories/{mem['id']}?tenant_id={tenant_id}",
        json={field: None},
        headers=headers,
    )

    assert resp.status_code == 200, f"{field}: expected 200, got {resp.status_code} {resp.text}"
    assert resp.json()[field] is None


async def test_a_real_value_still_updates(client, tenant_id):
    """Guard fires on null only — a normal PATCH of the same fields still works."""
    _, headers = get_test_auth(tenant_id)
    mem = await _write_memory(client, tenant_id, headers)

    resp = await client.patch(
        f"/api/v1/memories/{mem['id']}?tenant_id={tenant_id}",
        json={"content": "replacement content", "weight": 0.9, "status": "active"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["content"] == "replacement content"
    assert body["weight"] == pytest.approx(0.9)


def test_guard_list_matches_the_model():
    """Drift guard — the reason the list can be a literal.

    Every ``MemoryUpdate`` field backed by a NOT NULL column must be listed, and
    nothing else may be. Add a NOT NULL column with a matching update field and
    this fails until ``NON_NULLABLE_UPDATE_FIELDS`` is extended, which is the
    only thing keeping the two in sync.
    """
    columns = Memory.__table__.columns
    expected = {
        name
        for name in MemoryUpdate.model_fields
        if name in columns and not columns[name].nullable
    }

    assert set(NON_NULLABLE_UPDATE_FIELDS) == expected, (
        "NON_NULLABLE_UPDATE_FIELDS has drifted from Memory's NOT NULL columns: "
        f"missing={sorted(expected - set(NON_NULLABLE_UPDATE_FIELDS))} "
        f"stale={sorted(set(NON_NULLABLE_UPDATE_FIELDS) - expected)}"
    )
