"""CI guard (rule 6440b9a6): no direct DB engine/pool outside core-storage-api.

Only core-storage-api may open a SQLAlchemy async engine / asyncpg pool against
the shared Postgres DB; every other service (core-api, core-operations,
core-worker, …) must route through it over HTTP. This test fails if a forbidden
DB-connection constructor appears in service code outside the allowed package.

Exemptions: test fixtures, alembic migrations, and diagnostic/ops scripts are
not request-path services. (caura-ops is an accepted exception tracked in its
own repo; this guard covers the OSS repo only.)
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_FORBIDDEN_NAMES = {"create_async_engine", "create_engine", "async_sessionmaker", "sessionmaker"}
_FORBIDDEN_ATTRS = {"connect", "create_pool"}
_FORBIDDEN_MODULES = {"asyncpg", "psycopg", "psycopg2"}

# Only the storage API may open a direct connection.
_ALLOWED_DIR_PARTS = {"core-storage-api"}
# Path components that are not request-path services.
_EXEMPT_PARTS = {
    ".venv",
    "site-packages",
    "node_modules",
    "__pycache__",
    "tests",
    "test",
    "e2e",
    "scripts",
    "migrations",
}


def _iter_service_py():
    for p in REPO_ROOT.rglob("*.py"):
        parts = set(p.relative_to(REPO_ROOT).parts)
        if parts & _EXEMPT_PARTS or parts & _ALLOWED_DIR_PARTS:
            continue
        yield p


def _violations(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id in _FORBIDDEN_NAMES:
            hits.append((node.lineno, f.id))
        elif isinstance(f, ast.Attribute) and f.attr in _FORBIDDEN_ATTRS:
            mod = f.value.id if isinstance(f.value, ast.Name) else None
            if mod in _FORBIDDEN_MODULES:
                hits.append((node.lineno, f"{mod}.{f.attr}"))
    return hits


def test_no_direct_db_connection_outside_storage_api():
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_service_py():
        v = _violations(path)
        if v:
            offenders[str(path.relative_to(REPO_ROOT))] = v
    assert not offenders, (
        "Direct DB connection found outside core-storage-api (rule 6440b9a6). "
        "Route through the storage API over HTTP. Offenders:\n"
        + "\n".join(f"  {f}: {hits}" for f, hits in sorted(offenders.items()))
    )
