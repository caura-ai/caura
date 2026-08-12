"""ConflictResolver.record_conflict tests (A55) — classify+resolve+persist."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core_api.services.contradiction import resolver as rv

pytestmark = pytest.mark.unit

SUBJ = "00000000-0000-0000-0000-0000000000aa"


def _mem(mid, **over) -> dict:
    base = {
        "id": mid,
        "content": "text",
        "subject_entity_id": None,
        "predicate": None,
        "object_value": None,
        "is_inferred": False,
    }
    base.update(over)
    return base


def _mock_storage():
    sc = MagicMock()
    sc.record_memory_conflict = AsyncMock(side_effect=lambda p: {**p, "id": "rec-1"})
    return sc


@pytest.mark.asyncio
async def test_rdf_pair_records_supersede():
    """RDF exact-value pair -> relationship=exact_value, diagnosis=temporal_change,
    action=supersede (matching today's RDF effect). No LLM."""
    new = _mem(
        "m-new", subject_entity_id=SUBJ, predicate="lives_in", object_value="Haifa"
    )
    old = _mem(
        "m-old", subject_entity_id=SUBJ, predicate="lives_in", object_value="Tel Aviv"
    )
    sc = _mock_storage()
    with patch.object(rv, "get_storage_client", return_value=sc):
        rec = await rv.record_conflict(new, old, tenant_id="t1", fleet_id="f1")

    sc.record_memory_conflict.assert_awaited_once()
    payload = sc.record_memory_conflict.await_args.args[0]
    assert payload["relationship"] == "exact_value"
    assert payload["diagnosis"] == "temporal_change"
    assert payload["action"] == "supersede"
    assert payload["evidence_strength"] == "explicit"
    assert payload["new_memory_id"] == "m-new" and payload["old_memory_id"] == "m-old"
    assert payload["created_by"] == "contradiction-engine"
    assert rec["id"] == "rec-1"


@pytest.mark.asyncio
async def test_llm_pair_records_classification():
    new, old = _mem("a"), _mem("b")

    async def fake_classify(nm, cand, *, tenant_config=None):
        from core_api.services.contradiction.diagnosis import ClassifyResult

        return ClassifyResult("negation", 0.8, "correction", 0.8)

    sc = _mock_storage()
    with (
        patch.object(rv, "classify", new=fake_classify),
        patch.object(rv, "get_storage_client", return_value=sc),
    ):
        await rv.record_conflict(new, old, tenant_id="t1", fleet_id=None)

    payload = sc.record_memory_conflict.await_args.args[0]
    assert payload["relationship"] == "negation"
    assert payload["diagnosis"] == "correction"
    assert (
        payload["action"] == "replace"
    )  # correction -> replace (high conf, not inferred)


@pytest.mark.asyncio
async def test_inferred_memory_downgrades_action_via_invariant():
    """If either memory is inferred, a destructive action is downgraded to
    mark_disputed (invariant: inference must not overturn an explicit fact)."""
    new = _mem("a", is_inferred=True)
    old = _mem("b")

    async def fake_classify(nm, cand, *, tenant_config=None):
        from core_api.services.contradiction.diagnosis import ClassifyResult

        return ClassifyResult("exact_value", 0.9, "correction", 0.9)

    sc = _mock_storage()
    with (
        patch.object(rv, "classify", new=fake_classify),
        patch.object(rv, "get_storage_client", return_value=sc),
    ):
        await rv.record_conflict(new, old, tenant_id="t1", fleet_id="f1")

    payload = sc.record_memory_conflict.await_args.args[0]
    assert payload["action"] == "mark_disputed"
    assert payload["evidence_strength"] == "entailed"  # inferred evidence


@pytest.mark.asyncio
async def test_storage_failure_is_swallowed():
    new, old = _mem("a"), _mem("b")
    sc = MagicMock()
    sc.record_memory_conflict = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(rv, "get_storage_client", return_value=sc):
        rec = await rv.record_conflict(new, old, tenant_id="t1", fleet_id="f1")
    assert rec is None  # best-effort; never raises
