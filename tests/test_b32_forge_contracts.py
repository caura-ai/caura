"""B32: Forge writes, wires and configures what it says it does.

L-24  Forge checked a slug's status, then wrote the candidate over whatever was
      stored by then: an operator's quarantine in between was reverted to
      ``candidate``, which the same tick could promote. The write now lands only
      over no document or a ``candidate``, and a slug claimed in between is
      skipped like one the check caught. The storage half is
      ``core-storage-api/tests/test_b32_document_status_precondition.py``; the
      inbox half is in ``test_skills_inbox_routes.py``.
L-60  ``scripts/forge_dry_run.py`` writes, but checked status on the replica (the
      L-33 bug the cron had fixed) and wrote without a precondition. It now
      wires the cron's checker and writer.
L-210 Forge read ``cluster_entity_jaccard_threshold`` and
      ``memory_excerpt_char_cap`` from settings ``PUT /settings`` refuses, so
      the reads were dead. Dropped; Forge keeps its defaults (Eldad,
      2026-10-11).
L-211 ``skills_factory.openclaw_bridge.enabled`` was accepted and read by
      nothing; retired as ``fail_on_critical`` was (M-114). The cron doc stops
      claiming ``cron_interval_hours`` is stamped on audit rows and inbox cards,
      and the key stays informational (Eldad, 2026-10-11).
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core_api.clients import storage_client
from core_api.services import organization_settings
from core_api.services.forge import cron_handler, forge_service
from core_api.services.forge.forge_service import ForgeConfig, run_forge_distill
from tests.conftest import get_test_auth
from tests.test_forge_distill import (
    _llm_returns_golden,
    _memory_fetcher_always,
    _passing_traces,
    _poison_never,
)

ROOT = Path(__file__).resolve().parents[1]


class _RecordingStorage:
    """The two storage calls Forge's checker and writer make."""

    def __init__(self, refuse_writes: bool = False):
        self.reads: list[bool] = []
        self.expected: list[list | None] = []
        self.refuse_writes = refuse_writes

    async def get_document(self, *, tenant_id, collection, doc_id, read=True):
        self.reads.append(read)
        return {"doc_id": doc_id, "data": {"status": "candidate"}}

    async def upsert_document(self, payload, *, expected_status=None):
        self.expected.append(None if expected_status is None else list(expected_status))
        if self.refuse_writes:
            raise storage_client.DocumentStatusChangedError(
                f"{payload['doc_id']} is 'quarantined'"
            )


@pytest.fixture
def storage(monkeypatch):
    fake = _RecordingStorage()
    monkeypatch.setattr(storage_client, "get_storage_client", lambda: fake)
    monkeypatch.setattr(cron_handler, "get_storage_client", lambda: fake)
    return fake


# -- L-24: Forge writes over a candidate or nothing ---------------------------


@pytest.mark.unit
async def test_l24_forge_writes_only_over_a_candidate_or_nothing(storage):
    write = cron_handler._make_candidate_writer()
    await write({"doc_id": "forge/x", "data": {"status": "candidate"}})
    assert storage.expected == [[None, "candidate"]]


@pytest.mark.unit
async def test_l24_a_slug_claimed_after_the_check_is_skipped(storage, monkeypatch):
    """The finding's race: the check read ``candidate``, an operator then
    quarantined the skill, and storage refuses the write. Forge skips the slug
    as it skips one the check caught, not as an I/O failure."""
    storage.refuse_writes = True

    async def traces(*_args, **_kwargs):
        return _passing_traces(4)

    async def still_a_candidate(*_):
        return "candidate"

    monkeypatch.setattr(forge_service, "build_session_traces", traces)
    result = await run_forge_distill(
        run_label="b32",
        tenant_id="t1",
        fleet_id=None,
        window_start=datetime(2026, 5, 1, tzinfo=UTC),
        window_end=datetime(2026, 5, 15, tzinfo=UTC),
        llm_fn=_llm_returns_golden,
        memory_fetcher=_memory_fetcher_always,
        poison_checker=_poison_never,
        candidate_writer=cron_handler._make_candidate_writer(),
        status_checker=still_a_candidate,
    )

    assert result.candidates_skipped_existing == 1
    assert result.candidates_skipped_io_error == 0
    assert result.candidates_skipped_internal_error == 0
    assert result.candidates_written == 0


# -- L-60: the dry run wires the cron's checker and writer --------------------


def _dry_run():
    spec = importlib.util.spec_from_file_location(
        "forge_dry_run_b32", ROOT / "scripts" / "forge_dry_run.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
async def test_l60_the_dry_run_checks_the_primary_and_writes_with_the_precondition(
    storage,
):
    dry_run = _dry_run()

    check = await dry_run._wire_status_checker()
    await check("t1", "skills", "forge/x")
    write = await dry_run._wire_candidate_writer()
    await write({"doc_id": "forge/x", "data": {"status": "candidate"}})

    assert storage.reads == [False]
    assert storage.expected == [[None, "candidate"]]


# -- L-210: Forge reads only settings a tenant can set ------------------------

_UNSETTABLE = {"cluster_entity_jaccard_threshold": 0.9, "memory_excerpt_char_cap": 50}


@pytest.mark.unit
def test_l210_put_settings_refuses_both_keys():
    """Why the reads were dead: no tenant could store either key."""
    for key, value in _UNSETTABLE.items():
        with pytest.raises(ValueError, match=key):
            organization_settings._check_keys(
                {"skills_factory": {"forge": {key: value}}},
                organization_settings.DEFAULT_SETTINGS,
            )


@pytest.mark.unit
async def test_l210_forge_keeps_its_defaults_for_keys_settings_refuse(monkeypatch):
    async def display(_org_id):
        return {"skills_factory": {"forge": dict(_UNSETTABLE)}}

    monkeypatch.setattr(cron_handler, "get_settings_for_display", display)
    cfg = await cron_handler._resolve_forge_config("t1")

    base = ForgeConfig()
    assert cfg.cluster_entity_jaccard_threshold == base.cluster_entity_jaccard_threshold
    assert cfg.memory_excerpt_char_cap == base.memory_excerpt_char_cap


# -- L-211: the bridge toggle is retired, the interval stays informational ----


def _new_tenant() -> tuple[str, dict]:
    return get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")


@pytest.mark.unit
def test_l211_the_bridge_toggle_is_gone():
    skills_factory = organization_settings.DEFAULT_SETTINGS["skills_factory"]
    assert "openclaw_bridge" not in skills_factory
    assert not [k for k in organization_settings._LEAF_TYPES if "openclaw_bridge" in k]


async def test_l211_a_write_to_the_bridge_toggle_is_refused(client):
    tenant_id, headers = _new_tenant()

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"skills_factory": {"openclaw_bridge": {"enabled": True}}},
        headers=headers,
    )

    assert resp.status_code == 422, resp.text
    assert "openclaw_bridge" in resp.text


async def test_l211_a_bridge_toggle_stored_before_is_not_shown(client):
    tenant_id, headers = _new_tenant()
    await storage_client.get_storage_client().update_org_settings(
        tenant_id, {"skills_factory": {"openclaw_bridge": {"enabled": True}}}
    )

    resp = await client.get(f"/api/v1/settings?tenant_id={tenant_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    assert "openclaw_bridge" not in resp.json()["skills_factory"]


async def test_l211_the_cron_interval_still_writes(client):
    """The control: the informational interval stays a setting."""
    tenant_id, headers = _new_tenant()

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"skills_factory": {"forge": {"cron_interval_hours": 12}}},
        headers=headers,
    )

    assert resp.status_code == 200, resp.text


@pytest.mark.unit
def test_l211_the_cron_doc_promises_no_stamp():
    """Nothing writes the interval to an audit row or an inbox card."""
    doc = (ROOT / "docs" / "operator-forge-cron.md").read_text()
    section = doc.split("## Operator dial: `forge.cron_interval_hours`", 1)[1]
    section = section.split("\n## ", 1)[0]
    assert "audit row" not in section
    assert "inbox-card" not in section
