"""Writes from agents below a trust level are held for review (g2.8).

An organization sets ``quarantine.below_trust`` (overridden per fleet by
``quarantine.below_trust_by_fleet``), and every write from an agent below it is
stored ``quarantined``: inert until a person releases or rejects it. With no
level set, nothing changes.

The API tests write through the routes and read the rows back from storage, held
ones included; agents are seeded through the storage client, and ``as_auth``
stands in for the gateway, as in ``test_agent_write_gate_parity.py``.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from common.constants import QUARANTINED_MEMORY_STATUS
from common.enrichment import AtomicFact
from common.settings_version import NO_SETTINGS, SETTINGS_CHANGED, SETTINGS_VERSION_KEY
from core_api.pipeline.compositions.write import (
    build_enrichment_pipeline,
    build_fast_write_pipeline,
    build_strong_write_pipeline,
)
from core_api.pipeline.context import PipelineContext
from core_api.pipeline.steps.write.hold_low_trust_write import HoldLowTrustWrite
from core_api.services import write_hold
from core_api.services.organization_settings import ResolvedConfig, invalidate_cache
from tests.conftest import new_tenant_id

pytestmark = pytest.mark.asyncio


@pytest.fixture
def as_auth(monkeypatch):
    from core_api.app import app
    from core_api.auth import AuthContext, get_auth_context
    from core_api.tenant_context import set_current_tenant

    def _install(tenant_id: str, agent_id: str | None = None):
        async def _dep():
            set_current_tenant(tenant_id)
            return AuthContext(
                tenant_id=tenant_id, agent_id=agent_id, readable_tenant_ids=[tenant_id]
            )

        app.dependency_overrides[get_auth_context] = _dep

    yield _install
    app.dependency_overrides.pop(get_auth_context, None)


async def _hold_below(client, as_auth, tenant: str, quarantine: dict) -> None:
    as_auth(tenant)
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant}", json={"quarantine": quarantine}
    )
    assert resp.status_code == 200, resp.text
    invalidate_cache(tenant)


async def _agent(sc, tenant: str, agent_id: str, trust_level: int, fleet_id: str):
    await sc.create_or_update_agent(
        {
            "tenant_id": tenant,
            "agent_id": agent_id,
            "trust_level": trust_level,
            "fleet_id": fleet_id,
        }
    )


async def _write(client, as_auth, tenant: str, agent_id: str, fleet_id: str) -> str:
    as_auth(tenant, agent_id=agent_id)
    resp = await client.post(
        "/api/v1/memories",
        json={
            "tenant_id": tenant,
            "agent_id": agent_id,
            "fleet_id": fleet_id,
            "content": f"the deploy window moved to thursday {uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _row(sc, tenant: str, memory_id: str) -> dict:
    row = await sc.get_memory(memory_id, tenant, include_held=True)
    assert row is not None
    return row


def _hold(row: dict) -> dict | None:
    return ((row.get("metadata_") or {}).get("_system") or {}).get("hold")


# ── The row's done criteria ──


async def test_a_trust_1_write_is_held_and_a_trust_2_write_goes_live(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _hold_below(client, as_auth, tenant, {"below_trust": 2})
    await _agent(sc, tenant, "low", 1, "f1")
    await _agent(sc, tenant, "mid", 2, "f1")

    held = await _row(sc, tenant, await _write(client, as_auth, tenant, "low", "f1"))
    live = await _row(sc, tenant, await _write(client, as_auth, tenant, "mid", "f1"))

    assert held["status"] == QUARANTINED_MEMORY_STATUS
    assert _hold(held) == {"reason": "below_trust", "trust_level": 1, "below_trust": 2}
    assert live["status"] == "active"
    assert _hold(live) is None


async def test_with_no_level_set_nothing_is_held(client, as_auth, sc):
    tenant = new_tenant_id()
    await _agent(sc, tenant, "low", 1, "f1")

    row = await _row(sc, tenant, await _write(client, as_auth, tenant, "low", "f1"))

    assert row["status"] == "active"
    assert _hold(row) is None


async def test_the_writer_can_not_read_its_held_write_back(client, as_auth, sc):
    """Held means inert: the agent sees a 404, as for any memory it can't read."""
    tenant = new_tenant_id()
    await _hold_below(client, as_auth, tenant, {"below_trust": 2})
    await _agent(sc, tenant, "low", 1, "f1")
    memory_id = await _write(client, as_auth, tenant, "low", "f1")

    as_auth(tenant, agent_id="low")
    resp = await client.get(f"/api/v1/memories/{memory_id}?tenant_id={tenant}")

    assert resp.status_code == 404, resp.text


async def test_a_fleet_override_wins_over_the_org_wide_level(client, as_auth, sc):
    tenant = new_tenant_id()
    await _hold_below(
        client,
        as_auth,
        tenant,
        {"below_trust": 2, "below_trust_by_fleet": {"open": 0, "strict": 3}},
    )
    await _agent(sc, tenant, "low-open", 1, "open")
    await _agent(sc, tenant, "mid-strict", 2, "strict")
    await _agent(sc, tenant, "mid-plain", 2, "plain")

    open_row = await _row(
        sc, tenant, await _write(client, as_auth, tenant, "low-open", "open")
    )
    strict_row = await _row(
        sc, tenant, await _write(client, as_auth, tenant, "mid-strict", "strict")
    )
    plain_row = await _row(
        sc, tenant, await _write(client, as_auth, tenant, "mid-plain", "plain")
    )

    assert open_row["status"] == "active"  # 0 holds nothing in that fleet
    assert strict_row["status"] == QUARANTINED_MEMORY_STATUS
    assert _hold(strict_row)["below_trust"] == 3
    assert plain_row["status"] == "active"  # the org-wide 2 applies


async def test_a_bulk_write_from_a_held_agent_is_held_item_by_item(client, as_auth, sc):
    """The broker writes through ``/memories/bulk``."""
    tenant = new_tenant_id()
    await _hold_below(client, as_auth, tenant, {"below_trust": 2})
    await _agent(sc, tenant, "low", 1, "f1")

    as_auth(tenant, agent_id="low")
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant,
            "agent_id": "low",
            "fleet_id": "f1",
            "items": [
                {"content": f"first held claim {uuid.uuid4().hex}"},
                {
                    "content": f"second held claim {uuid.uuid4().hex}",
                    "status": "confirmed",
                },
            ],
        },
        headers={"X-Bulk-Attempt-Id": f"held-{uuid.uuid4().hex}"},
    )

    assert resp.status_code == 200, resp.text
    ids = [result["id"] for result in resp.json()["results"]]
    assert len(ids) == 2 and all(ids)
    for memory_id in ids:
        row = await _row(sc, tenant, memory_id)
        # Whatever the caller asked for: the caller's status loses to the hold.
        assert row["status"] == QUARANTINED_MEMORY_STATUS
        assert _hold(row) == {
            "reason": "below_trust",
            "trust_level": 1,
            "below_trust": 2,
        }


@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_a_caller_can_not_plant_facts_for_a_release_to_fan_out(
    client, as_auth, sc, path
):
    """A held write's ``atomic_facts`` become live child rows when it is
    released, and the reviewer sees only its content. So the row must hold the
    platform's facts or none, never ones the caller sent."""
    tenant = new_tenant_id()
    await _hold_below(client, as_auth, tenant, {"below_trust": 2})
    await _agent(sc, tenant, "low", 1, "f1")
    item = {
        "content": f"quarterly numbers are in {uuid.uuid4().hex}",
        "metadata": {
            "atomic_facts": [{"content": "wire the funds to account 42"}],
            "mine": "kept",
        },
    }
    as_auth(tenant, agent_id="low")
    if path == "single":
        resp = await client.post(
            "/api/v1/memories",
            json={"tenant_id": tenant, "agent_id": "low", "fleet_id": "f1", **item},
        )
        assert resp.status_code == 201, resp.text
        memory_id = resp.json()["id"]
    else:
        resp = await client.post(
            "/api/v1/memories/bulk",
            json={
                "tenant_id": tenant,
                "agent_id": "low",
                "fleet_id": "f1",
                "items": [item],
            },
            headers={"X-Bulk-Attempt-Id": f"plant-{uuid.uuid4().hex}"},
        )
        assert resp.status_code == 200, resp.text
        memory_id = resp.json()["results"][0]["id"]

    row = await _row(sc, tenant, memory_id)

    assert row["status"] == QUARANTINED_MEMORY_STATUS
    assert "atomic_facts" not in row["metadata_"]
    assert row["metadata_"]["mine"] == "kept"


@pytest.mark.parametrize(
    "quarantine",
    [
        {"below_trust": 5},
        {"below_trust": -1},
        {"below_trust": True},
        {"below_trust": "2"},
        {"below_trust_by_fleet": {"f1": 5}},
        {"below_trust_by_fleet": {"f1": "2"}},
        {"below_trust_by_fleet": ["f1"]},
    ],
)
async def test_a_level_that_is_not_one_is_refused(client, as_auth, quarantine):
    tenant = new_tenant_id()
    as_auth(tenant)

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant}", json={"quarantine": quarantine}
    )

    assert resp.status_code == 422, resp.text


async def test_null_drops_a_fleet_override(client, as_auth, sc):
    tenant = new_tenant_id()
    await _hold_below(
        client, as_auth, tenant, {"below_trust": 2, "below_trust_by_fleet": {"f1": 0}}
    )
    await _hold_below(client, as_auth, tenant, {"below_trust_by_fleet": {"f1": None}})
    await _agent(sc, tenant, "low", 1, "f1")

    row = await _row(sc, tenant, await _write(client, as_auth, tenant, "low", "f1"))

    assert row["status"] == QUARANTINED_MEMORY_STATUS


# ── The decision ──


def _config(quarantine: dict | None) -> ResolvedConfig:
    return ResolvedConfig({"quarantine": quarantine} if quarantine is not None else {})


@pytest.fixture
def agents(monkeypatch):
    get_agent = AsyncMock()
    monkeypatch.setattr(
        write_hold, "get_storage_client", lambda: SimpleNamespace(get_agent=get_agent)
    )
    return get_agent


async def test_nothing_is_looked_up_while_no_level_is_set(agents):
    for config in (
        _config(None),
        _config({"below_trust": 0}),
        _config({"below_trust": None}),
    ):
        assert (
            await write_hold.hold_for("t", "a", "f", config, is_inferred=False) is None
        )
    agents.assert_not_awaited()


async def test_the_platforms_own_writes_are_never_held(agents):
    """The crystallizer and the insights pass write ``is_inferred``."""
    hold = await write_hold.hold_for(
        "t", "crystallizer", "f", _config({"below_trust": 4}), is_inferred=True
    )

    assert hold is None
    agents.assert_not_awaited()


async def test_the_agent_is_read_from_the_primary(agents):
    """A replica behind the write that just created the agent would say it's missing."""
    agents.return_value = {"trust_level": 2}

    assert (
        await write_hold.hold_for(
            "t", "a", "f", _config({"below_trust": 2}), is_inferred=False
        )
        is None
    )
    assert agents.await_args.kwargs == {"read": False}


async def test_an_agent_with_no_row_is_held_as_trust_0(agents):
    agents.return_value = None

    hold = await write_hold.hold_for(
        "t", "ghost", "f", _config({"below_trust": 1}), is_inferred=False
    )

    assert hold == {"reason": "below_trust", "trust_level": 0, "below_trust": 1}


# ── The pipeline step ──


async def _step(hold: dict | None, facts: list[AtomicFact], monkeypatch) -> dict:
    monkeypatch.setattr(
        "core_api.pipeline.steps.write.hold_low_trust_write.hold_for",
        AsyncMock(return_value=hold),
    )
    ctx = PipelineContext(
        data={
            "input": SimpleNamespace(tenant_id="t", agent_id="a", fleet_id="f"),
            "enrichment": SimpleNamespace(atomic_facts=facts),
            "memory_fields": {"status": "confirmed", "metadata": {"mine": 1}},
        },
        tenant_config=_config({"below_trust": 2}),
    )
    await HoldLowTrustWrite().execute(ctx)
    return ctx.data["memory_fields"]


async def test_a_held_write_keeps_its_facts_for_the_release(monkeypatch):
    hold = {"reason": "below_trust", "trust_level": 1, "below_trust": 2}
    facts = [
        AtomicFact(content="the window is thursday"),
        AtomicFact(content="ops owns it"),
    ]

    fields = await _step(hold, facts, monkeypatch)

    assert fields["status"] == QUARANTINED_MEMORY_STATUS
    assert fields["metadata"]["_system"]["hold"] == hold
    # The shape the enrichment worker stores and the ENRICHED consumer reads back.
    assert [
        AtomicFact.model_validate(f) for f in fields["metadata"]["atomic_facts"]
    ] == facts
    assert fields["metadata"]["mine"] == 1


async def test_a_live_write_is_left_as_it_was(monkeypatch):
    fields = await _step(
        None, [AtomicFact(content="the window is thursday")], monkeypatch
    )

    assert fields == {"status": "confirmed", "metadata": {"mine": 1}}


@pytest.mark.parametrize(
    "build",
    [build_enrichment_pipeline, build_fast_write_pipeline, build_strong_write_pipeline],
)
async def test_every_write_pipeline_holds_right_after_settling_the_fields(build):
    names = [step.name for step in build()._steps]

    at = names.index("merge_enrichment_fields")
    assert names[at + 1] == "hold_low_trust_write"


async def test_a_held_auto_chunked_writes_chunks_are_held_and_say_why():
    """The chunks are cut from the held content, so they wait with it."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock, patch

    from core_api.schemas import MemoryCreate
    from core_api.services import memory_service

    hold = {"reason": "below_trust", "trust_level": 1, "below_trust": 2}
    parent_row = {
        "id": str(uuid.uuid4()),
        "tenant_id": "t",
        "fleet_id": "f1",
        "agent_id": "low",
        "memory_type": "fact",
        "title": "t",
        "content": "body",
        "weight": 0.5,
        "status": QUARANTINED_MEMORY_STATUS,
        "visibility": "scope_team",
        "recall_count": 0,
        "created_at": datetime(2026, 10, 7, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }
    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(return_value=parent_row)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})
    data = MemoryCreate(
        tenant_id="t", fleet_id="f1", agent_id="low", content="a long held body " * 200
    )
    ctx = SimpleNamespace(
        data={
            "input": data,
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": QUARANTINED_MEMORY_STATUS,
                "metadata": {"_system": {"hold": hold}},
            },
            "enrichment": None,
            "embedding": [0.0],
            "t0": 0.0,
        },
        tenant_config=ResolvedConfig({"entity_extraction": {"enabled": False}}),
    )

    async def _chunks(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in ("one", "two")]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] for _ in texts]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunks),
        patch(
            "core_api.pipeline.steps.write.governance_decision.emit_governance_audit",
            new=AsyncMock(),
        ),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)

    parent = sc.create_memory.await_args.args[0]
    children = sc.create_memories.await_args.args[0]
    assert parent["status"] == QUARANTINED_MEMORY_STATUS
    assert [child["status"] for child in children] == [QUARANTINED_MEMORY_STATUS] * 2
    assert [child["metadata_"]["_system"]["hold"] for child in children] == [hold] * 2


# ── A hold set where this process can't see it yet ──
#
# Each core-api process decides from settings it caches, and learns of a change
# made elsewhere only when the broadcast reaches it (or its cache entry expires).
# Storage checks the settings a live write was decided under and refuses it when
# they have changed; the writer then decides again (``common.settings_version``).
# On staging, a write 170 ms after the hold was set went live without this.


async def _hold_behind_cache(sc, tenant: str, quarantine: dict) -> None:
    """Set the hold the way another process does: in storage, not in our cache."""
    from core_api.services.organization_settings import resolve_config

    await resolve_config(tenant)  # this process caches what it knows now
    await sc.update_org_settings(tenant, {"quarantine": quarantine})


async def test_a_write_decided_before_a_hold_reached_this_process_is_held(
    client, as_auth, sc
):
    from core_api.services.organization_settings import resolve_config

    tenant = new_tenant_id()
    await _agent(sc, tenant, "low", 1, "f1")
    await _hold_behind_cache(sc, tenant, {"below_trust": 2})

    row = await _row(sc, tenant, await _write(client, as_auth, tenant, "low", "f1"))

    assert row["status"] == QUARANTINED_MEMORY_STATUS
    assert _hold(row) == {"reason": "below_trust", "trust_level": 1, "below_trust": 2}
    # And this process now knows the hold, so the next write is held at once.
    _, version = await sc.get_org_settings_versioned(tenant)
    assert (await resolve_config(tenant)).settings_version == version


async def test_a_bulk_write_decided_before_a_hold_reached_this_process_is_held(
    client, as_auth, sc
):
    tenant = new_tenant_id()
    await _agent(sc, tenant, "low", 1, "f1")
    await _hold_behind_cache(sc, tenant, {"below_trust_by_fleet": {"f1": 2}})

    as_auth(tenant, agent_id="low")
    resp = await client.post(
        "/api/v1/memories/bulk",
        json={
            "tenant_id": tenant,
            "agent_id": "low",
            "fleet_id": "f1",
            "items": [
                {"content": f"first late claim {uuid.uuid4().hex}"},
                {"content": f"second late claim {uuid.uuid4().hex}"},
            ],
        },
        headers={"X-Bulk-Attempt-Id": f"late-{uuid.uuid4().hex}"},
    )

    assert resp.status_code == 200, resp.text
    ids = [result["id"] for result in resp.json()["results"]]
    assert len(ids) == 2 and all(ids)
    for memory_id in ids:
        row = await _row(sc, tenant, memory_id)
        assert row["status"] == QUARANTINED_MEMORY_STATUS
        assert _hold(row) == {
            "reason": "below_trust",
            "trust_level": 1,
            "below_trust": 2,
        }


async def test_a_write_the_new_settings_still_let_through_is_written_live(
    client, as_auth, sc
):
    """Decided again, it goes live: the retry writes it, under the new version."""
    tenant = new_tenant_id()
    await _agent(sc, tenant, "mid", 2, "f1")
    await _hold_behind_cache(sc, tenant, {"below_trust": 2})

    row = await _row(sc, tenant, await _write(client, as_auth, tenant, "mid", "f1"))

    assert row["status"] == "active"
    assert _hold(row) is None


# ── Deciding again, by path ──


async def test_a_refused_auto_chunk_parent_is_decided_again_with_its_chunks():
    """The parent's insert is refused once; held now, its chunks are held with it."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock, patch

    from core_api.clients.storage_client import StorageSettingsChangedError
    from core_api.schemas import MemoryCreate
    from core_api.services import memory_service

    hold = {"reason": "below_trust", "trust_level": 1, "below_trust": 2}
    parent_row = {
        "id": str(uuid.uuid4()),
        "tenant_id": "t",
        "fleet_id": "f1",
        "agent_id": "low",
        "memory_type": "fact",
        "title": "t",
        "content": "body",
        "weight": 0.5,
        "status": QUARANTINED_MEMORY_STATUS,
        "visibility": "scope_team",
        "recall_count": 0,
        "created_at": datetime(2026, 10, 7, tzinfo=UTC),
        "metadata_": {},
        "embedding": None,
        "deleted_at": None,
    }
    sent: list[dict] = []

    async def _create(payload):
        # A copy: the payload is changed in place before the retry.
        sent.append(copy.deepcopy(payload))
        if len(sent) == 1:
            raise StorageSettingsChangedError("the organization settings changed")
        return parent_row

    sc = AsyncMock(name="storage_client")
    sc.create_memory = AsyncMock(side_effect=_create)
    sc.create_memories = AsyncMock(return_value=[])
    sc.bulk_find_by_content_hashes = AsyncMock(return_value={})
    data = MemoryCreate(
        tenant_id="t", fleet_id="f1", agent_id="low", content="a long body " * 200
    )
    ctx = SimpleNamespace(
        data={
            "input": data,
            "memory_fields": {
                "memory_type": "fact",
                "title": "t",
                "weight": 0.5,
                "status": "active",
                "metadata": {},
            },
            "enrichment": None,
            "embedding": [0.0],
            "t0": 0.0,
        },
        tenant_config=ResolvedConfig(
            {"entity_extraction": {"enabled": False}}, settings_version="v1"
        ),
    )
    fresh = ResolvedConfig(
        {"entity_extraction": {"enabled": False}, "quarantine": {"below_trust": 2}},
        settings_version="v2",
    )

    async def _chunks(_content, _x, _cfg):
        return [{"content": c, "suggested_type": "fact"} for c in ("one", "two")]

    async def _embeddings(texts, _cfg, background=False):
        return [[0.0] for _ in texts]

    with (
        patch.object(memory_service, "get_storage_client", lambda: sc),
        patch.object(memory_service, "track_task", MagicMock()),
        patch.object(memory_service, "get_embeddings_batch", new=_embeddings),
        patch("core_api.services.ingest_service._chunk_content", new=_chunks),
        patch(
            "core_api.pipeline.steps.write.governance_decision.emit_governance_audit",
            new=AsyncMock(),
        ),
        patch(
            "core_api.services.organization_settings.reload_config",
            new=AsyncMock(return_value=fresh),
        ),
        patch(
            "core_api.pipeline.steps.write.hold_low_trust_write.hold_for",
            new=AsyncMock(return_value=hold),
        ),
    ):
        await memory_service._handle_auto_chunk_from_ctx(data, ctx)

    first, second = sent
    assert (first["status"], first[SETTINGS_VERSION_KEY]) == ("active", "v1")
    assert second["status"] == QUARANTINED_MEMORY_STATUS
    assert second["metadata_"]["_system"]["hold"] == hold
    assert SETTINGS_VERSION_KEY not in second
    children = sc.create_memories.await_args.args[0]
    assert [child["status"] for child in children] == [QUARANTINED_MEMORY_STATUS] * 2
    assert [child["metadata_"]["_system"]["hold"] for child in children] == [hold] * 2


# ── The claim and the retry ──


def _live(version: str | None = "v1") -> tuple[dict, ResolvedConfig]:
    return {"metadata_": {}}, ResolvedConfig({}, settings_version=version)


async def test_a_live_write_claims_the_settings_it_was_decided_under():
    payload, config = _live()

    write_hold.claim_settings(payload, config, is_inferred=False)

    assert payload[SETTINGS_VERSION_KEY] == "v1"


async def test_a_tenant_with_no_settings_claims_the_empty_version():
    payload, config = _live(NO_SETTINGS)

    write_hold.claim_settings(payload, config, is_inferred=False)

    assert payload[SETTINGS_VERSION_KEY] == NO_SETTINGS


@pytest.mark.parametrize(
    ("metadata", "is_inferred", "version"),
    [
        ({"_system": {"hold": {"reason": "below_trust"}}}, False, "v1"),  # held
        ({}, True, "v1"),  # the platform's own
        ({}, False, None),  # settings from a storage that has no versions
    ],
)
async def test_a_write_that_was_not_decided_live_claims_nothing(
    metadata, is_inferred, version
):
    payload = {"metadata_": metadata, SETTINGS_VERSION_KEY: "stale"}

    write_hold.claim_settings(
        payload, ResolvedConfig({}, settings_version=version), is_inferred=is_inferred
    )

    assert SETTINGS_VERSION_KEY not in payload


async def test_a_refused_insert_is_decided_again_under_fresh_settings_and_retried(
    monkeypatch,
):
    from core_api.clients.storage_client import StorageSettingsChangedError

    fresh = ResolvedConfig({}, settings_version="v2")
    reload = AsyncMock(return_value=fresh)
    monkeypatch.setattr("core_api.services.organization_settings.reload_config", reload)
    insert = AsyncMock(side_effect=[StorageSettingsChangedError("changed"), "row"])
    decide_again = AsyncMock()

    assert await write_hold.insert_deciding_again("t", insert, decide_again) == "row"

    reload.assert_awaited_once_with("t")
    decide_again.assert_awaited_once_with(fresh)
    assert insert.await_count == 2


async def test_an_insert_that_is_not_refused_reloads_nothing(monkeypatch):
    reload = AsyncMock()
    monkeypatch.setattr("core_api.services.organization_settings.reload_config", reload)
    decide_again = AsyncMock()

    assert (
        await write_hold.insert_deciding_again(
            "t", AsyncMock(return_value="row"), decide_again
        )
        == "row"
    )

    reload.assert_not_awaited()
    decide_again.assert_not_awaited()


async def test_settings_that_change_again_mid_retry_ask_the_caller_to_retry(
    monkeypatch,
):
    from fastapi import HTTPException

    from core_api.clients.storage_client import StorageSettingsChangedError

    monkeypatch.setattr(
        "core_api.services.organization_settings.reload_config",
        AsyncMock(return_value=ResolvedConfig({}, settings_version="v2")),
    )
    insert = AsyncMock(side_effect=StorageSettingsChangedError("changed"))

    with pytest.raises(HTTPException) as caught:
        await write_hold.insert_deciding_again("t", insert, AsyncMock())

    assert caught.value.status_code == 503
    assert insert.await_count == 2


@pytest.mark.parametrize("method", ["create_memory", "create_memories"])
async def test_the_client_tells_a_settings_change_from_a_duplicate(method):
    """Both are a 409 from storage; only one is the caller's to resolve."""
    from unittest.mock import patch

    import httpx

    from core_api.clients.storage_client import (
        CoreStorageClient,
        DuplicateMemoryError,
        StorageSettingsChangedError,
    )

    def _refusal(body: dict) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "http://storage/memories")
        response = httpx.Response(status_code=409, json=body, request=request)
        return httpx.HTTPStatusError("upstream", request=request, response=response)

    payload = {"agent_id": "a", "tenant_id": "t"}
    arg = payload if method == "create_memory" else [payload]
    changed = _refusal(
        {"detail": {"error": SETTINGS_CHANGED, "message": "decide it again"}}
    )
    duplicate = _refusal({"detail": "Duplicate memory exists: x"})

    with patch.object(CoreStorageClient, "_post", new=AsyncMock(side_effect=changed)):
        with pytest.raises(StorageSettingsChangedError, match="decide it again"):
            await getattr(CoreStorageClient(), method)(arg)
    with patch.object(CoreStorageClient, "_post", new=AsyncMock(side_effect=duplicate)):
        with pytest.raises(DuplicateMemoryError):
            await getattr(CoreStorageClient(), method)(arg)
