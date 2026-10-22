"""Each pytest-xdist worker runs against its own copy of the test database.

CI runs the root suite on four workers. Every session ends by deleting all
``test-tenant-`` rows (``_setup_schema``), so workers sharing one database would
delete each other's rows mid-run. ``tests/conftest.py`` therefore points each
worker at a database named after it, made from the base one as a template
before the worker runs its first test.
"""

from __future__ import annotations

import pytest

from tests.conftest import (
    BASE_TEST_DB_URL,
    TEST_DB_URL,
    XDIST_WORKER,
    _session_needs_database,
    worker_database_url,
)

pytestmark = pytest.mark.unit

URL = "postgresql+asyncpg://caura:changeme@127.0.0.1:5432/caura_test"


def test_a_serial_run_uses_the_base_database() -> None:
    assert worker_database_url(URL, None) == URL


def test_a_worker_gets_a_database_named_after_it() -> None:
    assert worker_database_url(URL, "gw2") == (
        "postgresql+asyncpg://caura:changeme@127.0.0.1:5432/caura_test_gw2"
    )


def test_only_the_database_name_changes() -> None:
    url = "postgresql+asyncpg://u:p%40ss@db.internal:6543/suite?ssl=disable"

    assert worker_database_url(url, "gw0") == (
        "postgresql+asyncpg://u:p%40ss@db.internal:6543/suite_gw0?ssl=disable"
    )


def test_this_run_points_at_its_own_database() -> None:
    """Under xdist this runs on a worker; serially it checks the base is used."""
    assert worker_database_url(BASE_TEST_DB_URL, XDIST_WORKER) == TEST_DB_URL
    if XDIST_WORKER:
        assert TEST_DB_URL != BASE_TEST_DB_URL


def test_every_test_has_its_workers_database_made_first(request) -> None:
    """Database-free tests included, since many reach the database anyway.

    Making the copy only for the first test that asks for the schema left a
    worker with no database when it started on one of those: two tests in
    ``test_mcp_doc.py`` failed with ``database "caura_gw3" does not exist``.
    """
    assert "_worker_database" in request.fixturenames


class _Test:
    """Stands in for a collected test; only its markers matter here."""

    def __init__(self, *markers: str) -> None:
        self._markers = set(markers)

    def get_closest_marker(self, name: str) -> str | None:
        return name if name in self._markers else None


def test_a_session_of_database_free_tests_makes_no_copy() -> None:
    """So ``pytest -m unit -n 4`` needs PostgreSQL only where ``-m unit`` does."""
    assert not _session_needs_database([_Test("unit"), _Test("unit")])


@pytest.mark.parametrize("markers", [(), ("integration",), ("unit", "integration")])
def test_one_test_that_needs_the_database_is_enough(markers: tuple[str, ...]) -> None:
    assert _session_needs_database([_Test("unit"), _Test(*markers)])
