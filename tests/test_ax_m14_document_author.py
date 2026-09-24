"""ax-0917-m-14 — documents carried no author.

`documents` had no author column, and `DocWriteRequest` is `extra="forbid"`,
so there was no field to send one in either. A fleet whose agents all write to
the same collection could not answer "who wrote this" — the question every
other row in this schema can answer, since `memories` has carried `agent_id`
from the beginning.

The probe that found this put `owner` inside `data` instead. That is worse than
it looks: `data` is replaced wholesale on every upsert, so the attribution
survives only as long as each writer remembers to re-send it, and no query can
find it without knowing the convention.
"""

import uuid

import pytest

from tests.conftest import get_test_auth


def _uid() -> str:
    return uuid.uuid4().hex[:8]


async def _write(client, headers, tenant_id, tag, **extra):
    return await client.post(
        "/api/v1/documents",
        json={
            "tenant_id": tenant_id,
            "collection": f"notes-{tag}",
            "doc_id": f"doc-{tag}",
            "data": {"title": "Hello"},
            **extra,
        },
        headers=headers,
    )


# ── the field exists and round-trips ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_document_records_its_author(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()

    resp = await _write(client, headers, tenant_id, tag, agent_id="writer-1")

    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["agent_id"] == "writer-1"


@pytest.mark.asyncio
async def test_a_retired_service_author_is_stored_under_the_canonical_id(client):
    tenant_id, headers = get_test_auth()
    tag = _uid()

    resp = await _write(
        client,
        headers,
        tenant_id,
        tag,
        agent_id="memclaw-doc-indexer",  # legacy-name-ok: supported client input alias
    )

    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["agent_id"] == "caura-doc-indexer"


@pytest.mark.asyncio
async def test_the_author_survives_a_read(client):
    """The `owner`-inside-`data` workaround did not survive anything. This has
    to be readable back through the ordinary GET, not just echoed by the write
    response."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    await _write(client, headers, tenant_id, tag, agent_id="writer-1")

    resp = await client.get(
        f"/api/v1/documents/doc-{tag}",
        params={"tenant_id": tenant_id, "collection": f"notes-{tag}"},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["agent_id"] == "writer-1"


@pytest.mark.asyncio
async def test_an_upsert_records_the_agent_that_wrote_this_version(client):
    """An upsert replaces the document, so the author recorded is whoever
    wrote the version now stored. Keeping the first writer would attribute
    someone else's edit to them."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    await _write(client, headers, tenant_id, tag, agent_id="writer-1")

    resp = await _write(client, headers, tenant_id, tag, agent_id="writer-2")

    assert resp.json()["agent_id"] == "writer-2"


# ── omitting it ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_omitting_the_author_stores_no_author(client):
    """A tenant-scoped credential has no agent identity to record. NULL is the
    truthful answer; naming the tenant's first agent, or a literal "unknown",
    would be a fabricated fact that reads exactly like a real one."""
    tenant_id, headers = get_test_auth()
    tag = _uid()

    resp = await _write(client, headers, tenant_id, tag)

    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["agent_id"] is None


@pytest.mark.asyncio
async def test_the_read_shape_carries_the_field_even_when_unset(client):
    """Present-and-null, not absent. A client that has to branch on whether
    the key exists cannot tell "no author" from "old server"."""
    tenant_id, headers = get_test_auth()
    tag = _uid()
    await _write(client, headers, tenant_id, tag)

    resp = await client.get(
        f"/api/v1/documents/doc-{tag}",
        params={"tenant_id": tenant_id, "collection": f"notes-{tag}"},
        headers=headers,
    )

    assert "agent_id" in resp.json()


# ── the credential wins ──────────────────────────────────────────────────


def test_an_agent_credential_naming_a_peer_is_refused():
    """A caller must not write a document under a name that is not its own.

    Refusing beats silently substituting the verified identity: a credential
    that names a peer has made a claim, and quietly rewriting it means the
    caller never learns its attribution was wrong. `enforce_self_agent` is the
    gate the rest of this surface uses for that, and
    `test_authz_gate_inventory` requires any route taking a caller-supplied
    agent identity to call it (or to justify itself in SELF_GATE_ALLOWLIST) —
    this route calls it.
    """
    from fastapi import HTTPException

    from core_api.auth import AuthContext

    auth = AuthContext(tenant_id="t1", agent_id="writer-1")

    with pytest.raises(HTTPException) as exc:
        auth.enforce_self_agent("writer-2")

    assert exc.value.status_code == 403


def test_a_tenant_scoped_key_may_still_name_an_agent():
    """`enforce_self_agent` fires only for a credential that HAS an identity.
    A tenant-scoped key writing on behalf of one of its agents is the ordinary
    case, and it is the same latitude `POST /memories` gives."""
    from core_api.auth import AuthContext

    AuthContext(tenant_id="t1", agent_id=None).enforce_self_agent("writer-1")


def test_the_route_gates_before_it_resolves():
    """The gate has to run on the body value. Resolving first and gating the
    result would compare the verified identity against itself and never fire.

    Asserted on source because the OSS test path authenticates with an admin
    key, which carries no agent identity — a route-level test here exercises
    only the ungated branch and would pass with the order reversed.
    """
    import ast
    import inspect

    from core_api.routes import documents

    src = ast.unparse(ast.parse(inspect.getsource(documents.upsert_document)))
    assert "auth.enforce_self_agent(body.agent_id)" in src
    assert src.index("enforce_self_agent") < src.index(
        "raw_author = auth.agent_id or body.agent_id"
    )
    assert "author = canonical_service_agent_id(raw_author)" in src


# ── the column ───────────────────────────────────────────────────────────


def test_the_column_is_nullable():
    """Every row written before the column existed has no author, and no
    backfill can invent one honestly."""
    from common.models.document import Document

    assert Document.__table__.c.agent_id.nullable is True


def test_the_author_lookup_is_indexed():
    """ "What has this agent written" is the query the column exists to serve;
    unindexed it scans the tenant's whole document set. Mirrors
    ``ix_memories_tenant_agent``."""
    from common.models.document import Document

    names = {ix.name for ix in Document.__table__.indexes}
    assert "ix_documents_tenant_agent" in names


def test_both_upsert_paths_persist_it():
    """Indexed and un-indexed writes go through different storage methods —
    ``document_upsert`` and ``document_upsert_returning_xmax``. A document with
    a `data.summary` takes the second one, so wiring only the first would drop
    the author for exactly the documents that are searchable."""
    import inspect

    from core_storage_api.services.postgres_service import PostgresService

    for method in (
        PostgresService.document_upsert,
        PostgresService.document_upsert_returning_xmax,
    ):
        src = inspect.getsource(method)
        assert "agent_id=agent_id" in src, method.__name__
        assert '"agent_id": agent_id' in src, f"{method.__name__} on-conflict"
