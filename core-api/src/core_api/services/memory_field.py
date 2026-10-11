"""The memory field: a 2D map layout of a tenant's memories from their embeddings.

Serves ``GET /api/v1/memories/field``. The layout is a two-level linear
projection that needs only numpy (no umap / sklearn / numba):

1. PCA of all embeddings to at most 50 dimensions.
2. k-means on the PCA coordinates, k chosen by size
   (``clamp(round(sqrt(n / 2)), 2, 24)``), fixed seed.
3. Cluster centroids placed in 2D by a PCA of the centroids, then pushed apart
   by a few repulsion passes so clusters do not overlap.
4. Members placed inside their cluster by that cluster's own local 2D PCA,
   scaled to a radius proportional to ``sqrt(member count)``.
5. Everything normalised into ``[0, 1]`` with ~6% padding.

Deterministic for the same inputs. Rows without an embedding yet still appear,
with ``cluster = -1``, near the centre with a small jitter derived from their id.

The layout is computed on request and cached in-process (see
``_FieldCache``). Persisting the fitted model (mean, axes, centroids, local
axes) so the write path can place new memories inline, and so a refit is a
background job rather than a request, is the planned next step; no table is
added here.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from core_api.clients.storage_client import get_storage_client
from core_api.services.kmeans import numpy_kmeans

FIELD_KIND = "pca2"
FIELD_DEFAULT_LIMIT = 20000
FIELD_MAX_LIMIT = 50000
FIELD_PAGE_SIZE = 2000
FIELD_CACHE_TTL_SECONDS = 600.0
FIELD_CACHE_MAX_ENTRIES = 32
FIELD_PREVIEW_CHARS = 240

_PCA_DIMS = 50
_MIN_CLUSTERS = 2
_MAX_CLUSTERS = 24
_PADDING = 0.06
_REPULSION_ITERS = 60
_UNPLACED_JITTER = 0.03


# ---------------------------------------------------------------------------
# Layout (pure, synchronous)
# ---------------------------------------------------------------------------


@dataclass
class FieldLayout:
    """Normalised positions for every input row plus per-cluster anchors.

    ``cluster[i]`` is -1 for a row with no embedding. ``anchors`` and
    ``sizes`` are indexed by cluster id (0..k-1, largest cluster first).
    """

    x: list[float]
    y: list[float]
    cluster: list[int]
    anchors: list[tuple[float, float]]
    sizes: list[int]
    members: list[list[int]]


def choose_k(n: int) -> int:
    """Cluster count for ``n`` embedded points."""
    k = round(math.sqrt(n / 2))
    return min(n, max(_MIN_CLUSTERS, min(_MAX_CLUSTERS, k)))


def _fix_signs(axes: np.ndarray) -> np.ndarray:
    """Make each row's largest-magnitude component positive, so an SVD /
    eigendecomposition's arbitrary sign does not flip the map between runs."""
    if axes.size == 0:
        return axes
    idx = np.argmax(np.abs(axes), axis=1)
    signs = np.sign(axes[np.arange(axes.shape[0]), idx])
    signs[signs == 0] = 1.0
    return axes * signs[:, None]


def _pca_axes(data: np.ndarray, dims: int) -> tuple[np.ndarray, np.ndarray]:
    """``(mean, axes)`` of the top ``dims`` principal components.

    ``axes`` is ``(dims, d)``. Small inputs use an SVD of the centred matrix;
    large ones accumulate the ``d x d`` covariance in chunks so no ``n x d``
    float64 copy is ever made.
    """
    n, d = data.shape
    dims = max(0, min(dims, n - 1, d))
    mean = data.mean(axis=0, dtype=np.float64)
    if dims == 0:
        return mean, np.zeros((0, d))
    if n <= 2 * d:
        centred = data.astype(np.float64) - mean
        _, _, vt = np.linalg.svd(centred, full_matrices=False)
        return mean, _fix_signs(vt[:dims])
    cov = np.zeros((d, d), dtype=np.float64)
    for start in range(0, n, FIELD_PAGE_SIZE):
        chunk = data[start : start + FIELD_PAGE_SIZE].astype(np.float64) - mean
        cov += chunk.T @ chunk
    _, vecs = np.linalg.eigh(cov)
    axes = vecs[:, ::-1][:, :dims].T
    return mean, _fix_signs(axes)


def _project(data: np.ndarray, mean: np.ndarray, axes: np.ndarray) -> np.ndarray:
    out = np.empty((data.shape[0], axes.shape[0]), dtype=np.float64)
    for start in range(0, data.shape[0], FIELD_PAGE_SIZE):
        chunk = data[start : start + FIELD_PAGE_SIZE].astype(np.float64) - mean
        out[start : start + FIELD_PAGE_SIZE] = chunk @ axes.T
    return out


def _local_2d(points: np.ndarray, radius: float) -> np.ndarray:
    """A cluster's members in its own 2D PCA, filling a disc of ``radius``.

    Each local axis is scaled to unit spread so a cluster reads as a blob, not
    a line, then the cloud is sized so its 90th-percentile point sits on the
    rim; the few outliers beyond are pulled in to 1.25x the radius.
    """
    m = points.shape[0]
    if m == 1:
        return np.zeros((1, 2))
    centred = points - points.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    axes = _fix_signs(vt[:2])
    local = centred @ axes.T
    if local.shape[1] < 2:
        local = np.hstack([local, np.zeros((m, 2 - local.shape[1]))])
    std = local.std(axis=0)
    std[std < 1e-12] = 1.0
    local = local / std
    norms = np.linalg.norm(local, axis=1)
    scale = float(np.percentile(norms, 90)) if m > 2 else float(norms.max())
    if scale < 1e-12:
        return np.zeros((m, 2))
    local = local / scale * radius
    norms = np.linalg.norm(local, axis=1)
    cap = 1.25 * radius
    over = norms > cap
    local[over] = local[over] / norms[over][:, None] * cap
    return local


def _repel(anchors: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """Push overlapping cluster discs apart; deterministic."""
    k = anchors.shape[0]
    a = anchors.copy()
    for _ in range(_REPULSION_ITERS):
        moved = False
        for i in range(k):
            for j in range(i + 1, k):
                delta = a[j] - a[i]
                dist = float(np.hypot(delta[0], delta[1]))
                need = (radii[i] + radii[j]) * 1.12
                if dist >= need:
                    continue
                if dist < 1e-9:
                    angle = 2.0 * math.pi * ((i * 7 + j * 13) % 360) / 360.0
                    delta = np.array([math.cos(angle), math.sin(angle)])
                    dist = 1.0
                    push = need
                else:
                    push = need - dist
                unit = delta / dist
                # The smaller cluster moves more.
                wi = radii[j] / (radii[i] + radii[j])
                a[i] -= unit * push * wi
                a[j] += unit * push * (1.0 - wi)
                moved = True
        if not moved:
            break
    return a


def _jitter(memory_id: str) -> tuple[float, float]:
    digest = hashlib.sha256(memory_id.encode()).digest()
    angle = int.from_bytes(digest[:4], "big") / 2**32 * 2.0 * math.pi
    r = math.sqrt(int.from_bytes(digest[4:8], "big") / 2**32) * _UNPLACED_JITTER
    return 0.5 + r * math.cos(angle), 0.5 + r * math.sin(angle)


def compute_layout(ids: Sequence[str], vectors: np.ndarray, has_vec: np.ndarray) -> FieldLayout:
    """Lay out ``len(ids)`` rows. ``vectors`` is ``(n, d)``; rows where
    ``has_vec`` is False are ignored and placed as unplaced (cluster -1)."""
    n = len(ids)
    xs = [0.5] * n
    ys = [0.5] * n
    cluster = [-1] * n
    for i in range(n):
        if not has_vec[i]:
            xs[i], ys[i] = _jitter(ids[i])

    placed = np.flatnonzero(has_vec)
    m = int(placed.size)
    if m == 0:
        return FieldLayout(xs, ys, cluster, [], [], [])

    if m <= 3:
        # Too few points for PCA/k-means to mean anything: one cluster, the
        # points evenly spaced on a small circle around the centre.
        coords = np.zeros((m, 2))
        if m > 1:
            for t in range(m):
                angle = 2.0 * math.pi * t / m + math.pi / 2
                coords[t] = (0.15 * math.cos(angle), 0.15 * math.sin(angle))
        for t, idx in enumerate(placed.tolist()):
            xs[idx] = 0.5 + float(coords[t, 0])
            ys[idx] = 0.5 + float(coords[t, 1])
            cluster[idx] = 0
        return FieldLayout(xs, ys, cluster, [(0.5, 0.5)], [m], [[int(i) for i in placed]])

    data = vectors[placed]
    mean, axes = _pca_axes(data, _PCA_DIMS)
    reduced = _project(data, mean, axes)

    k = choose_k(m)
    labels, _ = numpy_kmeans(reduced, k, max_iters=30)
    # Compact relabel: drop empty clusters, largest first, ties by first member.
    groups: dict[int, list[int]] = {}
    for row, lab in enumerate(labels.tolist()):
        groups.setdefault(int(lab), []).append(row)
    ordered = sorted(groups.values(), key=lambda rows: (-len(rows), rows[0]))
    k = len(ordered)
    centroids = np.array([reduced[rows].mean(axis=0) for rows in ordered])
    sizes = np.array([len(rows) for rows in ordered], dtype=np.float64)

    # Centroids to 2D by their own PCA.
    if k > 1:
        c_mean, c_axes = _pca_axes(centroids, 2)
        anchors = (centroids - c_mean) @ c_axes.T
        if anchors.shape[1] < 2:
            anchors = np.hstack([anchors, np.zeros((k, 2 - anchors.shape[1]))])
        span = float(np.abs(anchors).max())
        anchors = anchors / span if span > 1e-12 else anchors
    else:
        anchors = np.zeros((1, 2))
    radii = 0.6 * np.sqrt(sizes / m)
    anchors = _repel(anchors, radii)

    coords = np.zeros((m, 2))
    for c, rows in enumerate(ordered):
        coords[rows] = anchors[c] + _local_2d(reduced[rows], float(radii[c]))

    # Normalise into [pad, 1 - pad], same scale on both axes, centred.
    lo = coords.min(axis=0)
    hi = coords.max(axis=0)
    span = float((hi - lo).max())
    scale = (1.0 - 2 * _PADDING) / span if span > 1e-12 else 0.0
    offset = 0.5 - (lo + hi) / 2.0 * scale

    def _norm(p: np.ndarray) -> tuple[float, float]:
        q = p * scale + offset
        return float(np.clip(q[0], 0.0, 1.0)), float(np.clip(q[1], 0.0, 1.0))

    members: list[list[int]] = []
    for c, rows in enumerate(ordered):
        members.append([int(placed[r]) for r in rows])
        for r in rows:
            i = int(placed[r])
            xs[i], ys[i] = _norm(coords[r])
            cluster[i] = c
    anchor_xy = [_norm(anchors[c]) for c in range(k)]
    return FieldLayout(xs, ys, cluster, anchor_xy, [len(rows) for rows in ordered], members)


# ---------------------------------------------------------------------------
# Cluster labels
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z][a-z0-9_-]{3,}")
_STOP_WORDS = frozenset(
    [
        "this",
        "that",
        "with",
        "from",
        "have",
        "been",
        "were",
        "will",
        "would",
        "should",
        "could",
        "about",
        "into",
        "over",
        "under",
        "after",
        "before",
        "their",
        "there",
        "then",
        "than",
        "them",
        "they",
        "what",
        "when",
        "where",
        "which",
        "while",
        "your",
        "using",
        "used",
        "user",
        "agent",
        "memory",
        "note",
        "notes",
        "task",
        "tasks",
        "also",
        "just",
        "more",
        "most",
        "some",
        "such",
        "only",
        "very",
        "each",
        "other",
        "same",
        "make",
        "made",
        "does",
        "done",
    ]
)
_LABEL_PART_MAX = 24
_LABEL_MAX = 32


def _short(text: str) -> str:
    text = " ".join(text.lower().split())
    return text if len(text) <= _LABEL_PART_MAX else text[: _LABEL_PART_MAX - 1].rstrip() + "…"


def label_clusters(
    members: list[list[int]],
    rows: Sequence[dict],
    entity_labels: dict[str, str],
) -> list[str]:
    """Short human labels, 1-2 parts joined with " · ".

    Entities are scored tf-idf style (count in the cluster times a rarity
    factor over clusters), so an entity linked everywhere — the tenant's own
    name, say — does not label every cluster. Fallback when a cluster has no
    usable entity: the most common memory_type plus a frequent title word.
    """
    k = len(members)
    per_cluster: list[Counter[str]] = []
    doc_freq: Counter[str] = Counter()
    for rows_in in members:
        counts: Counter[str] = Counter()
        for i in rows_in:
            for eid in rows[i].get("entity_ids") or []:
                if eid in entity_labels:
                    counts[eid] += 1
        per_cluster.append(counts)
        doc_freq.update(counts.keys())

    labels: list[str] = []
    for c, rows_in in enumerate(members):
        counts = per_cluster[c]
        min_count = 1 if len(rows_in) <= 3 else 2
        scored = sorted(
            (
                (cnt * math.log(1.0 + k / doc_freq[eid]), entity_labels[eid], eid)
                for eid, cnt in counts.items()
                if cnt >= min_count
            ),
            key=lambda t: (-t[0], t[1]),
        )
        parts: list[str] = []
        for _, name, _eid in scored:
            short = _short(name)
            if not short:
                continue
            if not parts:
                parts.append(short)
                continue
            # A second part only when it adds something and the label stays
            # short: "reranker · cross-encoder reranker" says one thing twice.
            first = parts[0]
            if short in first or first in short:
                continue
            if len(first) + len(short) <= _LABEL_MAX:
                parts.append(short)
            break
        if not parts:
            types = Counter(rows[i].get("memory_type") or "" for i in rows_in)
            types.pop("", None)
            if types:
                parts.append(_short(sorted(types.items(), key=lambda t: (-t[1], t[0]))[0][0]))
            words: Counter[str] = Counter()
            for i in rows_in:
                words.update(set(_WORD_RE.findall((rows[i].get("title") or "").lower())) - _STOP_WORDS)
            if words:
                word = sorted(words.items(), key=lambda t: (-t[1], t[0]))[0][0]
                if _short(word) not in parts:
                    parts.append(_short(word))
        labels.append(" · ".join(parts) if parts else f"cluster {c + 1}")
    return labels


# ---------------------------------------------------------------------------
# Payload assembly
# ---------------------------------------------------------------------------


def decode_embedding(b64: str | None) -> np.ndarray | None:
    if not b64:
        return None
    return np.frombuffer(base64.b64decode(b64), dtype="<f4")


def build_field(
    rows: Sequence[dict],
    vectors: np.ndarray,
    has_vec: np.ndarray,
    entities: dict[str, dict],
    *,
    total: int,
    signature: str,
) -> dict[str, Any]:
    """The ``/memories/field`` response body for ``rows`` (newest first)."""
    ids = [r["id"] for r in rows]
    layout = compute_layout(ids, vectors, has_vec)
    entity_labels = {eid: e.get("label") or "" for eid, e in entities.items()}
    labels = label_clusters(layout.members, rows, entity_labels)

    # Inverse of ``supersedes_id`` within the returned set; rows arrive newest
    # first, so the first successor seen is the newest one.
    superseded_by: dict[str, str] = {}
    for r in rows:
        prior = r.get("supersedes_id")
        if prior and prior not in superseded_by:
            superseded_by[prior] = r["id"]

    linked: set[str] = set()
    points = []
    for i, r in enumerate(rows):
        entity_ids = list(r.get("entity_ids") or [])
        linked.update(entity_ids)
        points.append(
            {
                "id": r["id"],
                "x": round(layout.x[i], 5),
                "y": round(layout.y[i], 5),
                "cluster": layout.cluster[i],
                "agent_id": r.get("agent_id"),
                "fleet_id": r.get("fleet_id"),
                "memory_type": r.get("memory_type"),
                "weight": r.get("weight"),
                "title": r.get("title"),
                "preview": (r.get("preview") or "")[:FIELD_PREVIEW_CHARS],
                "content_chars": int(r.get("content_chars") or 0),
                "created_at": r.get("created_at"),
                "recall_count": int(r.get("recall_count") or 0),
                "last_recalled_at": r.get("last_recalled_at"),
                "status": r.get("status"),
                "visibility": r.get("visibility"),
                "supersedes_id": r.get("supersedes_id"),
                "superseded_by": superseded_by.get(r["id"]),
                "entity_ids": entity_ids,
            }
        )

    clusters = [
        {
            "id": c,
            "label": labels[c],
            "x": round(layout.anchors[c][0], 5),
            "y": round(layout.anchors[c][1], 5),
            "size": layout.sizes[c],
        }
        for c in range(len(layout.sizes))
    ]
    version_src = "|".join(
        [FIELD_KIND, signature, *(f"{rid}:{int(has_vec[i])}" for i, rid in enumerate(ids))]
    )
    return {
        "kind": FIELD_KIND,
        "model_version": hashlib.sha256(version_src.encode()).hexdigest()[:12],
        "computed_at": datetime.now(UTC).isoformat(),
        "total": total,
        "clusters": clusters,
        "points": points,
        "entities": [
            {"id": eid, "label": entities[eid].get("label"), "type": entities[eid].get("type")}
            for eid in sorted(linked)
            if eid in entities
        ],
    }


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------


class _FieldCache:
    """Small in-process TTL + LRU cache of computed field payloads.

    Keyed on the caller's full visibility scope plus the storage signature
    (row count, embedded count, newest write/status change), so a write, an
    embedding landing or a status flip misses, and two callers who may see
    different rows never share an entry.
    """

    def __init__(self, max_entries: int = FIELD_CACHE_MAX_ENTRIES, ttl: float = FIELD_CACHE_TTL_SECONDS):
        self._data: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
        self._max = max_entries
        self._ttl = ttl

    def get(self, key: tuple) -> dict | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, payload = hit
        if expires < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return payload

    def put(self, key: tuple, payload: dict) -> None:
        self._data[key] = (time.monotonic() + self._ttl, payload)
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


field_cache = _FieldCache()


async def get_memory_field(scope: dict, *, limit: int) -> dict:
    """Fetch (paged, newest first) and lay out the caller's memories.

    ``scope`` is the visibility body the storage field reads take
    (``tenant_id``, ``caller_agent_id``, ``caller_tenant_id``, ``fleet_id``,
    ``include_scope_agent``) — the same values ``GET /memories`` sends to
    ``/memories/list``.
    """
    sc = get_storage_client()
    sig = await sc.memory_field_signature(scope)
    signature = f"{sig.get('count')}:{sig.get('embedded')}:{sig.get('newest')}"
    key = (
        scope.get("tenant_id"),
        scope.get("fleet_id"),
        scope.get("caller_agent_id"),
        scope.get("caller_tenant_id"),
        bool(scope.get("include_scope_agent")),
        limit,
        signature,
    )
    cached = field_cache.get(key)
    if cached is not None:
        return cached

    rows: list[dict] = []
    entities: dict[str, dict] = {}
    chunks: list[np.ndarray] = []
    flags: list[bool] = []
    dim = 0
    cursor: dict | None = None
    while len(rows) < limit:
        body = {**scope, "limit": min(FIELD_PAGE_SIZE, limit - len(rows))}
        if cursor:
            body["cursor_ts"] = cursor["ts"]
            body["cursor_id"] = cursor["id"]
        page = await sc.memory_field_page(body)
        page_rows = page.get("rows") or []
        page_vecs: list[np.ndarray | None] = []
        for r in page_rows:
            vec = decode_embedding(r.pop("embedding_b64", None))
            if vec is not None and dim == 0:
                dim = int(vec.shape[0])
            page_vecs.append(vec if vec is not None and vec.shape[0] == dim else None)
        rows.extend(page_rows)
        for e in page.get("entities") or []:
            entities.setdefault(e["id"], e)
        # float16 holds the working set at half size (50k x 1024 = 100 MB);
        # the PCA itself runs in float64 chunk by chunk.
        width = dim or 1
        block = np.zeros((len(page_vecs), width), dtype=np.float16)
        for t, vec in enumerate(page_vecs):
            if vec is not None:
                block[t] = vec.astype(np.float16)
            flags.append(vec is not None)
        chunks.append(block)
        cursor = page.get("next_cursor")
        if not cursor or not page_rows:
            break

    width = dim or 1
    chunks = [c if c.shape[1] == width else np.zeros((c.shape[0], width), dtype=np.float16) for c in chunks]
    # A page fetched before the first embedding was seen has width 1; its rows
    # carry no vector, so zero-filling it is exact.
    vectors = np.concatenate(chunks) if chunks else np.zeros((0, width), dtype=np.float16)
    has_vec = np.array(flags, dtype=bool)

    payload = await asyncio.to_thread(
        build_field,
        rows,
        vectors,
        has_vec,
        entities,
        total=int(sig.get("count") or len(rows)),
        signature=signature,
    )
    field_cache.put(key, payload)
    return payload
