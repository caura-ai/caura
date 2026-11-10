"""Shared active-call limits and retry-generation protection, against PostgreSQL."""

from __future__ import annotations

from uuid import uuid4

import pytest

from core_storage_api.config import settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
_PREFIX = "/api/v1/storage/background-work"


async def _post(client, path, body):
    response = await client.post(_PREFIX + path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_active_calls_share_a_cap_and_release_does_not_refund_usage(client, monkeypatch):
    monkeypatch.setattr(settings, "background_llm_concurrency", 1)
    monkeypatch.setattr(settings, "background_llm_attempts_per_minute", 2)
    tenant, attempt = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "attempt_id": attempt, "operation": "enrichment"}
    assert (await _post(client, "/reserve", body))["allowed"]
    denied = await _post(client, "/reserve", {**body, "attempt_id": str(uuid4())})
    assert not denied["allowed"] and denied["reason"] == "tenant_slots_busy"
    await _post(client, "/release", {"tenant_id": tenant, "attempt_id": attempt})
    second = str(uuid4())
    assert (await _post(client, "/reserve", {**body, "attempt_id": second}))["allowed"]
    await _post(client, "/release", {"tenant_id": tenant, "attempt_id": second})
    denied = await _post(client, "/reserve", {**body, "attempt_id": str(uuid4())})
    assert not denied["allowed"] and denied["retry_after_seconds"] > 0


async def test_a_foreign_tenant_cannot_release_a_slot(client, monkeypatch):
    monkeypatch.setattr(settings, "background_llm_concurrency", 1)
    tenant, attempt = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "attempt_id": attempt, "operation": "enrichment"}
    await _post(client, "/reserve", body)
    await _post(client, "/release", {"tenant_id": f"other-{uuid4()}", "attempt_id": attempt})
    assert not (await _post(client, "/reserve", {**body, "attempt_id": str(uuid4())}))["allowed"]


async def test_deferral_is_coalesced_and_a_stale_claim_cannot_remove_it(client):
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    tenant, memory = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "kind": "extraction", "memory_id": memory}
    await _post(client, "/defer", body)
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE background_model_work SET available_at = now() - interval '1 second' "
                "WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant},
        )
    [claim] = (await _post(client, "/claim", {"tenant_id": tenant}))["items"]
    assert set(claim) == {"tenant_id", "kind", "memory_id", "generation", "attempts"}
    await _post(client, "/defer", body)
    await _post(client, "/finish", {**body, "generation": claim["generation"]})
    async with get_session() as session:
        generation = (
            await session.execute(
                text("SELECT generation FROM background_model_work WHERE tenant_id = :tenant_id"),
                {"tenant_id": tenant},
            )
        ).scalar_one()
    assert generation > claim["generation"]


async def test_deferred_requests_reject_content_fields(client):
    response = await client.post(
        _PREFIX + "/defer",
        json={
            "tenant_id": f"budget-{uuid4()}",
            "memory_id": str(uuid4()),
            "kind": "extraction",
            "content": "must never enter the retry store",
        },
    )
    assert response.status_code == 422
    assert "must never enter the retry store" not in response.text


async def test_only_attempts_in_the_last_sixty_seconds_consume_the_allowance(client, monkeypatch):
    from sqlalchemy import text

    from core_storage_api.services.postgres_service import get_session

    monkeypatch.setattr(settings, "background_llm_attempts_per_minute", 2)
    tenant = f"budget-{uuid4()}"
    for _ in range(2):
        assert (
            await _post(
                client,
                "/reserve",
                {
                    "tenant_id": tenant,
                    "attempt_id": str(uuid4()),
                    "operation": "enrichment",
                },
            )
        )["allowed"]
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE background_model_reservations SET admitted_at = now() - interval '61 seconds', "
                "released = true WHERE tenant_id = :tenant_id"
            ),
            {"tenant_id": tenant},
        )
    assert (
        await _post(
            client,
            "/reserve",
            {
                "tenant_id": tenant,
                "attempt_id": str(uuid4()),
                "operation": "contradiction",
            },
        )
    )["allowed"]


async def _retry_row(tenant, memory, **values):
    from sqlalchemy import select, update

    from common.models.background_work import BackgroundModelWork as Work
    from core_storage_api.services.postgres_service import get_session

    async with get_session() as session:
        if values:
            await session.execute(
                update(Work).where(Work.tenant_id == tenant, Work.memory_id == memory).values(**values)
            )
        return (
            await session.execute(select(Work).where(Work.tenant_id == tenant, Work.memory_id == memory))
        ).scalar_one_or_none()


@pytest.mark.parametrize("attempts,delay", [(1, 60), (2, 120), (7, 3600)])
async def test_retry_backoff_grows_with_claim_count(client, attempts, delay):
    tenant, memory = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"}
    await _post(client, "/defer", body)
    row = await _retry_row(tenant, memory, attempts=attempts)
    await _post(client, "/finish", {**body, "generation": row.generation, "retry": True})
    after = await _retry_row(tenant, memory)
    assert delay - 1 <= (after.available_at - after.lease_until).total_seconds() <= delay


async def test_retry_cap_parks_only_the_matching_generation_and_logs_identifiers(client, caplog):
    tenant, memory = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"}
    await _post(client, "/defer", body)
    row = await _retry_row(tenant, memory, attempts=8)
    generation = row.generation
    await _post(client, "/defer", body)
    await _post(client, "/finish", {**body, "generation": generation, "retry": True})
    after = await _retry_row(tenant, memory)
    assert after is not None and after.generation > generation
    assert after.terminal_reason is None
    await _post(client, "/finish", {**body, "generation": after.generation, "retry": True})
    parked = await _retry_row(tenant, memory)
    assert parked.terminal_reason == "retry_exhausted" and parked.attempts == 8
    await _post(client, "/finish", {**body, "generation": after.generation, "retry": False})
    assert (await _retry_row(tenant, memory)).terminal_reason == "retry_exhausted"
    records = [r for r in caplog.records if getattr(r, "reason", None) == "retry_exhausted"]
    assert len(records) == 1
    assert (records[0].tenant_id, records[0].kind, records[0].memory_id) == (tenant, "extraction", memory)


async def test_an_expired_crashed_claim_cannot_exceed_the_replay_cap(client):
    from datetime import UTC, datetime, timedelta

    tenant, memory = f"budget-{uuid4()}", str(uuid4())
    await _post(client, "/defer", {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"})
    past = datetime.now(UTC) - timedelta(seconds=1)
    await _retry_row(tenant, memory, attempts=8, available_at=past, lease_until=past)
    assert (await _post(client, "/claim", {"tenant_id": tenant}))["items"] == []
    assert (await _retry_row(tenant, memory)).terminal_reason == "retry_exhausted"
    assert (await _post(client, "/claim", {"tenant_id": tenant}))["items"] == []


async def test_a_new_deferral_explicitly_reopens_parked_work_without_accepting_stale_finish(client):
    from datetime import UTC, datetime, timedelta

    tenant, memory = f"~park-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"}
    await _post(client, "/defer", body)
    past = datetime.now(UTC) - timedelta(seconds=1)
    old = await _retry_row(tenant, memory, attempts=8, available_at=past, lease_until=past)
    await _post(client, "/finish", {**body, "generation": old.generation, "retry": True})
    assert (await _retry_row(tenant, memory)).terminal_reason == "retry_exhausted"
    due = await client.get(
        "/api/v1/storage/tenants/active",
        params={
            "background_due": True,
            "after_tenant": tenant[:-1],
            "limit": 32,
        },
    )
    assert tenant not in due.json()["tenant_ids"]
    await _post(client, "/defer", body)
    reopened = await _retry_row(tenant, memory)
    assert reopened.terminal_reason is None and reopened.attempts == 0
    assert reopened.generation > old.generation
    await _post(client, "/finish", {**body, "generation": old.generation, "retry": False})
    assert await _retry_row(tenant, memory) is not None
    await _retry_row(tenant, memory, available_at=past, lease_until=past)
    [claim] = (await _post(client, "/claim", {"tenant_id": tenant}))["items"]
    assert claim["attempts"] == 1


async def test_due_discovery_excludes_future_and_leased_work_and_pages_distinct_tenants(client):
    from datetime import UTC, datetime, timedelta

    prefix = f"~due-{uuid4()}"
    now = datetime.now(UTC)
    for suffix, leased, future in [
        ("a", False, False),
        ("b", True, False),
        ("c", False, True),
        ("d", False, False),
    ]:
        tenant = f"{prefix}-{suffix}"
        for _ in range(2):
            memory = str(uuid4())
            await _post(client, "/defer", {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"})
            await _retry_row(
                tenant,
                memory,
                available_at=now + timedelta(seconds=60 if future else -1),
                lease_until=now + timedelta(seconds=60 if leased else -1),
            )
    path = "/api/v1/storage/tenants/active"
    first = await client.get(path, params={"background_due": True, "after_tenant": prefix, "limit": 1})
    assert first.status_code == 200
    assert first.json()["tenant_ids"] == [prefix + "-a"]
    second = await client.get(
        path, params={"background_due": True, "after_tenant": prefix + "-a", "limit": 1}
    )
    assert second.json()["tenant_ids"] == [prefix + "-d"]
    assert (await client.get(path, params={"background_due": True, "limit": 33})).status_code == 422


@pytest.mark.parametrize("order", ["release_first", "reserve_first", "concurrent"])
async def test_release_closes_late_reservations_without_refunding_admitted_attempts(
    client, monkeypatch, order
):
    import asyncio

    from sqlalchemy import select

    from common.models.background_work import BackgroundModelReservation as Reservation
    from core_storage_api.services.postgres_service import get_session

    monkeypatch.setattr(settings, "background_llm_concurrency", 1)
    monkeypatch.setattr(settings, "background_llm_attempts_per_minute", 2)
    tenant, attempt = f"budget-{uuid4()}", str(uuid4())
    reserve = {"tenant_id": tenant, "attempt_id": attempt, "operation": "enrichment"}
    release = {"tenant_id": tenant, "attempt_id": attempt}
    if order == "release_first":
        await _post(client, "/release", release)
        assert not (await _post(client, "/reserve", reserve))["allowed"]
    elif order == "reserve_first":
        assert (await _post(client, "/reserve", reserve))["allowed"]
        await _post(client, "/release", release)
    else:
        await asyncio.gather(_post(client, "/reserve", reserve), _post(client, "/release", release))
    denied = await _post(client, "/reserve", reserve)
    assert denied["allowed"] is False and denied["reason"] == "reservation_released"
    async with get_session() as session:
        row = (
            await session.execute(
                select(Reservation).where(Reservation.tenant_id == tenant, Reservation.attempt_id == attempt)
            )
        ).scalar_one()
        assert row.released
        admitted = row.admitted_at is not None
    # Tombstones spend nothing; a real admission stays spent after release.
    second = str(uuid4())
    assert (await _post(client, "/reserve", {**reserve, "attempt_id": second}))["allowed"]
    last = str(uuid4())
    # Free the active slot, preserving the minute's accounting.
    await _post(client, "/release", {"tenant_id": tenant, "attempt_id": second})
    assert (await _post(client, "/reserve", {**reserve, "attempt_id": last}))["allowed"] is (not admitted)


async def test_batched_claims_are_bounded_and_do_not_repeat_leased_work(client):
    from datetime import UTC, datetime, timedelta

    tenant = f"budget-{uuid4()}"
    past = datetime.now(UTC) - timedelta(seconds=1)
    for _ in range(10):
        memory = str(uuid4())
        await _post(client, "/defer", {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"})
        await _retry_row(tenant, memory, available_at=past, lease_until=past)
    first = (await _post(client, "/claim", {"tenant_id": tenant, "limit": 4}))["items"]
    second = (await _post(client, "/claim", {"tenant_id": tenant, "limit": 4}))["items"]
    assert len(first) == len(second) == 4
    assert len({work["memory_id"] for work in first + second}) == 8
    assert all(work["tenant_id"] == tenant and work["attempts"] == 1 for work in first + second)
    for invalid in (0, 9):
        assert (
            await client.post(_PREFIX + "/claim", json={"tenant_id": tenant, "limit": invalid})
        ).status_code == 422


async def test_scheduling_deferrals_survive_many_claims_and_finish_is_idempotent(client):
    from datetime import UTC, datetime, timedelta

    tenant, memory = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"}
    await _post(client, "/defer", body)
    for _ in range(10):
        past = datetime.now(UTC) - timedelta(seconds=1)
        await _retry_row(tenant, memory, available_at=past, lease_until=past)
        [claim] = (await _post(client, "/claim", {"tenant_id": tenant}))["items"]
        finish = {
            **body,
            "generation": claim["generation"],
            "retry": True,
            "deferred": True,
            "retry_after_seconds": 3,
        }
        await _post(client, "/finish", finish)
        once = await _retry_row(tenant, memory)
        await _post(client, "/finish", finish)
        twice = await _retry_row(tenant, memory)
        assert once.attempts == twice.attempts == 0
        assert once.generation == twice.generation > claim["generation"]
        assert once.available_at == twice.available_at
        assert 2 <= (once.available_at - once.lease_until).total_seconds() <= 3
        assert twice.terminal_reason is None


async def test_deferral_preserves_prior_failures_and_cannot_refund_a_newer_claim(client):
    from datetime import UTC, datetime, timedelta

    tenant, memory = f"budget-{uuid4()}", str(uuid4())
    body = {"tenant_id": tenant, "memory_id": memory, "kind": "extraction"}
    await _post(client, "/defer", body)
    past = datetime.now(UTC) - timedelta(seconds=1)
    await _retry_row(tenant, memory, attempts=7, available_at=past, lease_until=past)
    [claim] = (await _post(client, "/claim", {"tenant_id": tenant}))["items"]
    assert claim["attempts"] == 8
    finish = {
        **body,
        "generation": claim["generation"],
        "retry": True,
        "deferred": True,
        "retry_after_seconds": 1,
    }
    await _post(client, "/finish", finish)
    assert (await _retry_row(tenant, memory)).attempts == 7
    await _retry_row(tenant, memory, available_at=past, lease_until=past)
    [newer] = (await _post(client, "/claim", {"tenant_id": tenant}))["items"]
    await _post(client, "/finish", finish)
    assert (await _retry_row(tenant, memory)).attempts == 8
    await _post(client, "/finish", {**body, "generation": newer["generation"], "retry": True})
    assert (await _retry_row(tenant, memory)).terminal_reason == "retry_exhausted"


@pytest.mark.parametrize("after", [None, "tenant-cursor"])
async def test_due_discovery_can_seek_each_tenant_through_the_migrated_index(_ensure_schema, after):
    from core_storage_api.routers.background_work import _due_tenants_query
    from tests.conftest import plan_with_only_index

    plan = await plan_with_only_index(
        _due_tenants_query(after, 32),
        "ix_background_model_work_tenant_due",
        table="background_model_work",
    )
    assert "Recursive Union" in plan, plan
    assert "Seq Scan" not in plan and "Unique" not in plan, plan
    assert plan.count("ix_background_model_work_tenant_due") == 2, plan
    assert any("tenant_id" in line for line in plan.splitlines() if "Index Cond" in line), plan


async def test_due_discovery_skips_duplicate_backlogs_and_preserves_byte_order(client):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert

    from common.models.background_work import BackgroundModelWork as Work
    from core_storage_api.services.postgres_service import get_session

    prefix = f"~walk-{uuid4()}-"
    now = datetime.now(UTC)
    past, future = now - timedelta(seconds=1), now + timedelta(hours=1)
    expected = sorted(prefix + suffix for suffix in ["A", "a", "z", "é", "Ω", "中"])
    rows = [
        {
            "tenant_id": tenant,
            "memory_id": uuid4(),
            "kind": "extraction",
            "available_at": past,
            "lease_until": past,
        }
        for tenant in expected
    ]
    rows += [{**rows[0], "memory_id": uuid4()} for _ in range(512)]
    rows += [
        {
            "tenant_id": prefix + suffix,
            "memory_id": uuid4(),
            "kind": "extraction",
            "available_at": available,
            "lease_until": lease,
            "terminal_reason": terminal,
        }
        for suffix, available, lease, terminal in [
            ("B", future, past, None),
            ("b", past, future, None),
            ("c", past, past, "retry_exhausted"),
        ]
    ]
    # Executed only in the isolated CI database, alongside the real migrations.
    async with get_session() as session:
        await session.execute(insert(Work), rows)
    found, cursor = [], prefix
    while len(found) < len(expected):
        response = await client.get(
            "/api/v1/storage/tenants/active",
            params={"background_due": True, "after_tenant": cursor, "limit": 2},
        )
        assert response.status_code == 200
        page = response.json()["tenant_ids"]
        assert len(page) == 2
        found.extend(page)
        cursor = page[-1]
    assert found == expected
