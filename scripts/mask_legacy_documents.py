"""Operator-run, counts-only masking of proven older document payloads.

Run from the repository root with ``python -m scripts.mask_legacy_documents``.
Credentials come only from the operator's DATABASE_URL, never a dotenv file.
See docs/legacy-document-masking.md before running against a database.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from common.governance import PIICategory, mask, scan
from common.models.document import Document
from common.models.keystone_version import KeystoneVersion


def _counts() -> dict[str, int]:
    return dict.fromkeys(
        (
            "documents_scanned",
            "documents_changed",
            "versions_scanned",
            "versions_changed",
            "ambiguous_documents",
            "ambiguous_versions",
            "rows_changed",
            "rows_applied",
            "errors",
        ),
        0,
    )


class RepairError(Exception):
    """Only a fixed message and aggregate counts may cross the CLI boundary."""

    def __init__(self, *, code: int = 4, counts: dict[str, int] | None = None):
        super().__init__("Document masking did not complete")
        self.code = code
        self.counts = dict(counts) if counts is not None else _counts()
        self.counts.update(errors=1, rows_applied=0)


def _time(value: Any) -> datetime | None:
    if isinstance(value, str):
        # Do not silently round a timestamp on either side of the cutoff.
        if re.search(r"\.\d{7,}", value):
            return None
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        return None
    return value.astimezone(UTC)


@dataclass(frozen=True)
class Scope:
    tenant_ids: tuple[str, ...]
    before: datetime
    categories: frozenset[str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_ids, tuple)
            or not self.tenant_ids
            or any(
                not isinstance(t, str) or not t.strip() or len(t) > 200
                for t in self.tenant_ids
            )
            or not isinstance(self.before, datetime)
            or _time(self.before) is None
            or not self.categories
            or not self.categories <= frozenset(c.value for c in PIICategory)
        ):
            raise RepairError(code=2)


def _masked(value: Any, categories: frozenset[str]) -> Any:
    if isinstance(value, str):
        return mask(
            value, scan(value, enabled_categories=[PIICategory(c) for c in categories])
        )
    if isinstance(value, dict):
        return {key: _masked(item, categories) for key, item in value.items()}
    if isinstance(value, list):
        return [_masked(item, categories) for item in value]
    return value


def _document(row, scope: Scope) -> tuple[Any, bool]:
    updated = _time(row["updated_at"])
    if updated is None:
        return None, True
    if updated >= scope.before:
        return None, False
    data = _masked(row["data"], scope.categories)
    return (data if data != row["data"] else None), False


def _snapshot(row, scope: Scope) -> tuple[list | None, bool]:
    snapshot = row["snapshot"]
    if not isinstance(snapshot, list):
        return None, True
    created = _time(row["created_at"])
    old_version = created is not None and created < scope.before
    changed, ambiguous = False, False
    result = []
    for entry in snapshot:
        if not isinstance(entry, dict) or "data" not in entry:
            ambiguous = True
            result.append(entry)
            continue
        updated = _time(entry.get("updated_at"))
        if not old_version and updated is None:
            ambiguous = True
            result.append(entry)
            continue
        if not old_version and updated is not None and updated >= scope.before:
            result.append(entry)
            continue
        data = _masked(entry["data"], scope.categories)
        changed |= data != entry["data"]
        # Scope, ids, weights outside data, timestamps and version provenance
        # are structural: mask payload values only, as on the write gate.
        result.append({**entry, "data": data})
    return (result if changed else None), ambiguous


async def _scan(
    conn: AsyncConnection, scope: Scope, *, lock: bool, write: bool
) -> dict[str, int]:
    counts = _counts()
    documents = (
        select(Document.id, Document.tenant_id, Document.data, Document.updated_at)
        .where(
            Document.tenant_id.in_(scope.tenant_ids),
            or_(Document.updated_at < scope.before, Document.updated_at.is_(None)),
        )
        .order_by(Document.tenant_id, Document.id)
    )
    versions = (
        select(
            KeystoneVersion.tenant_id,
            KeystoneVersion.version,
            KeystoneVersion.snapshot,
            KeystoneVersion.created_at,
        )
        .where(KeystoneVersion.tenant_id.in_(scope.tenant_ids))
        .order_by(
            KeystoneVersion.tenant_id,
            KeystoneVersion.version,
        )
    )
    for name, candidate_query in (("documents", documents), ("versions", versions)):
        query = candidate_query.with_for_update() if lock else candidate_query
        async with conn.stream(query) as result:
            async for row in result.mappings():
                # Defense in depth: never rely on a caller's candidate iterator
                # for tenant selection, even though both SQL queries bind it.
                if row["tenant_id"] not in scope.tenant_ids:
                    continue
                counts[f"{name}_scanned"] += 1
                payload, ambiguous = (
                    _document(row, scope)
                    if name == "documents"
                    else _snapshot(row, scope)
                )
                counts[f"ambiguous_{name}"] += int(ambiguous)
                if payload is None:
                    continue
                counts[f"{name}_changed"] += 1
                counts["rows_changed"] += 1
                if not write:
                    continue
                if name == "documents":
                    statement = (
                        update(Document)
                        .where(
                            Document.id == row["id"],
                            Document.tenant_id == row["tenant_id"],
                        )
                        .values(data=payload, embedding=None)
                    )
                else:
                    statement = (
                        update(KeystoneVersion)
                        .where(
                            KeystoneVersion.tenant_id == row["tenant_id"],
                            KeystoneVersion.version == row["version"],
                        )
                        .values(snapshot=payload)
                    )
                updated = await conn.execute(statement)
                if updated.rowcount != 1:
                    raise RepairError()
    return counts


async def run_repair(
    engine: AsyncEngine,
    scope: Scope,
    *,
    apply: bool = False,
    expect: int | None = None,
) -> dict[str, int]:
    if (apply and (type(expect) is not int or expect < 0)) or (
        not apply and expect is not None
    ):
        raise RepairError(code=2)
    try:
        async with engine.connect() as connection:
            conn = await connection.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                await conn.execute(
                    text(
                        "SET TRANSACTION READ WRITE"
                        if apply
                        else "SET TRANSACTION READ ONLY"
                    )
                )
                await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                await conn.execute(text("SET LOCAL statement_timeout = '60s'"))
                if apply:
                    # Same namespace/order as ordinary keystone writes, taken
                    # before row locks. A concurrent change aborts safely under
                    # repeatable read; no automatic retry may change --expect.
                    for tenant in sorted(set(scope.tenant_ids)):
                        await conn.execute(
                            text(
                                "SELECT pg_advisory_xact_lock(hashtext('keystone_versions'), hashtext(:tenant_id))"
                            ),
                            {"tenant_id": tenant},
                        )
                counts = await _scan(conn, scope, lock=apply, write=False)
                if apply:
                    if counts["rows_changed"] != expect:
                        raise RepairError(code=3, counts=counts)
                    # Re-scan the locked, stable snapshot instead of retaining
                    # every changed payload in memory. All updates share this
                    # transaction, including count checks on both passes.
                    applied = await _scan(conn, scope, lock=True, write=True)
                    if any(applied[k] != counts[k] for k in counts):
                        raise RepairError()
                    counts["rows_applied"] = counts["rows_changed"]
            return counts  # only after a successful commit
    except RepairError:
        raise
    except Exception:  # noqa: BLE001 — never reflect SQL parameters or row data
        raise RepairError() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise RepairError(code=2)


async def _cli(scope: Scope, *, apply: bool, expect: int | None) -> dict[str, int]:
    # Parsed arguments are validated before reading the operator's connection
    # configuration. No dotenv discovery, app startup, provider or audit call.
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        raise RepairError(code=2)
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        raise RepairError(code=2)
    url = url.set(drivername="postgresql+asyncpg")
    engine = create_async_engine(
        url, echo=False, hide_parameters=True, poolclass=NullPool
    )
    try:
        return await run_repair(engine, scope, apply=apply, expect=expect)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    # Engine DEBUG logs can include result rows despite hide_parameters. This
    # standalone CLI suppresses library logs for the entire operation, then
    # restores the caller's logging threshold. It prints only integer counts.
    disabled = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        parser = _Parser(description=__doc__)
        parser.add_argument("--tenant-id", action="append", required=True)
        parser.add_argument("--before", required=True)
        parser.add_argument(
            "--category",
            action="append",
            choices=[c.value for c in PIICategory],
            required=True,
        )
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--expect", type=int)
        args = parser.parse_args(argv)
        before = _time(args.before)
        if (
            before is None
            or (args.apply and (args.expect is None or args.expect < 0))
            or (not args.apply and args.expect is not None)
        ):
            raise RepairError(code=2)
        scope = Scope(tuple(args.tenant_id), before, frozenset(args.category))
        counts = asyncio.run(_cli(scope, apply=args.apply, expect=args.expect))
    except RepairError as exc:
        print(json.dumps(exc.counts, sort_keys=True))
        return exc.code
    except (Exception, KeyboardInterrupt):  # noqa: BLE001 — counts only, no tracebacks
        counts = _counts()
        counts["errors"] = 1
        print(json.dumps(counts, sort_keys=True))
        return 4
    else:
        print(json.dumps(counts, sort_keys=True))
        return 0
    finally:
        logging.disable(disabled)


if __name__ == "__main__":
    raise SystemExit(main())
