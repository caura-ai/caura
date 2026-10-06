"""M-53: a parent PATCH reaches the rows derived from it.

Auto-chunk and atomic-fact children carry their parent's text and link to it
only through ``metadata.parent_memory_id``. Every delete takes them since
caura PR #1843, but a PATCH changed the parent alone: narrowing its visibility
left the children publishing the same text wider, a new ``expires_at`` left
them on the old retention, and a content edit left them describing text that
is gone.

Owner decision (B25): narrowing cascades and widening does not, since widening
is a separate publish decision; ``expires_at`` is mirrored; a content edit
soft-deletes the children, and its re-enrichment derives atomic facts from the
new text (H-07).

Real storage through the in-process bridge, because each claim is about what
the children hold after the PATCH.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core_api.clients.storage_client import get_storage_client
from core_api.schemas import MemoryUpdate
from core_api.services import memory_service
from core_api.services.hooks import ServiceHooks
from tests.conftest import close_scheduled_coro

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

TEXT = "The quarterly plan covers hiring, the budget and the launch date."
LATER = datetime(2027, 1, 1, tzinfo=UTC)


def _tenant() -> str:
    return f"test-tenant-m53-{uuid.uuid4().hex[:8]}"


async def _row(
    tenant: str,
    content: str,
    visibility: str,
    expires_at: datetime | None = None,
    **metadata,
) -> str:
    created = await get_storage_client().create_memory(
        {
            "tenant_id": tenant,
            "agent_id": "m53-agent",
            "memory_type": "fact",
            "content": content,
            "status": "active",
            "visibility": visibility,
            "expires_at": expires_at.isoformat() if expires_at else None,
            "metadata_": metadata,
        }
    )
    return str(created["id"])


async def _family(
    tenant: str,
    *,
    visibility: str = "scope_team",
    child_visibility: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, list[str]]:
    """A parent and two rows derived from it."""
    parent = await _row(tenant, TEXT, visibility, expires_at)
    children = [
        await _row(
            tenant,
            f"The quarterly plan covers {topic}.",
            child_visibility or visibility,
            expires_at,
            parent_memory_id=parent,
        )
        for topic in ("hiring", "the budget")
    ]
    return parent, children


async def _patch(tenant: str, memory_id: str, update: MemoryUpdate) -> None:
    """``update_memory`` with enrichment off and the providers stubbed."""
    config = SimpleNamespace(
        enrichment_enabled=False,
        enrichment_provider="none",
        semantic_dedup_enabled=False,
        entity_extraction_enabled=False,
        governance_pii=None,
    )
    with (
        patch(
            "core_api.services.organization_settings.resolve_config",
            new=AsyncMock(return_value=config),
        ),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(memory_service, "track_task", side_effect=close_scheduled_coro),
    ):
        await memory_service.update_memory(uuid.UUID(memory_id), tenant, update)


async def _children(tenant: str, ids: list[str]) -> list[dict]:
    sc = get_storage_client()
    return [await sc.get_memory(i, tenant, read=False) for i in ids]


@pytest.mark.parametrize(
    ("parent_was", "children_were"),
    [
        ("scope_team", "scope_team"),
        # Named but unchanged on the parent: a retry still repairs the children.
        ("scope_agent", "scope_team"),
    ],
)
async def test_narrowing_a_parent_narrows_its_children(parent_was, children_were):
    tenant = _tenant()
    parent, children = await _family(
        tenant, visibility=parent_was, child_visibility=children_were
    )

    await _patch(tenant, parent, MemoryUpdate(visibility="scope_agent"))

    rows = await _children(tenant, children)
    assert [r["visibility"] for r in rows] == ["scope_agent", "scope_agent"]


async def test_widening_a_parent_leaves_its_children():
    """Widening is a separate publish decision, made row by row."""
    tenant = _tenant()
    parent, children = await _family(tenant, visibility="scope_agent")

    await _patch(tenant, parent, MemoryUpdate(visibility="scope_org"))

    rows = await _children(tenant, children)
    assert [r["visibility"] for r in rows] == ["scope_agent", "scope_agent"]


@pytest.mark.parametrize(("was", "now"), [(None, LATER), (LATER, None)])
async def test_a_parent_ttl_is_mirrored_to_its_children(was, now):
    tenant = _tenant()
    parent, children = await _family(tenant, expires_at=was)

    await _patch(tenant, parent, MemoryUpdate(expires_at=now))

    for row in await _children(tenant, children):
        stored = row["expires_at"]
        assert (datetime.fromisoformat(stored) if stored else None) == now, row


async def test_a_content_edit_soft_deletes_the_children():
    tenant = _tenant()
    parent, _children_ids = await _family(tenant)

    await _patch(
        tenant,
        parent,
        MemoryUpdate(content="The quarterly plan now covers only the launch date."),
    )

    assert await get_storage_client().find_children_by_parent_id(tenant, parent) == []


@pytest.mark.parametrize(
    "update",
    [
        MemoryUpdate(content="The quarterly plan now covers only the launch date."),
        MemoryUpdate(visibility="scope_agent"),
        MemoryUpdate(expires_at=LATER),
    ],
    ids=["content", "narrowing", "ttl"],
)
async def test_a_failed_parent_write_leaves_the_children(update):
    """The children change in the parent's own storage write, so when that write
    fails they are as they were, and the caller's retry edits the whole family."""
    tenant = _tenant()
    parent, children = await _family(tenant)
    before = await _children(tenant, children)

    with (
        patch.object(
            type(get_storage_client()),
            "update_memory",
            new=AsyncMock(side_effect=RuntimeError("storage unavailable")),
        ),
        pytest.raises(RuntimeError),
    ):
        await _patch(tenant, parent, update)

    assert await _children(tenant, children) == before


async def _edit_with_a_race(tenant: str, parent: str, race) -> list[str]:
    """Edit ``parent``'s text, running ``race`` just before the PATCH reaches
    storage, after core-api has read what it needs; returns the ids audited as
    deleted."""
    client_type = type(get_storage_client())
    real_update = client_type.update_memory
    audited: list[str] = []

    async def racing_update(self, memory_id, tenant_id, data):
        await race()
        return await real_update(self, memory_id, tenant_id, data)

    async def capture(**entry):
        if entry.get("action") == "soft_delete":
            audited.append(str(entry["resource_id"]))

    with (
        patch.object(client_type, "update_memory", new=racing_update),
        patch.object(
            memory_service, "get_hooks", return_value=ServiceHooks(audit_log=capture)
        ),
    ):
        await _patch(
            tenant,
            parent,
            MemoryUpdate(content="The quarterly plan now covers only the launch date."),
        )
    return audited


async def test_a_child_written_after_the_edit_read_its_children_goes_too():
    """A fan-out of the old text that commits after core-api looked: the edit
    takes every child live when it lands, not the ones it saw first."""
    tenant = _tenant()
    parent, children = await _family(tenant)
    late: list[str] = []

    async def a_late_fan_out():
        late.append(await _row(tenant, "Late.", "scope_team", parent_memory_id=parent))

    audited = await _edit_with_a_race(tenant, parent, a_late_fan_out)

    assert await get_storage_client().find_children_by_parent_id(tenant, parent) == []
    assert sorted(audited) == sorted([*children, *late])


async def test_the_audit_names_only_the_children_the_edit_deleted():
    """A child another request deleted first is that request's to audit."""
    tenant = _tenant()
    parent, children = await _family(tenant)

    async def a_concurrent_delete():
        await get_storage_client().soft_delete_by_ids(tenant, [children[1]])

    audited = await _edit_with_a_race(tenant, parent, a_concurrent_delete)

    assert audited == [children[0]]
