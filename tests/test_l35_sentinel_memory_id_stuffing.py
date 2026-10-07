"""09/02 L-35 — Sentinel check #6 could never fire on a production doc.

The check read `evidence["memory_ids"]`, behind `isinstance(evidence, dict)`.

The only production writer is Forge, and its distill schema declares `evidence`
as a **string** — "a 2-3 sentence human-readable rationale" (`distill_prompt`).
So the isinstance guard returned immediately on every real doc and the check
never fired once.

Meanwhile the ids it was meant to bound live at `data["cites"]`
(`all_memory_ids` in `forge_service`) — top-level and unguarded. A runaway or
adversarial distillation could stuff hundreds there with no warning, which is
exactly what this check exists to surface.
"""

import pytest

from core_api.services.forge.sentinel_scan import (
    MAX_MEMORY_IDS_BEFORE_WARN,
    _scan_memory_id_stuffing,
)

pytestmark = pytest.mark.unit

_OVER = MAX_MEMORY_IDS_BEFORE_WARN + 5


def _ids(n: int, prefix: str = "m") -> list[str]:
    return [f"{prefix}-{i}" for i in range(n)]


def _codes(data: dict) -> list[str]:
    return [f.code for f in _scan_memory_id_stuffing(data)]


# ── the field production actually writes ─────────────────────────────────


def test_stuffing_in_cites_is_caught():
    """The defect. This is the field Forge writes, and it was unguarded."""
    assert "MEMORY_ID_STUFFING" in _codes({"cites": _ids(_OVER)})


def test_a_normal_cite_count_is_clean():
    assert _codes({"cites": _ids(MAX_MEMORY_IDS_BEFORE_WARN)}) == []


def test_the_production_evidence_shape_does_not_suppress_the_check():
    """Forge writes ``evidence`` as a STRING. The old signature took
    ``evidence`` and bailed on ``not isinstance(dict)``, so a string evidence —
    i.e. every real doc — skipped the check entirely, however many cites it
    carried."""
    data = {"cites": _ids(_OVER), "evidence": "A 2-3 sentence rationale."}
    assert "MEMORY_ID_STUFFING" in _codes(data)


# ── the original dict shape still works ──────────────────────────────────


def test_the_dict_evidence_shape_is_still_read():
    """The documents API accepts arbitrary ``data``, so an external writer may
    legitimately use the shape this check was first written against. Fixing
    the real path must not drop the old one."""
    data = {"evidence": {"memory_ids": _ids(_OVER)}}
    assert "MEMORY_ID_STUFFING" in _codes(data)


def test_ids_are_unioned_across_both_locations():
    """The cap describes what the RENDERER will show for the doc, not a
    per-field quota — so two under-cap fields that together exceed it must
    warn, and a duplicate id must not be counted twice."""
    half = MAX_MEMORY_IDS_BEFORE_WARN // 2 + 1
    data = {
        "cites": _ids(half, "a"),
        "evidence": {"memory_ids": _ids(half, "b")},
    }
    assert "MEMORY_ID_STUFFING" in _codes(data)


def test_duplicate_ids_across_locations_count_once():
    same = _ids(_OVER)
    assert "MEMORY_ID_STUFFING" in _codes(
        {"cites": same, "evidence": {"memory_ids": same}}
    )
    # …and the same ids under the cap stay clean rather than doubling.
    under = _ids(MAX_MEMORY_IDS_BEFORE_WARN)
    assert _codes({"cites": under, "evidence": {"memory_ids": under}}) == []


# ── shapes that must not raise ───────────────────────────────────────────


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"cites": None},
        {"cites": "not-a-list"},
        {"evidence": None},
        {"evidence": "string rationale"},
        {"evidence": {"memory_ids": "not-a-list"}},
        {"cites": [1, 2, 3]},  # non-string ids
    ],
)
def test_malformed_shapes_are_tolerated(data):
    """The scanner is called on caller-supplied ``data`` and must never be the
    thing that fails a write."""
    assert _codes(data) == []


def test_the_finding_points_at_the_real_field():
    """The locator lands on an inbox card; pointing at a path no production
    doc has would send an operator looking for a field that isn't there."""
    findings = list(_scan_memory_id_stuffing({"cites": _ids(_OVER)}))
    assert findings[0].locator == "data.cites"
    assert findings[0].severity == "warn"
