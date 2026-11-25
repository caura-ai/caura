"""A tenant can record every conflict for review, whatever its deployment does.

``contradiction_write_conflict_record`` (``CONTRADICTION_WRITE_CONFLICT_RECORD``)
makes the detector write a ``memory_conflicts`` record for every confirmed
conflict, for a person to review. It is one switch for every tenant on the
deployment, so a deployment could record conflicts for every tenant or for
none, never for the few that review them.

``write.conflict_records_enabled`` sets it per tenant. True records every
confirmed conflict for that tenant on a deployment that records none; false
records only what is always recorded on a deployment that records all; unset
follows the deployment. What is always recorded is unchanged: a loser no edge
points at (M-34) and a dispute.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core_api.constants import VECTOR_DIM
from core_api.services.contradiction_detector import (
    _detect,
    detect_contradictions_by_entities_async,
)
from core_api.services.organization_settings import ResolvedConfig

SUBJ = "00000000-0000-0000-0000-0000000000aa"
_CD = "core_api.services.contradiction_detector"
_RECORD = "core_api.services.contradiction.resolver.record_detected_conflicts"
_DEPLOYMENT = "core_api.config.settings.contradiction_write_conflict_record"

# A confirmed contradiction in the raw shape ``_judge_contradiction`` parses.
_CONFLICT_RAW = {
    "subject_a": "X",
    "subject_b": "X",
    "same_subject": True,
    "non_conflict_reason": "none",
    "contradicts": True,
    "reason": "mutually exclusive states",
}

# path -> the kind its records carry. ``path_c_rdf`` is the deterministic pass
# Path C re-runs once extraction has filled the triple.
PATHS = {"rdf": "rdf", "semantic": "semantic", "path_c_rdf": "rdf", "path_c": "entity"}


def _cfg(value: bool | None = None) -> ResolvedConfig:
    if value is None:
        return ResolvedConfig({})
    return ResolvedConfig({"write": {"conflict_records_enabled": value}})


def _row(path: str, hour: int, value: str) -> dict:
    """A memory row. Only the RDF paths get a triple, so the others are not
    short-circuited by the deterministic pass."""
    row = {
        "id": str(uuid4()),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": f"X lives in {value}",
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": f"2026-05-24T{hour:02d}:00:00+00:00",
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
    }
    if path in ("rdf", "path_c_rdf"):
        row.update(subject_entity_id=SUBJ, predicate="lives_in", object_value=value)
    return row


async def _recorded(
    path: str, *, tenant: bool | None, deployment: bool
) -> tuple[list, str, str]:
    """Run one detection path where a new memory contradicts two older ones, and
    return ``(candidate id, kind)`` for every pair it recorded, with the ids of
    the loser the edge points at and of the one it doesn't."""
    new = _row(path, 12, "Haifa")
    linked, unlinked = _row(path, 8, "Tel Aviv"), _row(path, 9, "Eilat")
    found = [linked, unlinked]
    cfg = _cfg(tenant)

    sc = AsyncMock()
    sc.find_rdf_conflicts = AsyncMock(
        return_value=found if path in ("rdf", "path_c_rdf") else []
    )
    sc.find_similar_candidates = AsyncMock(
        return_value=found if path == "semantic" else []
    )
    sc.find_entity_overlap_candidates = AsyncMock(
        return_value=found if path == "path_c" else []
    )
    sc.get_entity_links_for_memories = AsyncMock(return_value={})
    rows = {r["id"]: r for r in (new, *found)}
    sc.get_memory = AsyncMock(side_effect=lambda mid, _tenant, **_kw: rows.get(mid))
    sc.batch_update_status = AsyncMock(
        return_value={"ok": True, "skipped": [], "edge_skipped": []}
    )
    rec = AsyncMock()
    with (
        patch(f"{_CD}.get_storage_client", return_value=sc),
        patch(
            f"{_CD}._llm_contradiction_check_batch",
            AsyncMock(side_effect=lambda _c, cands, _cfg: [_CONFLICT_RAW] * len(cands)),
        ),
        patch(f"{_CD}._llm_contradiction_check", AsyncMock(return_value=(True, 0.95))),
        patch(f"{_CD}._acquire_entity_lock", AsyncMock(return_value=True)),
        patch(
            "core_api.services.organization_settings.resolve_config",
            AsyncMock(return_value=cfg),
        ),
        patch(_RECORD, rec),
        patch(_DEPLOYMENT, deployment),
    ):
        if path in ("rdf", "semantic"):
            await _detect(new, [0.1] * VECTOR_DIM, cfg)
        else:
            await detect_contradictions_by_entities_async(new["id"], "t1", "f1")
    pairs = [
        (cand["id"], kind)
        for call in rec.await_args_list
        for cand, kind, _conf in call.args[1]
    ]
    return pairs, linked["id"], unlinked["id"]


# ── the settings contract ────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.parametrize("deployment", [False, True])
def test_a_tenant_that_never_set_it_follows_the_deployment(deployment):
    with patch(_DEPLOYMENT, deployment):
        assert _cfg().conflict_records_enabled is deployment
        assert (
            ResolvedConfig(
                {"write": {"conflict_records_enabled": None}}
            ).conflict_records_enabled
            is deployment
        )


@pytest.mark.unit
def test_a_tenant_can_record_on_a_deployment_that_records_nothing():
    with patch(_DEPLOYMENT, False):
        assert _cfg(True).conflict_records_enabled is True


@pytest.mark.unit
def test_a_tenant_can_stop_recording_on_a_deployment_that_records_everything():
    with patch(_DEPLOYMENT, True):
        assert _cfg(False).conflict_records_enabled is False


@pytest.mark.unit
def test_the_setting_survives_the_settings_write_validation():
    """A knob only on ``ResolvedConfig`` is unsettable: ``_check_keys`` validates
    writes against ``DEFAULT_SETTINGS``."""
    from core_api.services.organization_settings import (
        DEFAULT_SETTINGS,
        _check_keys,
        _validate_leaf_types,
    )

    payload = {"write": {"conflict_records_enabled": True}}
    _check_keys(payload, DEFAULT_SETTINGS)
    _validate_leaf_types(payload)

    # A string "false" is truthy: without the type check it would record every
    # conflict for a tenant that believed it had stopped.
    with pytest.raises(ValueError, match="conflict_records_enabled"):
        _validate_leaf_types({"write": {"conflict_records_enabled": "false"}})


async def test_the_setting_round_trips_through_a_real_settings_put(client):
    from core_api.services.organization_settings import resolve_config
    from tests.conftest import get_test_auth
    from tests.conftest import uid as _uid

    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")

    unset = await client.get(f"/api/v1/settings?tenant_id={tenant_id}", headers=headers)
    assert unset.status_code == 200, unset.text
    assert unset.json()["write"]["conflict_records_enabled"] is None

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"write": {"conflict_records_enabled": True}},
        headers=headers,
    )
    assert resp.status_code == 200, (
        f"PUT failed, so the setting is unsettable: {resp.text}"
    )

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["write"]["conflict_records_enabled"] is True

    with patch(_DEPLOYMENT, False):
        assert (await resolve_config(tenant_id)).conflict_records_enabled is True


# ── every detection path ─────────────────────────────────────────────────


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_a_tenant_that_records_gets_every_conflict_on_a_deployment_that_records_none(
    path,
):
    pairs, linked, unlinked = await _recorded(path, tenant=True, deployment=False)

    assert pairs == [(linked, PATHS[path]), (unlinked, PATHS[path])]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_a_tenant_that_stopped_gets_only_the_unlinked_loser_on_a_deployment_that_records(
    path,
):
    pairs, _linked, unlinked = await _recorded(path, tenant=False, deployment=True)

    assert pairs == [(unlinked, PATHS[path])]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("deployment", [False, True])
async def test_a_tenant_that_never_set_it_records_as_the_deployment_does(
    path, deployment
):
    pairs, linked, unlinked = await _recorded(path, tenant=None, deployment=deployment)

    every = [(linked, PATHS[path]), (unlinked, PATHS[path])]
    assert pairs == (every if deployment else [(unlinked, PATHS[path])])
