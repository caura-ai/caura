"""pm-0918-c-02 — ``POST /api/v1/search`` must never answer with a blank ``content``.

THE REPORT. For ~75 minutes after a 3,500-chunk bulk write, ``/search`` came
back with rows whose ``content`` was empty — 17% of the top-50, on a store that
had been written and queried immediately. The stored rows were fine (zero
empty-content rows in either local database), so the emptiness was said to be
in the response.

WHY THIS FILE EXISTS RATHER THAN A FIX. Everything an investigation can reach
from the code says the REST route cannot produce that response, and the reason
is one line: ``MemoryOut.content`` is ``str`` with **no default**. A row that
reaches the serialiser without content does not degrade to ``""`` — it raises,
and the search fails loudly. The window in the report is exactly the window in
which rows are un-embedded and un-enriched, and that is the state nothing had
ever driven the real route in. So that is what these cases do: they hold the
route to the contract in the states that occur right after a bulk write, and
they fail if anything ever makes a blank row servable.

THE THREE SERIALISATION PATHS A SEARCH RESPONSE CAN COME THROUGH, one case each:

  1. scored search, row embedded — storage projects ``MEMORY_LIST_FIELDS`` and
     ``ExecuteScoredSearch`` maps it onto a row object;
  2. scored search, row NOT embedded — the post-bulk-write state.
     ``passes_relevance_filter`` short-circuits on ``has_embedding is False``,
     so such a row bypasses the similarity floor entirely and is admitted on its
     full-text match alone. This is the only way a row whose vector has not
     landed can appear in a result set, and it is the state the report
     describes;
  3. successor injection — ``LoadAndSerialize`` appends rows fetched by
     ``find_successors``, which never pass through the scored-search projection
     at all. A narrowed projection there is the one shape that could put a row
     shell into an otherwise normal result set, so it gets a case with the
     field actually removed.

Case 4 is the regression guard: give ``MemoryOut.content`` a default and it is
the one that stops failing.

TWO HARNESS FACTS THE CASES BELOW HAVE TO COMPENSATE FOR, both of which are why
path 2 had never been covered:

  * the test database is built from the ORM models, so migration 001's
    ``search_vector`` trigger does not exist and the column is NULL on every
    row — full-text match is dead suite-wide, and with it the only admission
    route an un-embedded row has. ``_index_for_fts`` rebuilds the vector for one
    tenant with migration 034's own expression;
  * ``track_task`` runs the deferred re-embed in-process, so rows written by a
    "deferred" bulk are embedded again before the next ``await`` — measured:
    the vectors were NULL in the database and present by the time the search
    ran one statement later. ``embedding_pending`` stays True regardless (only
    core-worker clears it), so the flag is NOT evidence of the state and the
    cases below assert on ``has_embedding`` from the diagnostic instead.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa

from core_api.config import settings
from core_api.pipeline.steps.search import load_and_serialize as las
from core_api.services import memory_service
from core_storage_api.services.postgres_service import get_session
from tests.conftest import close_scheduled_coro, get_test_auth

pytestmark = pytest.mark.integration

# Migration 034's expression, verbatim. Duplicated rather than imported because
# the migration builds it as a trigger body string; if the two ever diverge the
# FTS assertions here stop matching what production indexes, which is worth a
# grep-able copy.
_SEARCH_VECTOR_SQL = (
    "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))"
)


def _tenant() -> str:
    return f"t-c02-{uuid.uuid4().hex[:10]}"


@pytest.fixture
def pipeline_search(monkeypatch):
    """Pin the production (pipeline) search path.

    ``tests/pipeline/test_search_pipeline.py`` sets ``_USE_PIPELINE_SEARCH =
    False`` without restoring it, so without this a case that happens to run
    after it asserts against the deprecated legacy path instead.
    """
    monkeypatch.setattr(memory_service, "_USE_PIPELINE_SEARCH", True)


async def _index_for_fts(tenant_id: str) -> None:
    """Populate ``search_vector`` for one tenant, as the production trigger would.

    Without this the tenant's rows are unreachable by full-text match, which is
    the only admission route a row with no vector has — so the un-embedded case
    below would return nothing and assert nothing.
    """
    async with get_session() as session:
        await session.execute(
            sa.text(
                f"UPDATE memories SET search_vector = {_SEARCH_VECTOR_SQL} WHERE tenant_id = :t"
            ),
            {"t": tenant_id},
        )
        await session.commit()


async def _bulk_write(client, headers, tenant_id, agent_id, contents):
    resp = await client.post(
        "/api/v1/memories/bulk",
        headers={**headers, "X-Bulk-Attempt-Id": f"c02-{uuid.uuid4().hex}"},
        json={
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "items": [{"content": c} for c in contents],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["errors"] == 0, body
    return body


async def _search(client, headers, tenant_id, query, **extra):
    body = {"tenant_id": tenant_id, "query": query, "top_k": 50, "diagnostic": True}
    body.update(extra)
    return await client.post("/api/v1/search", headers=headers, json=body)


def _blank(items) -> list[dict]:
    """Rows of the shape the report flagged: returned, but with no content."""
    return [it for it in items if not (it.get("content") or "").strip()]


# ── the post-bulk-write window ───────────────────────────────────────────────


async def test_search_returns_full_content_for_rows_whose_vector_has_not_landed(
    client, monkeypatch, pipeline_search
):
    """THE REPORTED STATE. Bulk write, no vectors yet, query immediately.

    Suppressing ``track_task`` is what holds the rows in that state — it is the
    deferred backfill, and in-process it completes before the search would
    otherwise run. These rows therefore reach the result set the way production
    rows do during a long backfill: on full-text match, with the similarity
    floor skipped because ``has_embedding`` is False.
    """
    monkeypatch.setattr(settings, "deployment_mode", "deferred")
    monkeypatch.setattr(memory_service, "track_task", close_scheduled_coro)
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    tag = uuid.uuid4().hex[:8]
    contents = [
        f"Chunk {i} of the quarterly infrastructure runbook {tag}" for i in range(12)
    ]

    await _bulk_write(client, headers, tenant_id, "bulk-agent", contents)
    await _index_for_fts(tenant_id)

    resp = await _search(
        client, headers, tenant_id, f"quarterly infrastructure runbook {tag}"
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    items = payload["items"]
    assert items, "rows written seconds ago must be reachable by their own words"

    candidates = payload["diagnostic"]["all_candidates"]
    assert any(c["has_embedding"] is False for c in candidates), (
        "every candidate was embedded — the deferred window this case exists to "
        "cover was not exercised, so the floor-bypass path went untested"
    )
    assert not _blank(items), (
        f"{len(_blank(items))}/{len(items)} rows came back with empty content"
    )


async def test_search_returns_full_content_once_the_vectors_have_landed(
    client, pipeline_search
):
    """The settled state, for contrast: embedded rows on the ordinary scored path.

    Same route, same assertion, the other arm of ``passes_relevance_filter`` —
    these rows clear the floor on merit, so a blanking serialiser on the normal
    path shows up here and not above.
    """
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    tag = uuid.uuid4().hex[:8]
    contents = [
        f"Chunk {i} of the incident retrospective archive {tag}" for i in range(12)
    ]

    await _bulk_write(client, headers, tenant_id, "bulk-agent", contents)

    resp = await _search(
        client, headers, tenant_id, f"incident retrospective archive {tag}"
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["items"]
    assert any(
        c["has_embedding"] is True for c in payload["diagnostic"]["all_candidates"]
    )
    assert not _blank(payload["items"])


# ── the successor-injection path ─────────────────────────────────────────────


async def _stale_row(client, headers, tenant_id) -> tuple[str, str]:
    """Write a row, mark it conflicted, and return ``(id, the words that find it)``.

    A row in ``CONTRADICTED_STATUSES`` inside the result set is what makes
    ``LoadAndSerialize`` run its successor lookup; without one the injection
    branch is dead and the cases below would pass without reaching the code
    they name. The searches that use this pass ``status_filter="conflicted"``,
    which is the documented way to see such a row — the default query excludes
    ``outdated``/``conflicted`` outright, carving out only an exact lexical
    match.
    """
    tag = uuid.uuid4().hex[:8]
    content = f"The deploy freeze window is Friday afternoon {tag}"
    resp = await client.post(
        "/api/v1/memories",
        headers=headers,
        json={"tenant_id": tenant_id, "agent_id": "c02-agent", "content": content},
    )
    assert resp.status_code == 201, resp.text
    mid = resp.json()["id"]
    patched = await client.patch(
        f"/api/v1/memories/{mid}",
        headers=headers,
        params={"tenant_id": tenant_id},
        json={"status": "conflicted"},
    )
    assert patched.status_code == 200, patched.text
    return mid, content


def _successor_row(stale_id: str, tenant_id: str, *, content: str | None) -> dict:
    """A ``find_successors`` row, optionally with ``content`` removed.

    Removing the key is the shape a narrowed storage projection produces:
    ``orm_to_dict`` reads every field with ``getattr(obj, f, None)``, so a
    column that stopped being selected arrives as a missing value rather than
    as an error.
    """
    row = {
        "id": str(uuid.uuid4()),
        "tenant_id": tenant_id,
        "fleet_id": None,
        "agent_id": "c02-agent",
        "memory_type": "fact",
        "title": "the corrected window",
        "content": content,
        "weight": 0.5,
        "source_uri": None,
        "run_id": None,
        "metadata_": None,
        "created_at": "2026-09-01T00:00:00+00:00",
        "expires_at": None,
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
        "ts_valid_start": None,
        "ts_valid_end": None,
        "status": "active",
        "visibility": "scope_team",
        "recall_count": 0,
        "last_recalled_at": None,
        "supersedes_id": str(stale_id),
    }
    if content is None:
        del row["content"]
    return row


class _SuccessorStub:
    """Stubs ONLY ``find_successors``; the rest of the request uses real storage.

    ``LoadAndSerialize`` resolves ``get_storage_client`` through its own module
    namespace, so patching it there reaches the injection lookup and nothing
    else in the route.
    """

    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def find_successors(self, _payload):
        return self._rows


async def test_an_injected_successor_carries_its_content(
    client, monkeypatch, pipeline_search
):
    """A row that arrives by injection rather than by ranking still has content.

    Injected rows skip the scored-search projection entirely, so nothing the
    ordinary path proves says anything about theirs.
    """
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    stale_id, query = await _stale_row(client, headers, tenant_id)
    successor = _successor_row(
        stale_id, tenant_id, content="The freeze window moved to Monday"
    )
    monkeypatch.setattr(las, "get_storage_client", lambda: _SuccessorStub([successor]))

    resp = await _search(client, headers, tenant_id, query, status_filter="conflicted")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [it for it in items if it.get("injected")], (
        "the conflicted row's successor was not injected — the case is vacuous"
    )
    assert not _blank(items)


async def test_a_contentless_successor_is_never_served_as_a_blank_row(
    client, monkeypatch, pipeline_search
):
    """THE GUARD. A row reaching the serialiser without content must not become ``""``.

    This is the failure mode the report describes — a row shell among ordinary
    results — and the only one this route could plausibly produce. The contract
    is that it fails loudly instead: ``MemoryOut.content`` is required, so the
    search raises rather than answering 200 with an empty string.

    Written as a behaviour rather than an assertion about the model because the
    model alone does not carry the contract. Verified by mutation, in both
    directions: giving ``MemoryOut.content`` a ``""`` default does NOT make this
    pass — ``_memory_to_out`` passes the key explicitly, so ``None`` still fails
    validation and the default never engages. What DOES make it pass is
    ``content=_mem_attr(memory, "content") or ""`` in ``_memory_to_out``, and
    that is the realistic defect: the ``or ""`` coalescing idiom is used on
    eight other content reads in this codebase, just not on this one. That
    single-token edit is what this case exists to catch.

    Its setup is identical to the case above, which asserts that injection
    actually happens — so this one cannot pass merely by never reaching the
    branch.
    """
    tenant_id = _tenant()
    headers = get_test_auth(tenant_id)[1]
    stale_id, query = await _stale_row(client, headers, tenant_id)
    successor = _successor_row(stale_id, tenant_id, content=None)
    assert "content" not in successor
    monkeypatch.setattr(las, "get_storage_client", lambda: _SuccessorStub([successor]))

    try:
        resp = await _search(
            client, headers, tenant_id, query, status_filter="conflicted"
        )
    except Exception:
        # Loud failure IS the contract: a contentless row must not be servable.
        return

    assert resp.status_code != 200 or not _blank(resp.json()["items"]), (
        "a row with no content was served with a 200 — this is the reported bug"
    )
