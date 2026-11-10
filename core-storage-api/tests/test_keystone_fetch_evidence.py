"""Install-bound fetch records through real migrations, HTTP and PostgreSQL.

Only fetch timestamps are synthetic. The database, insert-or-ignore behavior,
tenant predicates, interval matching and retention use production code.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio
BASE = "/api/v1/storage/keystone-fetches"


@pytest.fixture
async def binding():
    value = {
        "tenant_id": f"test-tenant-fetch-{uuid4().hex}",
        "install_uuid": str(uuid4()),
        "credential_id": str(uuid4()),
        "principal": "recipient-agent",
        "fleet_id": "recipient-fleet",
        "rule_set_hash": "a" * 64,
        "truncated": False,
    }
    yield value
    from core_storage_api.database.init import get_engine

    async with get_engine().begin() as connection:
        await connection.execute(
            text("DELETE FROM keystone_fetches WHERE tenant_id=:tenant"),
            {"tenant": value["tenant_id"]},
        )


async def _record(client, binding, at):
    response = await client.post(BASE, json={**binding, "served_at": at.isoformat()})
    assert response.status_code == 200, response.text
    return response.json()


def _check(binding, at):
    return {
        k: binding[k] for k in ("tenant_id", "install_uuid", "principal", "fleet_id", "rule_set_hash")
    } | {"delivered_at": at.isoformat()}


async def _matches(client, checks):
    response = await client.post(
        f"{BASE}/match", json={"tenant_id": checks[0]["tenant_id"], "checks": checks}
    )
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def test_migration_creates_the_record_table(_ensure_schema):
    from core_storage_api.database.init import get_engine

    async with get_engine().connect() as connection:
        table = await connection.scalar(text("SELECT to_regclass('public.keystone_fetches')::text"))
    assert table == "keystone_fetches", "keystone fetch persistence has not been migrated"


@pytest.mark.parametrize("fleet_id", ["recipient-fleet", None])
async def test_repeated_and_concurrent_records_keep_one_real_first_read(client, binding, fleet_id):
    from core_storage_api.database.init import get_engine

    binding = binding | {"fleet_id": fleet_id}
    first = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(minutes=2)
    await _record(client, binding, first)
    await asyncio.gather(*(_record(client, binding, first + timedelta(seconds=20)) for _ in range(5)))
    async with get_engine().connect() as connection:
        rows = (
            await connection.execute(
                text("SELECT credential_id, served_at FROM keystone_fetches WHERE tenant_id=:tenant"),
                {"tenant": binding["tenant_id"]},
            )
        ).all()
    assert len(rows) == 1
    assert str(rows[0].credential_id) == binding["credential_id"]
    assert rows[0].served_at == first


async def test_new_bucket_does_not_replace_the_previous_minute(client, binding):
    first = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(minutes=3)
    await _record(client, binding, first)
    await _record(client, binding, first + timedelta(minutes=1))
    results = await _matches(client, [_check(binding, first), _check(binding, first + timedelta(minutes=1))])
    assert [r["status"] for r in results] == ["matched", "matched"]
    assert results[0]["fetch_id"] != results[1]["fetch_id"]


@pytest.mark.parametrize("dimension", ["tenant_id", "install_uuid", "principal", "fleet_id", "rule_set_hash"])
async def test_no_cross_binding_match(client, binding, dimension):
    at = datetime.now(UTC) - timedelta(minutes=1)
    await _record(client, binding, at)
    other = {
        "tenant_id": "another-tenant",
        "install_uuid": str(uuid4()),
        "principal": "another-recipient",
        "fleet_id": "another-fleet",
        "rule_set_hash": "b" * 64,
    }
    [miss] = await _matches(client, [_check(binding | {dimension: other[dimension]}, at)])
    [hit] = await _matches(client, [_check(binding, at)])
    assert (miss["status"], hit["status"]) == ("no_matching_fetch", "matched")


@pytest.mark.parametrize("principal", [None, "recipient-agent"])
@pytest.mark.parametrize("first_fleet", [None, "first-fleet"])
async def test_each_fleet_keeps_its_own_same_minute_record(client, binding, principal, first_fleet):
    at = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(minutes=2)
    first = binding | {"principal": principal, "fleet_id": first_fleet}
    second = first | {"fleet_id": "second-fleet"}
    assert (await _record(client, first, at))["created"] is True
    assert (await _record(client, second, at))["created"] is True
    results = await _matches(client, [_check(first, at), _check(second, at)])
    assert [r["status"] for r in results] == ["matched", "matched"]
    assert results[0]["fetch_id"] != results[1]["fetch_id"]


async def test_other_fleets_cannot_fill_the_bounded_match_candidates(client, binding):
    from sqlalchemy.dialects.postgresql import insert

    from common.models.keystone_fetch import KeystoneFetch
    from core_storage_api.database.init import get_engine

    at = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(minutes=2)
    await _record(client, binding, at)
    # These rows would sort ahead of the eligible row and exhaust the
    # 800-candidate limit if fleet scope were filtered only after the query.
    async with get_engine().begin() as connection:
        await connection.execute(
            insert(KeystoneFetch),
            [
                {
                    **binding,
                    "install_uuid": UUID(binding["install_uuid"]),
                    "fleet_id": f"other-fleet-{i}",
                    "bucket_start": at.replace(second=0),
                    "served_at": at.replace(second=40),
                }
                for i in range(800)
            ],
        )
    [result] = await _matches(client, [_check(binding, at.replace(second=59))])
    assert result["status"] == "matched"
    assert datetime.fromisoformat(result["served_at"].replace("Z", "+00:00")) == at


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(0, "matched"), (300, "matched"), (390, "matched"), (421, "no_matching_fetch")],
)
async def test_fixed_window_includes_documented_bucket_slack(client, binding, offset, expected):
    # Bucket [12:00,12:01) intersects [delivery-360s,delivery]. A first
    # read at :05 may be returned when later reads in that same minute were
    # coalesced; the exact first served_at remains part of the evidence.
    at = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(hours=1)
    await _record(client, binding, at)
    [result] = await _matches(client, [_check(binding, at + timedelta(seconds=offset))])
    assert result["status"] == expected
    if expected == "matched":
        assert datetime.fromisoformat(result["served_at"].replace("Z", "+00:00")) == at


async def test_future_bucket_cannot_vouch_for_earlier_delivery(client, binding):
    at = datetime.now(UTC).replace(second=5, microsecond=0) - timedelta(minutes=2)
    await _record(client, binding, at)
    delivery = at.replace(second=0) - timedelta(microseconds=1)
    [result] = await _matches(client, [_check(binding, delivery)])
    assert result["status"] == "no_matching_fetch"


async def test_same_minute_fetch_after_delivery_cannot_vouch_for_it(client, binding):
    at = datetime.now(UTC).replace(second=40, microsecond=0) - timedelta(minutes=2)
    await _record(client, binding, at)
    before, equal = await _matches(
        client, [_check(binding, at - timedelta(microseconds=1)), _check(binding, at)]
    )
    assert before["status"] == "no_matching_fetch"
    assert "fetch_id" not in before
    assert equal["status"] == "matched"


async def test_retention_excludes_old_evidence_before_any_cleanup_job(client, binding):
    old = datetime.now(UTC) - timedelta(days=91)
    await _record(client, binding, old)
    [result] = await _matches(client, [_check(binding, old + timedelta(seconds=1))])
    assert result["status"] == "evidence_expired"
    assert "fetch_id" not in result


async def test_recent_evidence_is_usable_for_the_90_day_reporting_horizon(client, binding):
    at = datetime.now(UTC) - timedelta(days=89)
    await _record(client, binding, at)
    [result] = await _matches(client, [_check(binding, at + timedelta(seconds=1))])
    assert result["status"] == "matched"


@pytest.mark.parametrize("count", [0, 101])
async def test_storage_match_bounds_are_enforced_too(client, binding, count):
    at = datetime.now(UTC)
    response = await client.post(
        f"{BASE}/match",
        json={"tenant_id": binding["tenant_id"], "checks": [_check(binding, at)] * count},
    )
    assert response.status_code == 422, response.text


async def test_storage_rejects_a_caller_selected_window(client, binding):
    check = _check(binding, datetime.now(UTC)) | {"window_seconds": 86400}
    response = await client.post(f"{BASE}/match", json={"tenant_id": binding["tenant_id"], "checks": [check]})
    assert response.status_code == 422, response.text


async def test_storage_record_endpoint_requires_the_storage_service_secret(client, binding):
    response = await client.post(
        BASE,
        json={**binding, "served_at": datetime.now(UTC).isoformat()},
        headers={"X-Storage-Secret": "invalid-synthetic-storage-secret"},
    )
    assert response.status_code in (401, 403), response.text


async def test_storage_refuses_checks_outside_the_explicit_tenant(client, binding):
    response = await client.post(
        f"{BASE}/match",
        json={
            "tenant_id": binding["tenant_id"],
            "checks": [_check(binding | {"tenant_id": "other"}, datetime.now(UTC))],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize("body", [None, [], {}])
async def test_storage_match_requires_a_scoped_object(client, body):
    response = await client.post(f"{BASE}/match", json=body)
    assert response.status_code == 422, response.text


async def test_operator_purge_is_bounded_and_keeps_recent_records(client, binding):
    from core_storage_api.database.init import get_engine
    from core_storage_api.services.keystone_fetches import purge_expired_fetches

    now = datetime.now(UTC)
    old = now - timedelta(days=92)
    for i in range(3):
        await _record(client, binding, old + timedelta(minutes=i))
    await _record(client, binding, now)
    assert await purge_expired_fetches(limit=1) == 1
    async with get_engine().connect() as connection:
        times = (
            (
                await connection.execute(
                    text("SELECT served_at FROM keystone_fetches WHERE tenant_id=:tenant"),
                    {"tenant": binding["tenant_id"]},
                )
            )
            .scalars()
            .all()
        )
    assert len(times) == 3
    assert now in times


@pytest.mark.parametrize("limit", [0, 10001])
async def test_operator_purge_rejects_an_unbounded_batch(limit):
    from core_storage_api.services.keystone_fetches import purge_expired_fetches

    with pytest.raises(ValueError):
        await purge_expired_fetches(limit=limit)
