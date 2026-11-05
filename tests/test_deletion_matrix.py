"""``docs/deletion-matrix.md`` held to the code.

* Every tenant-scoped table has a row in the matrix, every row's verdicts are
  ones the matrix defines, and every table it names exists.
* A memory written with a unique token is deleted, then purged. After each
  step every table is searched for the token, which may remain only in a table
  the matrix marks **Kept** for that step.

Full path for the second: core-api over REST -> the in-process storage app
(conftest ASGI bridge) -> test DB.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text

from core_api.clients.storage_client import get_storage_client
from core_api.tasks import _background_tasks
from core_storage_api.services.postgres_service import get_session
from tests.conftest import get_test_auth, uid

_MATRIX = Path(__file__).resolve().parents[1] / "docs" / "deletion-matrix.md"
# A database row: the table first, then its memory-delete and retention-purge
# verdicts, each the bold word that opens its cell.
_ROW = re.compile(
    r"^\| `([a-z_]+)`[^|]*\|[^|]*\| \*\*([^*]+)\*\*[^|]*\| \*\*([^*]+)\*\*"
)
_VERDICTS = {"Kept", "Cleared", "No text"}


def _matrix() -> dict[str, tuple[set[str], set[str]]]:
    """Each table's verdicts on a memory delete and on the retention purge."""
    rows: dict[str, tuple[set[str], set[str]]] = {}
    for line in _MATRIX.read_text().splitlines():
        if match := _ROW.match(line):
            table, on_delete, on_purge = match.groups()
            verdicts = rows.setdefault(table, (set(), set()))
            verdicts[0].add(on_delete)
            verdicts[1].add(on_purge)
    return rows


async def _tables(sql: str) -> set[str]:
    async with get_session() as session:
        return {row[0] for row in await session.execute(text(sql))}


async def test_the_matrix_names_every_tenant_table_and_only_real_ones():
    matrix = _matrix()
    tenant_tables = await _tables(
        "SELECT DISTINCT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name IN ('tenant_id', 'org_id')"
    )
    public_tables = await _tables(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )

    assert tenant_tables - matrix.keys() == set(), "tables missing from the matrix"
    assert matrix.keys() - public_tables == set(), "tables the schema does not have"
    for table, (on_delete, on_purge) in matrix.items():
        assert on_delete | on_purge <= _VERDICTS, table


def _token() -> str:
    """Letters only, so the fake entity extractor takes it into a name too."""
    return "qz" + "".join(chr(ord("a") + int(digit, 16)) for digit in uuid4().hex[:12])


async def _settle() -> None:
    """Let the request's background work (enrichment, extraction) finish."""
    for _ in range(10):
        if not _background_tasks:
            return
        await asyncio.wait(set(_background_tasks), timeout=30)


async def _tables_holding(token: str) -> set[str]:
    """Every table with a row whose text contains ``token``, in any case."""
    tables = await _tables(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    found = set()
    async with get_session() as session:
        for table in sorted(tables):
            hit = await session.execute(
                text(
                    f'SELECT 1 FROM public."{table}" t WHERE t::text ILIKE :pattern LIMIT 1'
                ),
                {"pattern": f"%{token}%"},
            )
            if hit.first():
                found.add(table)
    return found


async def test_a_deleted_memory_leaves_text_only_where_the_matrix_says(client):
    matrix = _matrix()
    kept_on_delete = {
        table for table, (verdicts, _) in matrix.items() if "Kept" in verdicts
    }
    kept_on_purge = {
        table for table, (_, verdicts) in matrix.items() if "Kept" in verdicts
    }
    tenant, token = f"tenant-matrix-{uid()}", _token()
    _, headers = get_test_auth(tenant)

    written = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "agent_id": "matrix-agent",
            "memory_type": "fact",
            "content": f"Dana Levi signed the {token.capitalize()} Holdings lease on Friday.",
        },
        headers={**headers, "Idempotency-Key": f"matrix-{uid()}"},
    )
    assert written.status_code == 201, written.text
    await _settle()
    assert "memories" in await _tables_holding(token), "the search finds nothing"

    deleted = await client.delete(
        f"/api/v1/memories/{written.json()['id']}?tenant_id={tenant}", headers=headers
    )
    assert deleted.status_code == 204, deleted.text
    await _settle()
    found = await _tables_holding(token)
    assert found <= kept_on_delete, found - kept_on_delete

    # Past the retention window, and past the replay's expiry.
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE memories SET deleted_at = now() - interval '31 days' "
                "WHERE tenant_id = :tenant AND deleted_at IS NOT NULL"
            ),
            {"tenant": tenant},
        )
        await session.execute(
            text(
                "UPDATE idempotency_responses SET expires_at = now() - interval '1 minute' "
                "WHERE tenant_id = :tenant"
            ),
            {"tenant": tenant},
        )
    await get_storage_client().purge_soft_deleted(tenant, retention_days=30)
    await _settle()
    found = await _tables_holding(token)
    assert found <= kept_on_purge, found - kept_on_purge
