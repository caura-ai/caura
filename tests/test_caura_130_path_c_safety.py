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

from contextlib import ExitStack
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
    new_mem: dict,
    candidates: list[dict],
    links_by_mem: dict[str, list[dict]],
    entities: dict[str, dict] | None = None,
) -> AsyncMock:
    """Build a mock storage client for the forward Path C path.

    ``entities`` (optional) maps entity_id → explicit entity row, for
    tests that need the canonical_name decoupled from the entity_id
    (WT-3: same canonical name under DIFFERENT entity_ids, and vice
    versa). Ids absent from the map fall back to the legacy synthesis
    (canonical_name derived from the ``ent:<name>`` id).
    """
    sc = AsyncMock()

    async def get_memory(mid: str, tenant_id: str, **_kw):
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
        if entities is not None and eid in entities:
            return entities[eid]
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
    # real-world subjects → preflight must drop. WT-3 scoped the drop
    # to this same-NAME collision class, so the entity rows must carry
    # the SAME canonical_name under DIFFERENT ids (the legacy id-derived
    # synthesis would have given them different names too).
    links = {
        str(new_id): [{"entity_id": "ent:priya-A", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:priya-B", "role": "subject"}],
    }
    entities = {
        "ent:priya-A": {
            "id": "ent:priya-A",
            "canonical_name": "Priya",
            "entity_type": "person",
        },
        "ent:priya-B": {
            "id": "ent:priya-B",
            "canonical_name": "Priya",
            "entity_type": "person",
        },
    }
    sc = _sc_for_forward_path(new_mem, [cand], links, entities=entities)
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
    must NOT drop; the LLM judge runs.

    Updated for CAURA-131: when both sides have non-empty entity
    context, the entity-aware judge is invoked (not the base judge),
    because CAURA-131 lifted the context fetch to also feed the
    detection LLM call. We assert the entity-aware judge runs and
    the base judge does NOT (regression guard for the wiring)."""
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
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
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

    entity_aware_judge.assert_called_once()
    base_judge.assert_not_called()


@pytest.mark.asyncio
async def test_l34_preflight_skips_drop_check_when_both_subject_ids_nonnull():
    """When BOTH sides have non-NULL ``subject_entity_id``, the legacy
    A1 #17 gate already covered the canonical-subject mismatch case;
    the L3.4 preflight stage must NOT additionally drop these
    candidates.

    Updated for CAURA-131: the context fetch now runs for ALL
    surviving post-A1-#17 candidates (so the detection LLM call can
    use the entity-aware judge — that's the whole CAURA-131 fix).
    The cost-guard is now the ``_ENTITY_LINKS_PREFLIGHT_MAX_CANDIDATES``
    cap, not "skip the fetch when both ids non-NULL". This test
    therefore verifies the L3.4 PREFLIGHT LOGIC (the per-candidate
    drop check) skips these rows — not the fetch itself."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    # Same subject_entity_id on both sides — legacy A1 #17 passes them
    # through; L3.4 preflight per-row check should skip them too.
    same_sid = "sid-shared"
    new_mem = _make_new_memory(new_id, subject_entity_id=same_sid)
    cand = _make_candidate(cand_id, subject_entity_id=same_sid)
    # Provide non-empty entity context so the entity-aware judge has
    # something to ground on (CAURA-131 detection path).
    links = {
        str(new_id): [{"entity_id": "ent:shared", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:shared", "role": "subject"}],
    }
    sc = _sc_for_forward_path(new_mem, [cand], links)
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(False, 0.95))

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
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

    # Candidate not dropped → the entity-aware judge runs on it
    # (CAURA-131 path). The L3.4 preflight's per-row drop check
    # correctly early-continues for non-NULL-on-both-sides rows.
    entity_aware_judge.assert_called_once()
    base_judge.assert_not_called()


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
    # A61 — the fan-out is now ONE batched base call, not N per-candidate
    # calls. Cap exceeded → fetch skipped → all candidates base kind.
    judge_batch = AsyncMock(return_value=[{"contradicts": False} for _ in cands])

    with (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check_batch",
            judge_batch,
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
    # Fail-open: ONE batched base call still covers every candidate.
    judge_batch.assert_awaited_once()
    assert len(judge_batch.call_args.args[1]) == len(cands), (
        f"expected the batched base call to cover {len(cands)} candidates, "
        f"got {len(judge_batch.call_args.args[1])}"
    )


# ---------------------------------------------------------------------------
# WT-3 — the L3.4 drop is scoped to the same-NAME collision class;
# differing names + differing ids (a possible canonicalisation split of
# ONE subject) must fail open to the LLM judge.
# ---------------------------------------------------------------------------


def _wt3_patches(sc, base_judge, entity_aware_judge):
    """One combined context manager for the standard WT-3 patch set."""
    stack = ExitStack()
    for p in (
        patch(
            "core_api.services.contradiction_detector.get_storage_client",
            return_value=sc,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_contradiction_check",
            base_judge,
        ),
        patch(
            "core_api.services.contradiction_detector._llm_entity_aware_contradiction_check",
            entity_aware_judge,
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
        stack.enter_context(p)
    return stack


@pytest.mark.asyncio
async def test_wt3_preflight_fails_open_when_names_and_ids_both_differ():
    """WT-3 regression — entity canonicalisation split ONE real-world
    subject into two entity rows with DIFFERENT canonical names ("new
    analytics service" vs "analytics service"). The L3.4 preflight
    previously dropped on the id mismatch alone, silently eating the
    real contradiction (wet-test: candidates_initial=1 → n_conflicts=0).
    Differing names cannot be the priya name-collision class, so the
    candidate must SURVIVE the preflight and reach the judge."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    links = {
        str(new_id): [{"entity_id": "ent:svc-A", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:svc-B", "role": "subject"}],
    }
    entities = {
        "ent:svc-A": {
            "id": "ent:svc-A",
            "canonical_name": "new analytics service",
            "entity_type": "project",
        },
        "ent:svc-B": {
            "id": "ent:svc-B",
            "canonical_name": "analytics service",
            "entity_type": "project",
        },
    }
    sc = _sc_for_forward_path(new_mem, [cand], links, entities=entities)
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(False, 0.95))

    with _wt3_patches(sc, base_judge, entity_aware_judge):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Fail open: the candidate reaches the judge (entity-aware, since
    # both contexts are populated — CAURA-131 wiring).
    entity_aware_judge.assert_called_once()
    base_judge.assert_not_called()


@pytest.mark.asyncio
async def test_wt3_preflight_still_drops_same_name_collision_after_normalisation():
    """The priya guard — SAME canonical subject name (up to the WT-3
    normalisation: case + whitespace) under DIFFERENT entity_ids is the
    name-collision class the gate was built for and must STILL drop."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    links = {
        str(new_id): [{"entity_id": "ent:priya-1", "role": "subject"}],
        str(cand_id): [{"entity_id": "ent:priya-2", "role": "subject"}],
    }
    entities = {
        "ent:priya-1": {
            "id": "ent:priya-1",
            "canonical_name": "Priya  Sharma",
            "entity_type": "person",
        },
        "ent:priya-2": {
            "id": "ent:priya-2",
            "canonical_name": "priya sharma",
            "entity_type": "person",
        },
    }
    sc = _sc_for_forward_path(new_mem, [cand], links, entities=entities)
    base_judge = AsyncMock(return_value=(True, 0.95))
    entity_aware_judge = AsyncMock(return_value=(True, 0.95))

    with _wt3_patches(sc, base_judge, entity_aware_judge):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    entity_aware_judge.assert_not_called()
    base_judge.assert_not_called()
    assert sc.update_memory_status.call_args_list == [], (
        "same-canonical-name collision must still be dropped by the "
        "L3.4 preflight after the WT-3 fail-open change"
    )


@pytest.mark.asyncio
async def test_wt3_preflight_null_identity_side_still_fails_open():
    """A candidate whose entity_links resolve NO subject-role entity has
    no identity to compare — the preflight must keep failing open (the
    pre-WT-3 behaviour) and let the judge decide."""
    from core_api.services.contradiction_detector import (
        detect_contradictions_by_entities_async,
    )

    new_id, cand_id = uuid4(), uuid4()
    new_mem = _make_new_memory(new_id, subject_entity_id=None)
    cand = _make_candidate(cand_id, subject_entity_id=None)
    links = {
        str(new_id): [{"entity_id": "ent:svc-A", "role": "subject"}],
        # Object-role only — _extract_subject_canonical_identity → None.
        str(cand_id): [{"entity_id": "ent:svc-A", "role": "object"}],
    }
    sc = _sc_for_forward_path(new_mem, [cand], links)
    base_judge = AsyncMock(return_value=(False, 0.95))
    entity_aware_judge = AsyncMock(return_value=(False, 0.95))

    with _wt3_patches(sc, base_judge, entity_aware_judge):
        await detect_contradictions_by_entities_async(new_id, "t1", "f1")

    # Kept: judge runs (entity-aware — both contexts non-empty).
    entity_aware_judge.assert_called_once()
    base_judge.assert_not_called()
