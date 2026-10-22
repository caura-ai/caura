"""A test marked ``unit`` must not touch the database, and the conftest holds it to that.

``docs/self-hosting.md`` runs the root unit tests with ``pytest tests/ -m "unit"``,
"no database needed". Nothing checked that while the suite always ran against
PostgreSQL, and 303 tests so marked had come to reach it.
``_keeping_off_the_database`` (``tests/conftest.py``) now fails a database-free
test that takes a connection, or whose fixtures need the schema. These pin that
it does, and that what it gives such a test in place of the database works.
"""

import pytest
from sqlalchemy import text

from tests.conftest import _keeping_off_the_database

NODEID = "tests/test_example.py::test_example"


async def test_taking_a_connection_fails_the_test_and_says_where(_engine):
    with pytest.raises(pytest.fail.Exception) as failed:
        with _keeping_off_the_database(NODEID):
            async with _engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

    message = str(failed.value)
    assert f"{NODEID} is marked unit, so it must not use the database" in message
    assert "took 1 connection(s)" in message
    assert "tests/test_unit_tests_need_no_database.py" in message, message


@pytest.mark.unit
def test_a_test_whose_fixtures_need_the_schema_fails_before_it_starts():
    ran = False
    with pytest.raises(
        pytest.fail.Exception, match="a fixture it uses needs the schema"
    ):
        with _keeping_off_the_database(NODEID, ["db", "_engine", "_setup_schema"]):
            ran = True

    assert not ran


@pytest.mark.unit
def test_staying_off_the_database_passes():
    with _keeping_off_the_database(NODEID, ["monkeypatch"]):
        pass


@pytest.mark.unit
async def test_tenant_lookups_answer_as_for_an_empty_tenant():
    """Through the real storage bridge, with no database behind it."""
    from common.settings_version import version_of
    from core_api.clients.storage_client import get_storage_client

    sc = get_storage_client()
    settings, version = await sc.get_org_settings_versioned("a-tenant-with-nothing")

    assert settings == {}
    assert version == version_of(None)
    assert await sc.is_tenant_suppressed("a-tenant-with-nothing") is False


@pytest.mark.unit
def test_tenant_caches_start_empty():
    """A cached answer would decide whether the next test reached storage."""
    from core_api import suppression
    from core_api.services import organization_settings

    organization_settings._settings_cache["a-warm-tenant"] = object()
    suppression._cache["a-warm-tenant"] = (True, float("inf"))

    with _keeping_off_the_database(NODEID):
        assert "a-warm-tenant" not in organization_settings._settings_cache
        assert "a-warm-tenant" not in suppression._cache


@pytest.mark.unit
async def test_audit_events_stop_in_a_queue_nothing_flushes():
    from core_api.services.audit_queue import get_audit_queue
    from core_api.services.audit_service import log_action

    await log_action(tenant_id="t", action="create", resource_type="memory")

    assert get_audit_queue().queue_size == 1
