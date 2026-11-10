"""Persistence and fixed-window matching on the writer, never a lagging replica."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from common.keystone_fetches import (
    BUCKET_SECONDS,
    BUCKET_SLACK_SECONDS,
    CACHE_SECONDS,
    MAX_CHECKS,
    RETENTION_DAYS,
    FetchCheck,
    FetchRecord,
)
from common.models.keystone_fetch import KeystoneFetch
from core_storage_api.database.init import get_engine


async def record_fetch(*, tenant_id: str, record: FetchRecord) -> dict:
    if record.tenant_id != tenant_id:
        raise ValueError("record tenant does not match scope")
    served_at = record.served_at.astimezone(UTC)
    stmt = (
        insert(KeystoneFetch)
        .values(
            tenant_id=tenant_id,
            install_uuid=record.install_uuid,
            credential_id=record.credential_id,
            principal=record.principal or "",
            fleet_id=record.fleet_id or "",
            rule_set_hash=record.rule_set_hash,
            bucket_start=served_at.replace(second=0, microsecond=0),
            served_at=served_at,
            truncated=record.truncated,
        )
        .on_conflict_do_nothing(constraint="uq_keystone_fetch_bucket")
        .returning(KeystoneFetch.id)
    )
    async with AsyncSession(get_engine()) as session, session.begin():
        fetch_id = (await session.execute(stmt)).scalar_one_or_none()
    return {"created": fetch_id is not None}


def _lower(check: FetchCheck) -> datetime:
    # Fixed 300s broker cache plus the approved extra 60s slack. Match a
    # minute bucket by overlap, not by pretending every coalesced read was
    # recorded. The actual first served_at is returned as evidence; overlap
    # also has the bucket's own granularity and is not an exact fetch instant.
    return check.delivered_at - timedelta(seconds=CACHE_SECONDS + BUCKET_SLACK_SECONDS)


async def match_fetches(*, tenant_id: str, checks: list[FetchCheck]) -> dict:
    if not 1 <= len(checks) <= MAX_CHECKS:
        raise ValueError("expected 1 to 100 checks")
    if any(check.tenant_id != tenant_id for check in checks):
        raise ValueError("check tenant does not match scope")
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    predicates = [
        and_(
            KeystoneFetch.tenant_id == check.tenant_id,
            KeystoneFetch.install_uuid == check.install_uuid,
            KeystoneFetch.principal == (check.principal or ""),
            KeystoneFetch.fleet_id == (check.fleet_id or ""),
            KeystoneFetch.rule_set_hash == check.rule_set_hash,
            KeystoneFetch.bucket_start <= check.delivered_at,
            KeystoneFetch.served_at <= check.delivered_at,
            KeystoneFetch.bucket_start > _lower(check) - timedelta(seconds=BUCKET_SECONDS),
        )
        for check in checks
        if check.delivered_at >= cutoff
    ]
    rows = []
    if predicates:
        # At most eight unique minute buckets for each of <=100 checks. Fetch
        # their union once, rather than doing 100 database round trips.
        stmt = (
            select(KeystoneFetch)
            .where(
                KeystoneFetch.tenant_id == tenant_id,
                KeystoneFetch.served_at >= cutoff,
                or_(*predicates),
            )
            .order_by(KeystoneFetch.bucket_start.desc(), KeystoneFetch.served_at)
            .limit(MAX_CHECKS * 8)
        )
        async with AsyncSession(get_engine()) as session:
            rows = list((await session.scalars(stmt)).all())
    results: list[dict] = []
    for check in checks:
        if check.delivered_at < cutoff:
            results.append({"status": "evidence_expired"})
            continue
        found = next(
            (
                row
                for row in rows
                if row.tenant_id == check.tenant_id
                and row.install_uuid == check.install_uuid
                and row.principal == (check.principal or "")
                and row.fleet_id == (check.fleet_id or "")
                and row.rule_set_hash == check.rule_set_hash
                and row.bucket_start <= check.delivered_at
                and row.served_at <= check.delivered_at
                and row.bucket_start + timedelta(seconds=BUCKET_SECONDS) > _lower(check)
            ),
            None,
        )
        if found is None:
            results.append({"status": "no_matching_fetch"})
        else:
            results.append(
                {
                    "status": "matched",
                    "fetch_id": str(found.id),
                    "served_at": found.served_at.isoformat(),
                    "bucket_start": found.bucket_start.isoformat(),
                    "truncated": found.truncated,
                }
            )
    return {"results": results}


async def purge_expired_fetches(*, limit: int = 1000) -> int:
    """One bounded operator-scheduled batch; no scheduler is enabled here."""
    if not 1 <= limit <= 10_000:
        raise ValueError("purge limit must be 1 to 10000")
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    victims = (
        select(KeystoneFetch.id)
        .where(KeystoneFetch.served_at < cutoff)
        .order_by(KeystoneFetch.served_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    async with AsyncSession(get_engine()) as session, session.begin():
        ids = list((await session.scalars(victims)).all())
        if ids:
            await session.execute(delete(KeystoneFetch).where(KeystoneFetch.id.in_(ids)))
    return len(ids)
