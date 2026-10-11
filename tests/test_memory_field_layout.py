"""Unit tests for the memory field layout (``core_api.services.memory_field``).

Pure numpy: determinism, bounds, tiny inputs, rows without an embedding,
cluster labels and the response shape the frontend is built against.
"""

from __future__ import annotations

import numpy as np
import pytest

from core_api.services.memory_field import (
    FieldLayout,
    _FieldCache,
    build_field,
    choose_k,
    compute_layout,
    label_clusters,
)

pytestmark = pytest.mark.unit

DIM = 64


def _blobs(
    n: int, centres: int = 4, seed: int = 0
) -> tuple[list[str], np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    c = rng.normal(size=(centres, DIM)) * 3
    lab = rng.integers(0, centres, n)
    vecs = (c[lab] + rng.normal(size=(n, DIM)) * 0.3).astype(np.float16)
    return [f"m{i:05d}" for i in range(n)], vecs, lab


def _in_bounds(layout: FieldLayout) -> bool:
    return all(0.0 <= v <= 1.0 for v in layout.x + layout.y)


def test_deterministic_for_same_inputs():
    ids, vecs, _ = _blobs(300)
    has = np.ones(len(ids), dtype=bool)
    a = compute_layout(ids, vecs, has)
    b = compute_layout(ids, vecs.copy(), has.copy())
    assert a.x == b.x and a.y == b.y and a.cluster == b.cluster
    assert a.anchors == b.anchors


def test_bounds_padding_and_cluster_count():
    ids, vecs, _ = _blobs(500)
    layout = compute_layout(ids, vecs, np.ones(len(ids), dtype=bool))
    assert _in_bounds(layout)
    # ~6% padding: the extremes touch the padded frame on at least one axis.
    assert min(min(layout.x), min(layout.y)) == pytest.approx(0.06, abs=1e-6)
    assert max(max(layout.x), max(layout.y)) == pytest.approx(0.94, abs=1e-6)
    assert len(layout.sizes) == choose_k(500)
    assert sum(layout.sizes) == 500
    assert layout.sizes == sorted(layout.sizes, reverse=True)
    assert all(0 <= a[0] <= 1 and 0 <= a[1] <= 1 for a in layout.anchors)


def test_clusters_follow_the_embedding_structure():
    ids, vecs, lab = _blobs(400, centres=3)
    layout = compute_layout(ids, vecs, np.ones(len(ids), dtype=bool))
    # Every k-means cluster is pure w.r.t. the generating blob.
    for members in layout.members:
        assert len({int(lab[i]) for i in members}) == 1


@pytest.mark.parametrize("n, k", [(1, 1), (8, 2), (50, 5), (200, 10), (10_000, 24)])
def test_choose_k(n, k):
    assert choose_k(n) == k


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_tiny_inputs(n):
    ids, vecs, _ = _blobs(max(n, 1))
    ids, vecs = ids[:n], vecs[:n]
    layout = compute_layout(ids, vecs.reshape(n, DIM), np.ones(n, dtype=bool))
    assert len(layout.x) == n
    assert _in_bounds(layout)
    if n:
        assert set(layout.cluster) == {0}
        assert layout.sizes == [n]
        # Distinct positions for distinct points.
        assert (
            len(
                {
                    (round(x, 6), round(y, 6))
                    for x, y in zip(layout.x, layout.y, strict=True)
                }
            )
            == n
        )


@pytest.mark.parametrize("n", [4, 5, 7])
def test_small_inputs_cluster(n):
    ids, vecs, _ = _blobs(n)
    layout = compute_layout(ids, vecs, np.ones(n, dtype=bool))
    assert _in_bounds(layout)
    assert all(c >= 0 for c in layout.cluster)


def test_identical_vectors_do_not_crash():
    n = 30
    vecs = np.ones((n, DIM), dtype=np.float16)
    layout = compute_layout([f"m{i}" for i in range(n)], vecs, np.ones(n, dtype=bool))
    assert _in_bounds(layout)


def test_rows_without_embedding_are_unplaced_near_centre():
    ids, vecs, _ = _blobs(120)
    has = np.ones(len(ids), dtype=bool)
    has[::10] = False
    vecs[~has] = 0
    layout = compute_layout(ids, vecs, has)
    for i in np.flatnonzero(~has):
        assert layout.cluster[i] == -1
        assert abs(layout.x[i] - 0.5) <= 0.031 and abs(layout.y[i] - 0.5) <= 0.031
    assert all(layout.cluster[i] >= 0 for i in np.flatnonzero(has))
    # Jitter is per-id and stable.
    again = compute_layout(ids, vecs, has)
    assert [layout.x[i] for i in np.flatnonzero(~has)] == [
        again.x[i] for i in np.flatnonzero(~has)
    ]


def test_no_embeddings_at_all():
    ids = ["a", "b", "c"]
    layout = compute_layout(
        ids, np.zeros((3, 1), dtype=np.float16), np.zeros(3, dtype=bool)
    )
    assert layout.cluster == [-1, -1, -1]
    assert layout.sizes == []
    assert _in_bounds(layout)


def test_labels_prefer_distinctive_entities_with_fallback():
    rows = [
        {
            "entity_ids": ["acme", "billing"],
            "memory_type": "fact",
            "title": "Refund policy",
        },
        {
            "entity_ids": ["acme", "billing", "refunds"],
            "memory_type": "fact",
            "title": "Refund window",
        },
        {"entity_ids": ["acme", "refunds"], "memory_type": "fact", "title": "Refunds"},
        {
            "entity_ids": ["acme"],
            "memory_type": "decision",
            "title": "Deploy freeze Friday",
        },
        {
            "entity_ids": ["acme"],
            "memory_type": "decision",
            "title": "Deploy window moved",
        },
        {"entity_ids": [], "memory_type": "decision", "title": "Deploy rollback"},
    ]
    names = {"acme": "Acme Corp", "billing": "Billing", "refunds": "Refunds"}
    labels = label_clusters([[0, 1, 2], [3, 4, 5]], rows, names)
    # "acme" is everywhere, so the distinctive entities win.
    assert labels[0] in ("billing · refunds", "refunds · billing")
    # Cluster 2's only entity is the everywhere one; it still labels, but the
    # rarer-scored ordering never prefers it over a distinctive one.
    assert labels[1]
    assert " · " not in labels[1] or labels[1].count(" · ") == 1
    fallback = label_clusters([[3, 4, 5]], rows, {})
    assert fallback == ["decision · deploy"]


def _row(i: int, **kw) -> dict:
    base = {
        "id": f"00000000-0000-0000-0000-{i:012d}",
        "agent_id": "a1",
        "fleet_id": None,
        "memory_type": "fact",
        "weight": 0.5,
        "title": f"t{i}",
        "preview": "x" * 300,
        "content_chars": 300,
        "created_at": "2026-10-01T00:00:00+00:00",
        "recall_count": 0,
        "last_recalled_at": None,
        "status": "active",
        "visibility": "scope_team",
        "supersedes_id": None,
        "entity_ids": [],
    }
    base.update(kw)
    return base


def test_build_field_contract():
    _, vecs, _ = _blobs(40)
    rows = [_row(i) for i in range(40)]
    rows[0]["supersedes_id"] = rows[5]["id"]
    rows[5]["status"] = "superseded"
    rows[1]["entity_ids"] = ["e1"]
    has = np.ones(40, dtype=bool)
    has[3] = False
    out = build_field(
        rows,
        vecs,
        has,
        {
            "e1": {"id": "e1", "label": "Billing", "type": "topic"},
            "e2": {"id": "e2", "label": "x", "type": None},
        },
        total=55,
        signature="40:39:2026",
    )
    assert out["kind"] == "pca2"
    assert len(out["model_version"]) == 12
    assert out["total"] == 55
    assert {"id", "label", "x", "y", "size"} <= set(out["clusters"][0])
    assert sum(c["size"] for c in out["clusters"]) == 39
    pts = {p["id"]: p for p in out["points"]}
    assert len(pts) == 40
    p0 = out["points"][0]
    assert set(p0) == {
        "id", "x", "y", "cluster", "agent_id", "fleet_id", "memory_type", "weight", "title",
        "preview", "content_chars", "created_at", "recall_count", "last_recalled_at", "status",
        "visibility", "supersedes_id", "superseded_by", "entity_ids",
    }  # fmt: skip
    assert len(p0["preview"]) == 240
    assert p0["content_chars"] == 300
    assert pts[rows[5]["id"]]["superseded_by"] == rows[0]["id"]
    assert pts[rows[3]["id"]]["cluster"] == -1
    # Only entities linked from returned points.
    assert out["entities"] == [{"id": "e1", "label": "Billing", "type": "topic"}]
    # Same inputs, same version.
    again = build_field(rows, vecs, has, {}, total=55, signature="40:39:2026")
    assert again["model_version"] == out["model_version"]


def test_cache_ttl_and_lru():
    cache = _FieldCache(max_entries=2, ttl=60)
    cache.put(("a",), {"v": 1})
    cache.put(("b",), {"v": 2})
    assert cache.get(("a",)) == {"v": 1}
    cache.put(("c",), {"v": 3})  # evicts least-recently-used "b"
    assert cache.get(("b",)) is None
    assert cache.get(("a",)) == {"v": 1}
    expired = _FieldCache(ttl=-1)
    expired.put(("x",), {})
    assert expired.get(("x",)) is None
