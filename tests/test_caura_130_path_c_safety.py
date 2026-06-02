"""CAURA-130 — Direct unit tests for the Path C safety controls:

  * ``_extract_subject_canonical_identity`` — pure helper, no I/O.
  * Forward-path entity-links preflight in
    ``detect_contradictions_by_entities_async`` (L3.4): when the
    legacy ``subject_entity_id`` gate falls through (NULL on at
    least one side), the entity-links subject identity gate fires
    and drops candidates whose canonical subjects are distinct
    entity rows (the original ``priya``-collision case from the
    inline TODO).
  * ``ResolvedConfig.retraction_enabled`` resolver (L3.8): JSONB
    absence → True; explicit False → False; explicit True → True.

The kill-switch behaviour in ``_attempt_path_c_retraction`` itself
is locked in by tests in ``tests/test_a4_13_path_c_retraction.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from tests._contradiction_batch_compat import install_batch_status_replay_shim

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _extract_subject_canonical_identity — pure helper
# ---------------------------------------------------------------------------


def test_subject_identity_extracts_first_subject_role():
    from core_api.services.contradiction_detector import (
        _extract_subject_canonical_identity,
    )

    out = _extract_subject_canonical_identity(
        [
            {
                "name": "Project Helios",
                "entity_type": "project",
                "role": "subject",
                "entity_id": "ent-1",
            },
            {
                "name": "2027-05-01",
                "entity_type": "date",
                "role": "object",
                "entity_id": "ent-2",
            },
        ]
    )
    assert out == ("Project Helios", "project", "ent-1")


def test_subject_identity_empty_list_returns_none():
    from core_api.services.contradiction_detector import (
        _extract_subject_canonical_identity,
    )

    assert _extract_subject_canonical_identity([]) is None


def test_subject_identity_no_subject_role_returns_none():
    """Only ``object``-role entities → no subject identity to key on."""
    from core_api.services.contradiction_detector import (
        _extract_subject_canonical_identity,
    )

    out = _extract_subject_canonical_identity(
        [
            {
                "name": "2027-05-01",
                "entity_type": "date",
                "role": "object",
                "entity_id": "ent-1",
            },
        ]
    )
    assert out is None


def test_subject_identity_falls_back_to_name_when_no_canonical_name():
    """Raw entity rows (rather than the normalised shape from
    ``_fetch_entity_context``) may carry ``name`` directly. The helper
    must still extract a subject."""
    from core_api.services.contradiction_detector import (
        _extract_subject_canonical_identity,
    )

    out = _extract_subject_canonical_identity(
        [
            {
                "name": "Priya Patel",
                "entity_type": "person",
                "role": "subject",
                "entity_id": "ent-priya-1",
            }
        ]
    )
    assert out == ("Priya Patel", "person", "ent-priya-1")


def test_subject_identity_skips_subject_with_no_entity_id():
    """A subject-role entry with no ``entity_id`` carries no identity
    to compare against; skip it (and continue searching for another
    subject)."""
    from core_api.services.contradiction_detector import (
        _extract_subject_canonical_identity,
    )

    out = _extract_subject_canonical_identity(
        [
            {"name": "phantom", "entity_type": "person", "role": "subject"},
            {
                "name": "Real Subject",
                "entity_type": "person",
                "role": "subject",
                "entity_id": "ent-real",
            },
        ]
    )
    assert out == ("Real Subject", "person", "ent-real")


def test_subject_identity_role_match_is_case_insensitive():
    """Some storage paths may produce ``Subject`` rather than
    ``subject``. Be lenient on case for the role match."""
    from core_api.services.contradiction_detector import (
        _extract_subject_canonical_identity,
    )

    out = _extract_subject_canonical_identity(
        [
            {
                "name": "X",
                "entity_type": "project",
                "role": "Subject",
                "entity_id": "ent-x",
            }
        ]
    )
    assert out is not None
    assert out[2] == "ent-x"


# ---------------------------------------------------------------------------
# ResolvedConfig.retraction_enabled — JSONB resolver
# ---------------------------------------------------------------------------


def test_resolved_config_retraction_enabled_defaults_true_on_empty_settings():
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig({})
    assert cfg.retraction_enabled is True


def test_resolved_config_retraction_enabled_defaults_true_when_unset_in_write_block():
    """The JSONB key may be present at the ``write`` block level but
    explicitly ``None`` — that still means "use the global default"
    which is True."""
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig({"write": {"retraction_enabled": None}})
    assert cfg.retraction_enabled is True


def test_resolved_config_retraction_enabled_explicit_false_disables():
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig({"write": {"retraction_enabled": False}})
    assert cfg.retraction_enabled is False


def test_resolved_config_retraction_enabled_explicit_true_enables():
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig({"write": {"retraction_enabled": True}})
    assert cfg.retraction_enabled is True


# ---------------------------------------------------------------------------
# Forward-path L3.4 entity-links preflight
# ---------------------------------------------------------------------------


def _make_candidate(
    mid, *, subject_entity_id=None, content: str = "candidate content"
) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": content,
        "subject_entity_id": subject_entity_id,
        "visibility": "scope_team",
        "deleted_at": None,
        "created_at": "2026-05-24T10:00:00+00:00",
    }


def _make_new_memory(mid, *, subject_entity_id=None) -> dict:
    return {
        "id": str(mid),
        "tenant_id": "t1",
        "fleet_id": "f1",
        "content": "new memory content",
        "subject_entity_id": subject_entity_id,
        "visibility": "scope_team",
        "supersedes_id": None,
        "deleted_at": None,
        "created_at": "2026-05-24T11:00:00+00:00",
    }


def _sc_for_forward_path(
    new_mem: dict, candidates: list[dict], links_by_mem: dict[str, list[dict]]
) -> AsyncMock:
    """Build a mock storage client for the forward Path C path."""
    sc = AsyncMock()

    async def get_memory(mid: str):
        if mid == new_mem["id"]:
            return new_mem
        for c in candidates:
            if c["id"] == mid:
                return c
        return None

    sc.get_memory = AsyncMock(side_effect=get_memory)
    sc.find_entity_overlap_candidates = AsyncMock(return_value=candidates)
    sc.update_memory_status = AsyncMock()
    sc.get_entity_links_for_memories = AsyncMock(return_value=links_by_mem)

    async def get_entity(eid: str):
        # Synthesize a minimal entity row keyed by id; tests pass the
        # canonical_name they want here via the links_by_mem structure
        # (we encode it in the link by using entity_id == "ent:<name>").
        return {
            "id": eid,
            "canonical_name": eid.split(":", 1)[-1] if ":" in eid else eid,
            "entity_type": "person" if "priya" in eid else "project",
        }

    sc.get_entity = AsyncMock(side_effect=get_entity)
    install_batch_status_replay_shim(sc)
    return sc


@pytest.mark.asyncio
async def test_forward_preflight_drops_collision_when_subject_entity_id_null():
    """The legacy A1 #17 gate can't decide cases where ``subject_entity_id``
    is NULL on at least one side. CAURA-130 L3.4 — resolve the canonical
    subject via entity_links; drop the candidate when subjects are
    distinct entity rows even though canonical names match."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    # Same canonical name ("Priya"), different entity_ids → distinct
    # real-world subjects → preflight must drop.
    links = {
        str(new_id): [{"entity_id": "ent:priya-A", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:priya-B", "role": "subject"}],
    }
    sc = _sc_for_forward_path(new_mem, [cand], links)
    judge = AsyncMock(return_value=(True, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    judge.assert_not_called()
    assert sc.update_memory_status.call_args_list == [], (
        "candidate with distinct entity_id under same canonical name "
        "must be dropped by the L3.4 preflight"
    )


@pytest.mark.asyncio
async def test_forward_preflight_keeps_candidate_when_subjects_truly_match():
    """Same canonical name AND same entity_id → same subject → preflight
    must NOT drop; let the LLM judge handle it."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    links = {
        str(new_id): [{"entity_id": "ent:project-helios", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:project-helios", "role": "subject"}],
    }
    sc = _sc_for_forward_path(new_mem, [cand], links)
    judge = AsyncMock(return_value=(False, 0.95))  # judge says not a contradiction

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    judge.assert_called_once()


@pytest.mark.asyncio
async def test_forward_preflight_skipped_when_both_subject_ids_nonnull():
    """Cost guard — when BOTH sides have non-NULL ``subject_entity_id``,
    the legacy A1 #17 gate already covered it; the L3.4 entity-links
    stage must NOT issue a fetch (saves the round-trips)."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    # Same subject_entity_id on both sides — legacy gate passes them
    # both through, and L3.4 stage must skip the fetch.
    same_sid = "sid-shared"
    new_mem = _make_new_memory(new_id, subject_entity_id=same_sid)
    cand = _make_candidate(cand_id, subject_entity_id=same_sid)
    sc = _sc_for_forward_path(new_mem, [cand], {})
    judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # The fetch path must not run when both sides have non-NULL ids.
    sc.get_entity_links_for_memories.assert_not_called()


@pytest.mark.asyncio
async def test_forward_preflight_fails_open_on_storage_error():
    """Storage failure during the entity-links fetch must NOT drop
    candidates — fail open and let the LLM judge decide. Conservative
    against losing real contradictions on a transient hiccup."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    sc = _sc_for_forward_path(new_mem, [cand], {})
    sc.get_entity_links_for_memories = AsyncMock(
        side_effect=RuntimeError("storage down")
    )
    judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Storage failure must not prevent the judge from running.
    judge.assert_called_once()


@pytest.mark.asyncio
async def test_forward_preflight_caps_fallthrough_set_at_max():
    """CAURA-130 (L3.4) — when the fall-through set exceeds the
    ``_ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES`` cap, the L3.4 stage
    must skip the fan-out fetch entirely and fail-open (let the LLM
    judge decide). Bounds the storage round-trip blast radius for
    popular entities with high candidate-count."""
    from core_api.services.contradiction_detector import (
        _ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES,
        detect_contradictions_by_entities_async,
    )

    new_id = uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    # Build cap + 5 candidates, all with NULL subject_entity_id so
    # they all fall through.
    cand_ids = [uuid4() for _ in range(_ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES + 5)]
    cands = [_make_candidate(cid, subject_entity_id=None) for cid in cand_ids]
    sc = _sc_for_forward_path(new_mem, cands, {})
    judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            judge,
        ),
        patch(
            "core_api.services.contradiction_detector.resolve_config",
            new_callable=AsyncMock,
            return_value=None,
            create=True,
        ),
        patch(
            "core_api.services.contradiction_detector._acquire_path_c_lock",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # The fan-out fetch must NOT run when the set exceeds the cap.
    sc.get_entity_links_for_memories.assert_not_called()
    # Fail-open: judge still runs on each candidate.
    assert judge.call_count == len(cands), (
        f"expected {len(cands)} judge calls (fail-open), got {judge.call_count}"
    )
