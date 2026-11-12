"""Operator-only masking: exact selection, counts and an atomic apply contract.

All fixtures are synthetic and the connection is in memory. These tests never
connect to a database or read environment credentials.
"""

from __future__ import annotations

import asyncio
import copy
import importlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.sql.dml import Update

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]
BEFORE = datetime(2026, 10, 28, tzinfo=UTC)
OLD = BEFORE - timedelta(days=1)
NEW = BEFORE + timedelta(days=1)
SAMPLE = "Contact alice@example.test"


@pytest.fixture
def repair():
    return importlib.import_module("scripts.mask_legacy_documents")


def document(*, tenant="tenant-a", updated=OLD, data=None, collection="notes"):
    return {
        "id": uuid4(),
        "tenant_id": tenant,
        "collection": collection,
        "doc_id": "doc-a",
        "data": {"text": SAMPLE} if data is None else data,
        "updated_at": updated,
        "created_at": OLD - timedelta(days=1),
        "embedding": [0.25],
    }


def entry(*, updated=OLD, data=None):
    return {
        "doc_id": "rule-a",
        "fleet_id": "fleet-a",
        "updated_at": updated.isoformat() if updated else None,
        "data": {"content": SAMPLE, "weight": 1} if data is None else data,
    }


def version(*, tenant="tenant-a", created=OLD, snapshot=None):
    return {
        "tenant_id": tenant,
        "version": 1,
        "created_at": created,
        "snapshot": [entry()] if snapshot is None else snapshot,
    }


def scope(repair, **overrides):
    return repair.Scope(
        **{
            "tenant_ids": ("tenant-a",),
            "before": BEFORE,
            "categories": frozenset({"email"}),
            **overrides,
        }
    )


class Connection:
    """Transaction fault injection, with no database/driver or URL involved."""

    def __init__(self, documents=(), versions=(), fail_write=0):
        self.documents, self.versions = (
            copy.deepcopy(list(documents)),
            copy.deepcopy(list(versions)),
        )
        self.calls, self.options, self.transactions = [], [], 0
        self.readonly, self.write_count, self.fail_write = False, 0, fail_write
        self.committed, self.rolled_back = False, False

    async def execution_options(self, **options):
        self.options.append(options)
        return self

    @asynccontextmanager
    async def begin(self):
        self.transactions += 1
        self.readonly = False  # transaction-local, as SET TRANSACTION is in PostgreSQL
        original = copy.deepcopy((self.documents, self.versions))
        try:
            yield
        except BaseException:
            self.documents, self.versions = original
            self.rolled_back = True
            raise
        else:
            self.committed = True

    async def execute(self, statement, parameters=None):
        self.calls.append((statement, parameters))
        if not isinstance(statement, Update):
            if "READ ONLY" in str(statement):
                self.readonly = True
            return SimpleNamespace(rowcount=0)
        assert not self.readonly, "dry-run attempted a write"
        self.write_count += 1
        if self.write_count == self.fail_write:
            raise RuntimeError(SAMPLE)
        params = statement.compile().params
        rows = self.documents if statement.table.name == "documents" else self.versions
        # SQL UPDATE selectors are bound parameters, not interpolated text.
        assert "tenant_id" in str(statement).split("WHERE", 1)[1]
        target = next(
            r
            for r in rows
            if r["tenant_id"] in params.values()
            and (
                r["id"] in params.values()
                if "id" in r
                else r["version"] in params.values()
            )
        )
        for key in ("data", "embedding", "snapshot"):
            if key in params:
                target[key] = params[key]
        return SimpleNamespace(rowcount=1)

    @asynccontextmanager
    async def stream(self, statement):
        self.calls.append((statement, None))
        name = (
            "keystone_versions"
            if "keystone_versions" in str(statement)
            else "documents"
        )
        rows = self.versions if name == "keystone_versions" else self.documents

        async def iterate():
            for row in copy.deepcopy(rows):
                yield row

        yield SimpleNamespace(mappings=iterate)


class Engine:
    def __init__(self, connection):
        self.connection, self.connected = connection, 0

    @asynccontextmanager
    async def connect(self):
        self.connected += 1
        yield self.connection


async def test_default_is_readonly_counts_only_and_never_updates(repair):
    conn = Connection([document()], [version()])
    before = copy.deepcopy((conn.documents, conn.versions))
    counts = await repair.run_repair(Engine(conn), scope(repair))
    assert conn.readonly and conn.transactions == 1
    assert conn.options == [{"isolation_level": "REPEATABLE READ"}]
    assert (conn.documents, conn.versions) == before and conn.write_count == 0
    assert counts["rows_changed"] == 2 and counts["rows_applied"] == 0
    assert counts["documents_changed"] == counts["versions_changed"] == 1
    assert all(type(value) is int for value in counts.values())
    assert SAMPLE not in json.dumps(counts) and "tenant-a" not in json.dumps(counts)
    for statement, _ in conn.calls:
        if getattr(statement, "is_select", False):
            assert statement.compile().params["tenant_id_1"] == ["tenant-a"]
            if "keystone_versions" not in str(statement):
                assert BEFORE in statement.compile().params.values()


@pytest.mark.parametrize(
    "apply,expect", [(True, None), (False, 1), (True, -1), (True, True)]
)
async def test_apply_and_nonnegative_expect_are_required_together(
    repair, apply, expect
):
    engine = Engine(Connection([document()]))
    with pytest.raises(repair.RepairError):
        await repair.run_repair(engine, scope(repair), apply=apply, expect=expect)
    assert engine.connected == 0


async def test_expect_means_changed_physical_rows_not_findings(repair):
    doc = document(data={"nested": [SAMPLE, SAMPLE], "n": 7})
    conn = Connection(
        [doc], [version(snapshot=[entry(), {**entry(), "doc_id": "rule-b"}])]
    )
    counts = await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=2)
    assert counts["rows_applied"] == counts["rows_changed"] == 2
    assert conn.committed and conn.transactions == 1 and conn.write_count == 2
    assert conn.documents[0]["embedding"] is None
    assert SAMPLE not in json.dumps(conn.documents[0]["data"])
    assert conn.documents[0]["updated_at"] == doc["updated_at"]
    assert conn.documents[0]["id"] == doc["id"]
    assert conn.versions[0]["version"] == 1
    assert conn.versions[0]["snapshot"][0]["doc_id"] == "rule-a"
    assert conn.versions[0]["snapshot"][0]["updated_at"] == OLD.isoformat()
    assert conn.versions[0]["snapshot"][0]["data"]["weight"] == 1
    assert not any(
        "audit_log" in str(stmt) or "keystone_fetch" in str(stmt)
        for stmt, _ in conn.calls
    )
    assert all(
        "FOR UPDATE" in str(stmt)
        for stmt, _ in conn.calls
        if getattr(stmt, "is_select", False)
    )


async def test_mismatched_expect_rolls_back_without_any_updates(repair):
    conn = Connection([document()], [version()])
    with pytest.raises(repair.RepairError):
        await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=1)
    assert conn.rolled_back and not conn.committed and conn.write_count == 0


async def test_a_later_write_failure_rolls_back_both_tables_without_row_details(repair):
    conn = Connection([document()], [version()], fail_write=2)
    original = copy.deepcopy((conn.documents, conn.versions))
    with pytest.raises(repair.RepairError) as caught:
        await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=2)
    assert (conn.documents, conn.versions) == original and conn.rolled_back
    assert SAMPLE not in str(caught.value) and caught.value.__cause__ is None


async def test_exact_scope_skips_foreign_recent_unknown_and_unchanged_documents(repair):
    docs = [
        document(collection="_keystones"),
        document(tenant="tenant-b"),
        document(updated=BEFORE),
        document(updated=NEW),
        document(updated=None),
        document(data={"text": "ordinary text"}),
    ]
    conn = Connection(docs)
    counts = await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=1)
    assert counts["ambiguous_documents"] == 1
    assert conn.documents[1:] == docs[1:]
    assert conn.documents[0]["embedding"] is None


async def test_a_later_snapshot_masks_only_proven_old_payloads(repair):
    old, recent, unknown = (
        entry(),
        {**entry(updated=NEW), "doc_id": "new"},
        {**entry(updated=None), "doc_id": "unknown"},
    )
    conn = Connection(versions=[version(created=NEW, snapshot=[old, recent, unknown])])
    counts = await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=1)
    result = conn.versions[0]["snapshot"]
    assert result[1:] == [recent, unknown]
    assert result[0]["data"]["content"] != SAMPLE
    assert counts["ambiguous_versions"] == 1


async def test_old_snapshot_timestamp_proves_age_even_without_entry_timestamp(repair):
    conn = Connection(versions=[version(snapshot=[entry(updated=None)])])
    counts = await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=1)
    assert counts["ambiguous_versions"] == 0
    assert conn.versions[0]["snapshot"][0]["data"]["content"] != SAMPLE


async def test_masking_is_category_limited_and_repeat_apply_is_a_noop(repair):
    conn = Connection([document()])
    first = await repair.run_repair(
        Engine(conn), scope(repair, categories=frozenset({"credit_card"}))
    )
    assert first["rows_changed"] == 0
    await repair.run_repair(Engine(conn), scope(repair), apply=True, expect=1)
    conn.write_count = 0
    repeated = await repair.run_repair(
        Engine(conn), scope(repair), apply=True, expect=0
    )
    assert repeated["rows_changed"] == repeated["rows_applied"] == conn.write_count == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"before": BEFORE.replace(tzinfo=None)},
        {"tenant_ids": ()},
        {"tenant_ids": (" ",)},
        {"categories": frozenset()},
        {"categories": frozenset({"unrecognised"})},
    ],
)
async def test_scope_must_be_explicit_and_valid(repair, overrides):
    with pytest.raises(repair.RepairError):
        scope(repair, **overrides)


@pytest.mark.parametrize(
    "args", [[], ["--apply"], ["--expect", "1"], ["--tenant-id", SAMPLE]]
)
async def test_invalid_cli_never_connects_or_echoes_inputs(
    repair, monkeypatch, capsys, args
):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid invocation must not create an engine or read credentials")

    monkeypatch.setattr(repair, "create_async_engine", forbidden)
    assert repair.main(args) != 0
    output = capsys.readouterr()
    assert not output.err and SAMPLE not in output.out
    counts = json.loads(output.out)
    assert counts["errors"] == 1 and all(type(v) is int for v in counts.values())


async def test_cli_contains_driver_errors_without_printing_row_data(
    repair, monkeypatch, capsys
):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/unused")

    def unavailable(*_args, **_kwargs):
        logging.getLogger("sqlalchemy.engine").critical(SAMPLE)
        raise RuntimeError(SAMPLE)

    monkeypatch.setattr(repair, "create_async_engine", unavailable)
    args = [
        "--tenant-id",
        "tenant-a",
        "--before",
        BEFORE.isoformat(),
        "--category",
        "email",
    ]
    assert await asyncio.to_thread(repair.main, args) != 0
    output = capsys.readouterr()
    assert not output.err and SAMPLE not in output.out
    counts = json.loads(output.out)
    assert counts["errors"] == 1 and all(type(v) is int for v in counts.values())
