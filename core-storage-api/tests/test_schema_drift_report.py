"""Startup reporting for schema changes that migrations can leave unapplied."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from sqlalchemy import text

from core_storage_api import app as app_module
from core_storage_api.config import settings
from core_storage_api.database.init import get_engine
from core_storage_api.database.migration_postconditions import (
    MIGRATION_POSTCONDITIONS,
    MigrationPostcondition,
)

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
        assert any("migration 044 did not apply" in message for message in warnings)
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


async def test_registry_checks_share_one_connection_and_respect_severity(caplog) -> None:
    warning = MigrationPostcondition(
        "900", "warning_check", "SELECT false AS warning_check", "warning", "warning drift"
    )
    broken = MigrationPostcondition(
        "901", "broken_check", "SELECT broken AS broken_check", "warning", "broken drift"
    )
    error = MigrationPostcondition(
        "902", "error_check", "SELECT false AS error_check", "error", "error drift"
    )

    execute_result = Mock()
    execute_result.scalars.return_value.all.return_value = []
    connection = AsyncMock()
    connection.execute.return_value = execute_result
    connection.scalar.side_effect = [False, RuntimeError("predicate failed"), False]
    connection.begin_nested = Mock(return_value=AsyncMock())
    connection_manager = AsyncMock()
    connection_manager.__aenter__.return_value = connection
    engine = Mock()
    engine.connect.return_value = connection_manager

    caplog.clear()
    with (
        patch.object(settings, "core_storage_role", "writer"),
        patch.object(app_module, "MIGRATION_POSTCONDITIONS", (warning, broken, error)),
        patch.object(app_module, "get_engine", return_value=engine),
        caplog.at_level(logging.WARNING, logger=app_module.__name__),
    ):
        await app_module.report_schema_drift()

    engine.connect.assert_called_once_with()
    assert [str(awaited.args[0]) for awaited in connection.scalar.await_args_list] == [
        "SELECT false AS warning_check",
        "SELECT broken AS broken_check",
        "SELECT false AS error_check",
    ]
    records = {(record.levelno, record.getMessage()) for record in caplog.records}
    assert any(level == logging.WARNING and "900/warning_check" in message for level, message in records)
    assert any(level == logging.ERROR and "901/broken_check" in message for level, message in records)
    assert any(level == logging.ERROR and "902/error_check" in message for level, message in records)


async def test_failed_predicate_savepoint_does_not_suppress_later_checks(_ensure_schema, caplog) -> None:
    broken = MigrationPostcondition(
        "900", "broken_check", "SELECT this_is_not_valid_sql", "warning", "broken drift"
    )
    later = MigrationPostcondition("901", "later_check", "SELECT false", "error", "later drift")

    caplog.clear()
    with (
        patch.object(app_module, "MIGRATION_POSTCONDITIONS", (broken, later)),
        caplog.at_level(logging.WARNING, logger=app_module.__name__),
    ):
        await app_module.report_schema_drift()

    records = {(record.levelno, record.getMessage()) for record in caplog.records}
    assert any(level == logging.ERROR and "900/broken_check" in message for level, message in records)
    assert any(
        level == logging.ERROR and "Migration post-condition failed [901/later_check]" in message
        for level, message in records
    )


async def test_startup_survives_probe_failure(caplog) -> None:
    events: list[str] = []

    async def record_init_database() -> None:
        events.append("init_database")

    async def fail_probe(_predicate) -> None:
        events.append("schema_drift_probe")
        raise RuntimeError("catalog unavailable")

    execute_result = Mock()
    execute_result.scalars.return_value.all.return_value = []
    connection = AsyncMock()
    connection.execute.return_value = execute_result
    connection.scalar.side_effect = fail_probe
    connection.begin_nested = Mock(return_value=AsyncMock())
    connection_manager = AsyncMock()
    connection_manager.__aenter__.return_value = connection
    engine = Mock()
    engine.connect.return_value = connection_manager
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


def _mock_engine(scalar_results: list[object]) -> Mock:
    execute_result = Mock()
    execute_result.scalars.return_value.all.return_value = []
    connection = AsyncMock()
    connection.execute.return_value = execute_result
    connection.scalar.side_effect = scalar_results
    connection.begin_nested = Mock(return_value=AsyncMock())
    connection_manager = AsyncMock()
    connection_manager.__aenter__.return_value = connection
    engine = Mock()
    engine.connect.return_value = connection_manager
    return engine


async def _drift_with(condition: MigrationPostcondition, scalar_results: list[object], caplog):
    caplog.clear()
    with (
        patch.object(settings, "core_storage_role", "writer"),
        patch.object(app_module, "MIGRATION_POSTCONDITIONS", (condition,)),
        patch.object(app_module, "get_engine", return_value=_mock_engine(scalar_results)),
        caplog.at_level(logging.INFO, logger=app_module.__name__),
    ):
        await app_module.report_schema_drift()
    return caplog.records


async def test_a_postcondition_expected_here_logs_info_not_warning(caplog) -> None:
    """A soft-failing migration this deployment could never apply is not a defect.

    044 is unapplied on every managed-Postgres deployment, by design, and no
    number of boots will change that. Warning each time buries the one case
    that matters — and it did: the condition was read as a production incident
    the day after it shipped.
    """
    condition = MigrationPostcondition(
        "900", "expected_check", "SELECT false", "warning", "drift", expected_when="SELECT true"
    )

    records = await _drift_with(condition, [False, True], caplog)

    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert any(r.levelno == logging.INFO and "900/expected_check" in r.getMessage() for r in records)


async def test_a_postcondition_not_expected_here_still_warns(caplog) -> None:
    """The genuine case: the deployment COULD have applied it, and it is unapplied."""
    condition = MigrationPostcondition(
        "901", "genuine_check", "SELECT false", "warning", "drift", expected_when="SELECT false"
    )

    records = await _drift_with(condition, [False, False], caplog)

    assert any(r.levelno == logging.WARNING and "901/genuine_check" in r.getMessage() for r in records)


async def test_a_failing_expectation_probe_falls_back_to_the_warning(caplog) -> None:
    """A probe that cannot answer must not be read as "expected".

    Silence is the costly direction here: it would hide a real unapplied
    migration behind a broken query, which is the failure the post-condition
    exists to catch.
    """
    condition = MigrationPostcondition(
        "902", "broken_expectation", "SELECT false", "warning", "drift", expected_when="SELECT nope"
    )

    records = await _drift_with(condition, [False, RuntimeError("expectation failed")], caplog)

    assert any(r.levelno == logging.WARNING and "902/broken_expectation" in r.getMessage() for r in records)


async def test_a_postcondition_without_an_expectation_is_unconditional(caplog) -> None:
    """No expected_when means no second query, and the warning stands."""
    condition = MigrationPostcondition("903", "plain_check", "SELECT false", "warning", "drift")

    records = await _drift_with(condition, [False], caplog)

    assert any(r.levelno == logging.WARNING and "903/plain_check" in r.getMessage() for r in records)


async def test_the_044_expectation_holds_when_the_function_is_absent(_ensure_schema) -> None:
    """044 tolerates undefined_function too, so its expectation must cover it.

    ``to_regprocedure`` returns NULL rather than raising, so a missing function
    makes the WHERE match zero rows. Without a COALESCE the probe returns no
    row at all, which reads as "not expected" and fires a warning claiming this
    role owns the pgvector extension — a claim nothing established.
    """
    condition = next(item for item in MIGRATION_POSTCONDITIONS if item.revision == "044")
    assert condition.expected_when is not None
    absent = condition.expected_when.replace(
        "to_regprocedure('cosine_distance(vector, vector)')",
        "to_regprocedure('no_such_function(integer)')",
    )
    assert absent != condition.expected_when

    async with get_engine().connect() as connection:
        assert await connection.scalar(text(absent)) is True
        # And with the real function present, CI owns the extension, so the
        # condition is NOT expected here and a failure would still warn.
        assert await connection.scalar(text(condition.expected_when)) is False
