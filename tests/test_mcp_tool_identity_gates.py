"""MCP tools that persist agent-attributed state apply the same identity gates
as ``caura_write`` and their REST twins.

- ``caura_tune``, ``caura_doc op=write``, ``caura_insights`` and
  ``caura_evolve`` run an install credential's ``agent_id`` claim through the
  broker ownership boundary, so one install cannot act as an agent another
  install owns (``caura_write`` and ``caller_identity.resolve_caller_and_gate``
  already did).
- ``caura_doc op=write`` refuses an agent credential below trust 3 replacing a
  document someone else wrote (parity with REST ``POST /documents`` and with
  ``op=delete``) — a document with no recorded author stays writable — and
  records the writer so its own documents stay its own.
- ``caura_doc op=write`` refuses an agent awaiting approval (trust 0), as
  ``caura_write`` does (M-89).
- ``caura_doc`` cannot write or delete the interview service's own
  collections, for any credential (M-86).

Real in-process storage (the conftest bridge); only the request context —
tenant, credential kind, install uuid, verified agent id — is stubbed.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from core_api import mcp_server
from core_api.services.interview_service import JOBS_COLLECTION, WATERMARK_COLLECTION
from tests._mcp_test_helpers import is_error_envelope, parse_envelope
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio


def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def ctx(monkeypatch):
    """Arm the MCP request context. Returns a setter for the credential shape."""
    tenant = new_tenant_id()
    monkeypatch.setattr(mcp_server, "_check_auth", lambda: None)
    monkeypatch.setattr(mcp_server, "_check_write_scope", lambda: None)
    monkeypatch.setattr(mcp_server, "_get_tenant", lambda: tenant)

    def _arm(*, install_uuid: str | None = None, agent_header: str | None = None):
        monkeypatch.setattr(
            mcp_server, "_is_install_credential", lambda: install_uuid is not None
        )
        monkeypatch.setattr(mcp_server, "_get_install_uuid", lambda: install_uuid)
        monkeypatch.setattr(
            mcp_server,
            "_get_agent_id",
            lambda: mcp_server.AgentIdentity(agent_header) if agent_header else None,
        )

    _arm()
    return tenant, _arm


async def _seed_agent(sc, tenant: str, agent_id: str, trust: int, **extra) -> None:
    await sc.create_or_update_agent(
        {"tenant_id": tenant, "agent_id": agent_id, "trust_level": trust, **extra}
    )


# ---------------------------------------------------------------------------
# Broker ownership boundary on the non-write tools
# ---------------------------------------------------------------------------


async def test_tune_cannot_rewrite_another_installs_agent_profile(ctx, sc):
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "victim", 1, owner_install_uuid="install-B")
    arm(install_uuid="install-A")

    out = await mcp_server.caura_tune(agent_id="victim", top_k=3)

    assert parse_envelope(out)["agent_id"] == "broker:install-A"
    victim = await sc.get_agent("victim", tenant, read=False)
    assert not (victim.get("search_profile") or {}).get("top_k")


async def test_tune_still_tunes_an_agent_the_install_owns(ctx, sc):
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "mine", 1, owner_install_uuid="install-A")
    arm(install_uuid="install-A")

    out = await mcp_server.caura_tune(agent_id="mine", top_k=3)

    assert parse_envelope(out)["agent_id"] == "mine"
    assert (await sc.get_agent("mine", tenant, read=False))["search_profile"][
        "top_k"
    ] == 3


@pytest.mark.parametrize("tool", ["insights", "evolve"])
async def test_insights_and_evolve_resolve_the_install_owned_identity(
    ctx, sc, monkeypatch, tool
):
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "victim", 1, owner_install_uuid="install-B")
    arm(install_uuid="install-A")
    seen: list[str] = []

    async def _require_trust(tenant_id, agent_id, min_level):
        seen.append(agent_id)
        return 1, True, None  # not_found → the tool stops before any write

    monkeypatch.setattr(mcp_server, "_require_trust", _require_trust)
    if tool == "insights":
        await mcp_server.caura_insights(focus="stale", agent_id="victim")
    else:
        await mcp_server.caura_evolve(
            outcome="it worked", outcome_type="success", agent_id="victim"
        )

    assert seen == ["broker:install-A"]


async def test_doc_write_mint_is_attributed_to_the_install_owned_identity(
    ctx, sc, monkeypatch
):
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "victim", 1, owner_install_uuid="install-B")
    arm(install_uuid="install-A")
    mint = AsyncMock()
    monkeypatch.setattr("core_api.services.doc_memory.safe_sync_doc_memory", mint)

    out = await mcp_server.caura_doc(
        op="write",
        collection="notes",
        doc_id=f"d-{_uid()}",
        data={"body": "a note long enough to mint"},
        agent_id="victim",
    )

    assert not is_error_envelope(out), out
    assert mint.await_args.kwargs["agent_id"] == "broker:install-A"


async def test_doc_write_skills_context_is_the_install_owned_identity(
    ctx, sc, monkeypatch
):
    """M-79: the skills validator ran on the claimed ``agent_id``.

    It binds a staged draft to its author and stamps ``data.origin.agent_id``,
    so an install naming another install's agent passed that agent's draft
    ownership check and was recorded as it.
    """
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "victim", 1, owner_install_uuid="install-B")
    arm(install_uuid="install-A")
    monkeypatch.setattr(
        mcp_server,
        "get_settings_for_display",
        AsyncMock(return_value={"skills_factory": {"enabled": True}}),
    )

    out = await mcp_server.caura_doc(
        op="write",
        collection="skills",
        doc_id="forge-x",
        data={
            "name": "Forge X",
            "slug": "forge-x",
            "description": "what this skill does",
            "domain": "ops",
            "kind": "create",
            "source": "agent",
            "content": "# Forge X\n\nDo the thing safely.",
            "summary": "Use when doing the thing in ops.",
        },
        agent_id="victim",
    )

    assert not is_error_envelope(out), out
    stored = await sc.get_document(tenant, "skills", "forge-x", read=False)
    assert stored["data"]["origin"]["agent_id"] == "broker:install-A"


# ---------------------------------------------------------------------------
# caura_doc op=write — overwriting another agent's document needs trust 3
# ---------------------------------------------------------------------------


async def test_doc_write_cannot_replace_another_writers_document_below_trust_3(ctx, sc):
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "low", 1)
    doc_id = f"d-{_uid()}"
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": "notes",
            "doc_id": doc_id,
            "data": {"body": "original"},
            "agent_id": "someone-else",
        }
    )
    arm(agent_header="low")

    out = await mcp_server.caura_doc(
        op="write", collection="notes", doc_id=doc_id, data={"body": "replaced"}
    )

    assert is_error_envelope(out)
    stored = await sc.get_document(tenant, "notes", doc_id, read=False)
    assert stored["data"] == {"body": "original"}


async def test_doc_write_lets_an_agent_update_its_own_document(ctx, sc):
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "low", 1)
    doc_id = f"d-{_uid()}"
    arm(agent_header="low")

    first = await mcp_server.caura_doc(
        op="write", collection="notes", doc_id=doc_id, data={"v": 1}
    )
    second = await mcp_server.caura_doc(
        op="write", collection="notes", doc_id=doc_id, data={"v": 2}
    )

    assert not is_error_envelope(first) and not is_error_envelope(second), second
    stored = await sc.get_document(tenant, "notes", doc_id, read=False)
    assert stored["data"] == {"v": 2}
    assert stored["agent_id"] == "low"


async def test_doc_write_may_still_update_an_unowned_document(ctx, sc):
    """No recorded author (e.g. written over MCP before authors were stored)
    means unowned: a trust-1 agent keeps today's ability to update it."""
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "low", 1)
    doc_id = f"d-{_uid()}"
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": "notes",
            "doc_id": doc_id,
            "data": {"body": "shared"},
        }
    )
    arm(agent_header="low")

    out = await mcp_server.caura_doc(
        op="write", collection="notes", doc_id=doc_id, data={"body": "edited"}
    )

    assert not is_error_envelope(out), out
    stored = await sc.get_document(tenant, "notes", doc_id, read=False)
    assert stored["data"] == {"body": "edited"}


# ---------------------------------------------------------------------------
# caura_doc op=write — an agent awaiting approval writes nothing (M-89)
# ---------------------------------------------------------------------------


async def test_doc_write_refuses_an_agent_awaiting_approval(ctx, sc):
    """``caura_write`` refuses trust 0; this wrote, and minted a memory from
    the document."""
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "pending", 0)
    arm(agent_header="pending")
    doc_id = f"d-{_uid()}"

    out = await mcp_server.caura_doc(
        op="write", collection="notes", doc_id=doc_id, data={"body": "x"}
    )

    assert parse_envelope(out)["error"]["code"] == "AGENT_NOT_APPROVED"
    assert await sc.get_document(tenant, "notes", doc_id, read=False) is None


async def test_doc_write_refuses_an_agent_awaiting_approval_before_embedding(
    ctx, sc, monkeypatch
):
    """Review of caura PR #1865: the refusal ran after the synchronous embedding
    call, so an agent awaiting approval still spent provider quota."""
    tenant, arm = ctx
    await _seed_agent(sc, tenant, "pending", 0)
    arm(agent_header="pending")
    embed = AsyncMock(return_value=[0.0] * 8)
    monkeypatch.setattr("common.embedding.get_embedding", embed)

    out = await mcp_server.caura_doc(
        op="write",
        collection="notes",
        doc_id=f"d-{_uid()}",
        data={"body": "x", "summary": "A note worth indexing."},
    )

    assert parse_envelope(out)["error"]["code"] == "AGENT_NOT_APPROVED"
    embed.assert_not_awaited()


# ---------------------------------------------------------------------------
# caura_doc — the interview service's own collections (M-86)
# ---------------------------------------------------------------------------
#
# Server state written with no author, so the overwrite gate let any caller
# replace it. Refused to every credential, as storage refuses a ``_``-prefixed
# collection; a tenant credential is the widest, so it is the one tested.


@pytest.mark.parametrize("collection", [WATERMARK_COLLECTION, JOBS_COLLECTION])
async def test_doc_write_cannot_reach_the_interview_collections(ctx, sc, collection):
    tenant, _arm = ctx
    doc_id = f"d-{_uid()}"

    out = await mcp_server.caura_doc(
        op="write",
        collection=collection,
        doc_id=doc_id,
        data={"last_seq": 10**9},
        agent_id="ops",
    )

    assert is_error_envelope(out)
    assert await sc.get_document(tenant, collection, doc_id, read=False) is None


@pytest.mark.parametrize("collection", [WATERMARK_COLLECTION, JOBS_COLLECTION])
async def test_doc_delete_cannot_reach_the_interview_collections(ctx, sc, collection):
    tenant, _arm = ctx
    doc_id = f"d-{_uid()}"
    await sc.upsert_document(
        {
            "tenant_id": tenant,
            "collection": collection,
            "doc_id": doc_id,
            "data": {"last_seq": 5},
        }
    )

    out = await mcp_server.caura_doc(
        op="delete", collection=collection, doc_id=doc_id, agent_id="ops"
    )

    assert is_error_envelope(out)
    assert await sc.get_document(tenant, collection, doc_id, read=False) is not None
