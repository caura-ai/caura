"""The end-of-run cleanup must actually delete what it claims to.

Every statement in ``purge_test_rows`` swallows its exception, so a DELETE
naming a table that does not exist — or filtering on a column that table does
not have — is indistinguishable from one that worked. ``organization_settings``
keys on ``org_id`` rather than ``tenant_id``, which is exactly the shape that
fails silently if it is bolted onto the ``tenant_id`` loop instead of given its
own statement.

Left unreclaimed those rows are not merely untidy. The interviewer schedule
sweep enumerates every enabled org on each tick, so they make it slower on
every subsequent local run — the accumulation that took one sweep test to 48s
before this was fixed.
"""

import uuid

from sqlalchemy import text

from tests.conftest import SWEEP_TENANT_PREFIX, purge_test_rows


async def _count(engine, table: str, org_id: str) -> int:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE org_id = :o"),
            {"o": org_id},
        )
        return int(result.scalar() or 0)


async def test_purge_reclaims_organization_settings(_engine, _setup_schema):
    # A prefix unique to this test, still inside SWEEP_TENANT_PREFIX so the
    # real end-of-run sweep would reclaim it too. Purging by this narrow
    # prefix rather than the session-wide one keeps the call from deleting
    # rows other tests in this session are still using.
    prefix = f"{SWEEP_TENANT_PREFIX}purgeprobe-{uuid.uuid4().hex[:8]}"
    org_id = f"{prefix}-org"

    async with _engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO organization_settings (org_id, settings) "
                "VALUES (:o, '{}'::jsonb)"
            ),
            {"o": org_id},
        )
        # The audit trail is written by the same request that updates the
        # settings, keys on ``org_id`` the same way, and is append-only — so
        # it leaks faster than the settings row itself.
        await conn.execute(
            text(
                "INSERT INTO organization_settings_audit (org_id, diff) "
                "VALUES (:o, '{}'::jsonb)"
            ),
            {"o": org_id},
        )
    assert await _count(_engine, "organization_settings", org_id) == 1, (
        "seed row missing"
    )
    assert await _count(_engine, "organization_settings_audit", org_id) == 1, (
        "seed audit row missing"
    )

    async with _engine.begin() as conn:
        await purge_test_rows(conn, f"{prefix}%")

    assert await _count(_engine, "organization_settings", org_id) == 0, (
        "organization_settings row survived purge_test_rows — every test that "
        "opts a tenant into a feature leaves one behind, and the interviewer "
        "sweep pays for all of them on every tick"
    )
    assert await _count(_engine, "organization_settings_audit", org_id) == 0, (
        "organization_settings_audit row survived purge_test_rows — append-only "
        "and keyed on org_id, so it accumulates faster than the settings table"
    )
