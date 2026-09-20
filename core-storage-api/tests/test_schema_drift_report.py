"""Startup reporting for schema changes that migrations can leave unapplied."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from sqlalchemy import text

from core_storage_api import app as app_module
from core_storage_api.config import settings
from core_storage_api.database.init import get_engine

_TEST_INDEX = "test_schema_drift_report_invalid_idx"


async def _set_cosine_distance_cost(cost: float) -> None:
    async with get_engine().begin() as connection:
        await connection.execute(text(f"ALTER FUNCTION cosine_distance(vector, vector) COST {cost:g}"))


async def _cosine_distance_cost() -> float:
    async with get_engine().connect() as connection:
        cost = await connection.scalar(
            text("SELECT procost FROM pg_proc WHERE oid = to_regprocedure('cosine_distance(vector, vector)')")
        )
    assert cost is not None
    return float(cost)


async def test_invalid_index_is_detected_and_named(_ensure_schema, caplog) -> None:
    try:
        async with get_engine().begin() as connection:
            await connection.execute(text(f"DROP INDEX IF EXISTS {_TEST_INDEX}"))
            await connection.execute(text(f"CREATE INDEX {_TEST_INDEX} ON memories (id)"))
            await connection.execute(
                text(f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = '{_TEST_INDEX}'::regclass")
            )

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger=app_module.__name__):
            await app_module.report_schema_drift()

        errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
        assert any(_TEST_INDEX in message for message in errors)
        assert any(
            "DROP INDEX CONCURRENTLY" in message and "CREATE INDEX CONCURRENTLY" in message
            for message in errors
        )
    finally:
        async with get_engine().begin() as connection:
            await connection.execute(text(f"DROP INDEX IF EXISTS {_TEST_INDEX}"))


async def test_healthy_schema_logs_no_error(_ensure_schema, caplog) -> None:
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=app_module.__name__):
        await app_module.report_schema_drift()

    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []


async def test_default_cosine_distance_cost_logs_warning(_ensure_schema, caplog) -> None:
    original_cost = await _cosine_distance_cost()
    try:
        await _set_cosine_distance_cost(1)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=app_module.__name__):
            await app_module.report_schema_drift()

        warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
        assert any(
            "migration 044 did not apply" in message and "under-pricing <=> by ~100x" in message
            for message in warnings
        )
    finally:
        await _set_cosine_distance_cost(original_cost)


async def test_applied_cosine_distance_cost_logs_no_warning(_ensure_schema, caplog) -> None:
    original_cost = await _cosine_distance_cost()
    try:
        await _set_cosine_distance_cost(100)
        async with get_engine().connect() as connection:
            other_costs = (
                (
                    await connection.execute(
                        text(
                            "SELECT procost FROM pg_proc WHERE proname = 'cosine_distance' "
                            "AND oid <> to_regprocedure('cosine_distance(vector, vector)')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert 1 in other_costs, "the test needs a default-cost overload to distinguish by signature"

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=app_module.__name__):
            await app_module.report_schema_drift()

        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []
    finally:
        await _set_cosine_distance_cost(original_cost)


async def test_reader_role_does_not_probe() -> None:
    with (
        patch.object(settings, "core_storage_role", "reader"),
        patch.object(app_module, "get_engine") as get_engine_mock,
    ):
        await app_module.report_schema_drift()

    get_engine_mock.assert_not_called()


async def test_startup_survives_probe_failure(caplog) -> None:
    events: list[str] = []

    async def record_init_database() -> None:
        events.append("init_database")

    def fail_probe() -> None:
        events.append("schema_drift_probe")
        raise RuntimeError("catalog unavailable")

    engine = Mock()
    engine.connect.side_effect = fail_probe
    engine.dispose = AsyncMock()

    caplog.clear()
    with (
        patch.object(settings, "core_storage_role", "writer"),
        patch.object(app_module, "init_database", side_effect=record_init_database),
        patch.object(app_module, "get_engine", return_value=engine),
        caplog.at_level(logging.ERROR, logger=app_module.__name__),
    ):
        async with app_module.lifespan(FastAPI()):
            pass

    assert any("schema drift" in record.getMessage().lower() for record in caplog.records)
    assert events == ["init_database", "schema_drift_probe"]
    engine.dispose.assert_awaited_once()
