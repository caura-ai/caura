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
5. Each axis normalised by the 3rd-97th percentile of the points to
   ``[0.06, 0.94]``, outliers clamped into ``[0.02, 0.98]``.

Deterministic for the same inputs. Rows without an embedding yet still appear,
with ``cluster = -1``, near the centre with a small jitter derived from their id.

The fit is saved (``FieldModel``, in core-storage's ``/projections``): one
process fits a tenant under a storage claim and saves the model and every
memory's position, and a read serves the saved positions. A memory with no
position yet is placed with the saved model, a few small matrix products, and
saved, so a new memory appears without fitting again and no point moves. Only
a tenant with too few embeddings to fit is laid out on each request.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import logging
import math
import re
import time
from collections import Counter, OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import uuid4

import httpx
import numpy as np
from fastapi import HTTPException
from slowapi.wrappers import LimitGroup

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.middleware.per_tenant_concurrency import per_tenant_slot
from core_api.middleware.rate_limit import limiter
from core_api.services.kmeans import numpy_kmeans

logger = logging.getLogger(__name__)

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
_ROBUST_LO_PCT = 3.0
_ROBUST_HI_PCT = 97.0
_CLAMP_LO = 0.02
_CLAMP_HI = 0.98
_MAX_ANCHOR_SHIFT = 0.5


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


def _local_fit(points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """A cluster's own 2D PCA: ``(mean, axes, std, scale)``, what
    ``_local_apply`` needs to put a point inside the cluster's disc.

    Each local axis is scaled to unit spread so a cluster reads as a blob, not
    a line, and ``scale`` is the 90th-percentile point's distance after that,
    which ``_local_apply`` puts on the rim. ``scale`` is 0 for a cluster with
    no spread (one member, or all alike): its members sit at the anchor.
    """
    m, p = points.shape
    mean = points.mean(axis=0)
    if m == 1:
        return mean, np.zeros((2, p)), np.ones(2), 0.0
    centred = points - mean
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    axes = _fix_signs(vt[:2])
    if axes.shape[0] < 2:
        axes = np.vstack([axes, np.zeros((2 - axes.shape[0], p))])
    local = centred @ axes.T
    std = local.std(axis=0)
    std[std < 1e-12] = 1.0
    norms = np.linalg.norm(local / std, axis=1)
    scale = float(np.percentile(norms, 90)) if m > 2 else float(norms.max())
    return mean, axes, std, scale if scale >= 1e-12 else 0.0


def _local_apply(
    points: np.ndarray, mean: np.ndarray, axes: np.ndarray, std: np.ndarray, scale: float, radius: float
) -> np.ndarray:
    """Points in a cluster's disc of ``radius``, by the cluster's
    ``_local_fit``; the few beyond the rim are pulled in to 1.25x the radius."""
    if scale < 1e-12:
        return np.zeros((points.shape[0], 2))
    local = ((points - mean) @ axes.T) / std / scale * radius
    norms = np.linalg.norm(local, axis=1)
    cap = 1.25 * radius
    over = norms > cap
    local[over] = local[over] / norms[over][:, None] * cap
    return local


def _spread_anchors(anchors: np.ndarray, sizes: np.ndarray) -> np.ndarray:
    """Centre the centroid layout on its size-weighted mean, scale each axis to
    [-1, 1] and compress it (signed square root), so one outlying centroid
    cannot push every other cluster into a corner of the map."""
    centre = (anchors * sizes[:, None]).sum(axis=0) / sizes.sum()
    out = anchors - centre
    for axis in range(out.shape[1]):
        span = float(np.abs(out[:, axis]).max())
        if span > 1e-12:
            out[:, axis] = np.sign(out[:, axis]) * np.sqrt(np.abs(out[:, axis]) / span)
    return out


def _robust_range(coords: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-axis 3rd-97th percentile of ``coords``, falling back to min/max on
    an axis whose percentile range is degenerate."""
    lo = np.percentile(coords, _ROBUST_LO_PCT, axis=0)
    hi = np.percentile(coords, _ROBUST_HI_PCT, axis=0)
    flat = hi - lo <= 1e-12
    lo = np.where(flat, coords.min(axis=0), lo)
    hi = np.where(flat, coords.max(axis=0), hi)
    return lo, hi


def _repel(anchors: np.ndarray, radii: np.ndarray) -> np.ndarray:
    """Push overlapping cluster discs apart; deterministic. No anchor moves
    more than ``_MAX_ANCHOR_SHIFT`` from where the centroid PCA put it, so a
    tiny cluster cannot be shoved out to the edge of the map."""
    k = anchors.shape[0]
    a = anchors.copy()
    start = anchors.copy()
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
        shift = a - start
        dist = np.hypot(shift[:, 0], shift[:, 1])
        over = dist > _MAX_ANCHOR_SHIFT
        a[over] = start[over] + shift[over] / dist[over][:, None] * _MAX_ANCHOR_SHIFT
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

    model, ordered, coords = _fit_layout(vectors[placed])
    k = len(ordered)

    def _norm(p: np.ndarray) -> tuple[float, float]:
        q = np.clip(p * model.norm_scale + model.norm_offset, _CLAMP_LO, _CLAMP_HI)
        return float(q[0]), float(q[1])

    members: list[list[int]] = []
    for c, rows in enumerate(ordered):
        members.append([int(placed[r]) for r in rows])
        for r in rows:
            i = int(placed[r])
            xs[i], ys[i] = _norm(coords[r])
            cluster[i] = c
    anchor_xy = [_norm(model.anchors[c]) for c in range(k)]
    return FieldLayout(xs, ys, cluster, anchor_xy, [len(rows) for rows in ordered], members)


# ---------------------------------------------------------------------------
# The fitted model: what is saved, and how a new embedding is placed with it
# ---------------------------------------------------------------------------

# The arrays a model is, in the order ``FieldModel`` declares them.
_MODEL_ARRAYS = (
    "mean",
    "axes",
    "centroids",
    "local_mean",
    "local_axes",
    "local_std",
    "local_scale",
    "radii",
    "anchors",
    "norm_scale",
    "norm_offset",
    "sizes",
)


@dataclass
class FieldModel:
    """A fitted layout: enough to place any embedding without fitting again.

    ``mean`` and ``axes`` project an embedding to the PCA space (``(d,)`` and
    ``(p, d)``); the nearest of the ``k`` ``centroids`` picks its cluster; the
    cluster's ``local_*`` and ``radii`` put it inside that cluster's disc
    around its ``anchors`` row; ``norm_scale`` and ``norm_offset`` map the
    result into ``[0, 1]``. ``sizes`` counts each cluster's members at the fit.
    Numbers only: no memory text, so a saved model outlives no deleted memory.
    """

    mean: np.ndarray
    axes: np.ndarray
    centroids: np.ndarray
    local_mean: np.ndarray
    local_axes: np.ndarray
    local_std: np.ndarray
    local_scale: np.ndarray
    radii: np.ndarray
    anchors: np.ndarray
    norm_scale: np.ndarray
    norm_offset: np.ndarray
    sizes: np.ndarray

    @property
    def dim(self) -> int:
        return int(self.mean.shape[0])

    def to_bytes(self) -> bytes:
        """The model as an ``.npz`` of float32 arrays, about 210 KB for
        1,024-dimension embeddings: what storage saves as its parameters."""
        buf = io.BytesIO()
        arrays: dict[str, Any] = {
            name: np.asarray(getattr(self, name), dtype=np.float32) for name in _MODEL_ARRAYS
        }
        np.savez(buf, **arrays)
        return buf.getvalue()

    @classmethod
    def from_bytes(cls, raw: bytes) -> FieldModel:
        # ``allow_pickle=False``: parameters are arrays, never objects.
        with np.load(io.BytesIO(raw), allow_pickle=False) as arrays:
            return cls(**{name: arrays[name].astype(np.float64) for name in _MODEL_ARRAYS})


def _fit_layout(data: np.ndarray) -> tuple[FieldModel, list[list[int]], np.ndarray]:
    """Fit the layout on ``data`` (four rows or more).

    Returns the model, each cluster's rows as k-means assigned them (largest
    cluster first, ties by first member), and those rows' 2D coordinates
    before normalisation, which is what ``compute_layout`` draws.
    """
    m = data.shape[0]
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
        anchors = _spread_anchors(anchors, sizes)
    else:
        anchors = np.zeros((1, 2))
    radii = 0.6 * np.sqrt(sizes / m)
    anchors = _repel(anchors, radii)

    p = reduced.shape[1]
    local_mean = np.zeros((k, p))
    local_axes = np.zeros((k, 2, p))
    local_std = np.ones((k, 2))
    local_scale = np.zeros(k)
    coords = np.zeros((m, 2))
    for c, rows in enumerate(ordered):
        local_mean[c], local_axes[c], local_std[c], local_scale[c] = _local_fit(reduced[rows])
        coords[rows] = anchors[c] + _local_apply(
            reduced[rows], local_mean[c], local_axes[c], local_std[c], float(local_scale[c]), float(radii[c])
        )

    # Normalise each axis by the 3rd-97th percentile of the POINTS, mapped to
    # [pad, 1 - pad], outliers clamped into [0.02, 0.98]. Min/max scaling let
    # one far-off tiny cluster squeeze everything else into a corner.
    lo, hi = _robust_range(coords)
    scale = np.where(hi - lo > 1e-12, (1.0 - 2 * _PADDING) / np.maximum(hi - lo, 1e-12), 0.0)
    offset = np.where(hi - lo > 1e-12, _PADDING - lo * scale, 0.5)
    model = FieldModel(
        mean=mean,
        axes=axes,
        centroids=centroids,
        local_mean=local_mean,
        local_axes=local_axes,
        local_std=local_std,
        local_scale=local_scale,
        radii=radii,
        anchors=anchors,
        norm_scale=scale,
        norm_offset=offset,
        sizes=sizes,
    )
    return model, ordered, coords


def fit_model(vectors: np.ndarray) -> FieldModel:
    """Fit a layout model on ``vectors`` (``(n, d)``, four rows or more)."""
    data = np.asarray(vectors)
    if data.shape[0] < MIN_FIT:
        raise ValueError(f"a layout fit needs at least {MIN_FIT} embeddings, got {data.shape[0]}")
    return _fit_layout(data)[0]


def place(model: FieldModel, vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Place ``vectors`` (``(n, d)``) with ``model``: ``(xy, cluster)``, with
    ``xy`` in ``[0.02, 0.98]`` and each row in the cluster whose centroid is
    nearest. Pure numpy, a few small matrix products per row."""
    data = np.asarray(vectors)
    if data.shape[0] == 0:
        return np.zeros((0, 2)), np.zeros(0, dtype=np.int64)
    reduced = _project(data, model.mean, model.axes)
    distances = (
        (reduced**2).sum(axis=1)[:, None]
        - 2.0 * reduced @ model.centroids.T
        + (model.centroids**2).sum(axis=1)[None, :]
    )
    cluster = distances.argmin(axis=1)
    coords = np.zeros((data.shape[0], 2))
    for c in np.unique(cluster).tolist():
        rows = np.flatnonzero(cluster == c)
        coords[rows] = model.anchors[c] + _local_apply(
            reduced[rows],
            model.local_mean[c],
            model.local_axes[c],
            model.local_std[c],
            float(model.local_scale[c]),
            float(model.radii[c]),
        )
    xy = np.clip(coords * model.norm_scale + model.norm_offset, _CLAMP_LO, _CLAMP_HI)
    return xy, cluster


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
    """The ``/memories/field`` response body for ``rows`` (newest first), laid
    out on request: what a tenant with too few embeddings to fit is served."""
    ids = [r["id"] for r in rows]
    layout = compute_layout(ids, vectors, has_vec)
    version_src = "|".join(
        [FIELD_KIND, signature, *(f"{rid}:{int(has_vec[i])}" for i, rid in enumerate(ids))]
    )
    return _assemble_field(rows, layout, entities, total=total, version_src=version_src)


def _assemble_field(
    rows: Sequence[dict],
    layout: FieldLayout,
    entities: dict[str, dict],
    *,
    total: int,
    version_src: str,
) -> dict[str, Any]:
    """The response body for ``rows`` placed by ``layout``. A cluster none of
    the rows falls in (a saved model's, for this caller's rows) is left out."""
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
        if layout.sizes[c]
    ]
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

    def pop(self, key: tuple) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()


field_cache = _FieldCache()
_signature_cache = _FieldCache(ttl=5.0)


class FieldBusyError(Exception):
    """The map cannot be read or fitted right now; the route answers 429."""


class FieldPreparingError(Exception):
    """The tenant's layout is being fitted; the route answers 429 until it is saved."""


@dataclass
class SavedModel:
    """A tenant's saved fit: the version naming it, and the model itself."""

    version: str
    model: FieldModel


# A fit needs four embeddings; fewer are laid out on each request (``build_field``).
MIN_FIT = 4
# A tenant with up to this many embedded memories fits inside its first request,
# in a few seconds; a larger one fits in the background while requests answer
# that the map is being prepared.
FIT_INLINE_MAX = 5000
# How long a request waits for an inline fit before answering that the map is
# being prepared. The fit itself goes on and is saved; the next request finds it.
FIT_INLINE_WAIT_SECONDS = 30.0
# The layout is fitted on at most this many embeddings, spread evenly over the
# newest ``FIELD_MAX_LIMIT``; every memory in that window is then placed.
FIT_SAMPLE = 20000
# How long a fit's claim holds before another process may take the tenant over.
FIT_LEASE_SECONDS = 900
# A read places at most this many memories that have no position yet; the rest
# are drawn unplaced and placed by the reads that follow.
PLACE_PER_REQUEST = 4000
# Positions per save, the storage route's bound.
POSITIONS_BATCH = 2000
# How long a fit that failed, or found too few shared embeddings to fit, holds
# its tenant off fitting again. A fit pages up to ``FIELD_MAX_LIMIT``
# embeddings, so a failure that would recur must not be retried by every
# request that follows it, and a tenant with too few shared memories is laid
# out on request without paging for a fit each time.
FIT_RETRY_SECONDS = 30.0
# A tenant whose shared memories are too few to fit, while a caller sees
# enough of its own private ones, is laid out on request: from at most this
# many of the caller's newest memories, holding one of the process's layout
# slots (``_fit_slots``), so it costs no more than an inline fit.
ON_REQUEST_MAX = FIT_INLINE_MAX

# Fits running in this process, by tenant, and reads in flight, by cache key.
# Touched only on this process's event loop, so they need no lock.
_fits: dict[str, asyncio.Task[SavedModel | None]] = {}
# Each tenant's last fit that saved nothing: ``(until, "failed" | "too_few")``.
_fit_outcomes: OrderedDict[str, tuple[float, str]] = OrderedDict()
_serves: dict[tuple, asyncio.Task[dict]] = {}
_fit_slots = asyncio.Semaphore(settings.memory_field_max_concurrent_builds)
# Which version each tenant's saved model is, rechecked every five seconds,
# and the decoded models by version, so a recheck downloads but does not decode.
_current_model = _FieldCache(ttl=5.0)
_decoded_models = _FieldCache()
# Signature reads share in-flight work and cache successful results for five
# seconds per visibility scope, avoiding a full scan on every payload hit.
_signatures: dict[tuple, asyncio.Task[dict]] = {}


def _reset_state() -> None:
    """Forget every cache and every fit or read in flight (tests)."""
    global _fit_slots
    for cache in (field_cache, _signature_cache, _current_model, _decoded_models):
        cache.clear()
    # A semaphore a waiter has used is bound to that event loop; a test's loop
    # is gone with the test.
    _fit_slots = asyncio.Semaphore(settings.memory_field_max_concurrent_builds)
    _fits.clear()
    _fit_outcomes.clear()
    _serves.clear()
    _signatures.clear()


def _scope_key(scope: dict) -> tuple:
    # Storage reads a missing ``caller_tenant_id`` as the tenant itself, so a
    # tenant credential's scope and the fit's tenant-wide one are one key.
    return (
        scope.get("tenant_id"),
        scope.get("fleet_id"),
        scope.get("caller_agent_id"),
        scope.get("caller_tenant_id") or scope.get("tenant_id"),
        bool(scope.get("include_scope_agent")),
    )


async def _signature(scope: dict) -> dict:
    key = _scope_key(scope)
    cached = _signature_cache.get(key)
    if cached is not None:
        return cached
    read = _signatures.get(key)
    if read is None:
        read = asyncio.create_task(get_storage_client().memory_field_signature(scope))
        _signatures[key] = read

        def _forget(done: asyncio.Task[dict]) -> None:
            if _signatures.get(key) is done:
                del _signatures[key]
            if not done.cancelled() and done.exception() is None:
                _signature_cache.put(key, done.result())

        read.add_done_callback(_forget)
    try:
        return await asyncio.shield(read)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 503:
            raise FieldBusyError from exc
        raise


async def get_memory_field(scope: dict, *, limit: int) -> dict:
    """Rate-limit map requests per tenant, then serve the saved layout.

    Signatures refresh within five seconds. See ``_get_memory_field``.
    """
    tenant = scope["tenant_id"]
    if limiter.enabled:
        rates = LimitGroup(
            settings.rate_limit_search,
            key_function=lambda: tenant,
            scope="memory-field",
            per_method=False,
            methods=None,
            error_message=None,
            exempt_when=None,
            cost=1,
            override_defaults=True,
        )
        for rate in rates:
            try:
                allowed = limiter.limiter.hit(rate.limit, rate.scope, rate.key_func())
            except Exception:
                # Direct backend calls do not inherit slowapi's swallow_errors.
                logger.warning("Memory map rate limiter backend unavailable; allowing request", exc_info=True)
                continue
            if not allowed:
                raise HTTPException(
                    status_code=429, detail="Memory map request rate exceeded", headers={"Retry-After": "1"}
                )
    return await _get_memory_field(scope, limit=limit)


async def _get_memory_field(scope: dict, *, limit: int) -> dict:
    """The caller's map, from the tenant's saved layout.

    No saved model yet: the request claims the tenant's fit. With at most
    ``FIT_INLINE_MAX`` shared embedded memories it fits inline and is served,
    if the fit finishes within ``FIT_INLINE_WAIT_SECONDS``; a larger tenant
    fits in the background and this answers
    ``FieldPreparingError`` until the model is saved, as does any request
    while another process holds the claim. A tenant with fewer than
    ``MIN_FIT`` embeddings is laid out on each request instead, and so is one
    whose last fit found too few shared embeddings; after a failed fit,
    requests answer ``FieldBusyError`` for ``FIT_RETRY_SECONDS`` rather than
    fitting again.
    """
    tenant = scope["tenant_id"]
    # Only the signature read occupies a search slot.
    async with per_tenant_slot("search", tenant):
        sig = await _signature(scope)
    signature = f"{sig.get('count')}:{sig.get('embedded')}:{sig.get('newest')}:{sig.get('revision')}"
    embedded = int(sig.get("embedded") or 0)
    saved = await _saved_model(tenant)
    if saved is None and embedded >= MIN_FIT:
        outcome = _fit_outcome(tenant)
        if outcome == "failed":
            raise FieldBusyError
        if outcome != "too_few":
            # Inline or not by the fit's size: it pages the tenant's shared
            # memories, which a narrow caller (one fleet, one agent) can see
            # few of. The same five-second signature cache serves it.
            async with per_tenant_slot("search", tenant):
                shared = await _signature({"tenant_id": tenant})
            inline = int(shared.get("embedded") or 0) <= FIT_INLINE_MAX
            saved = await _fit_or_wait(tenant, inline=inline)
    # Laid out on request although there are embeddings enough to fit: bound it.
    bounded = saved is None and embedded >= MIN_FIT
    if bounded:
        limit = min(limit, ON_REQUEST_MAX)

    key = (*_scope_key(scope), signature, saved.version if saved else None, limit)
    cached = field_cache.get(key)
    if cached is not None:
        return cached
    serve = _serves.get(key)
    if serve is None:
        serve = asyncio.create_task(
            _serve(scope, key, saved, limit=limit, total=int(sig.get("count") or 0), bounded=bounded)
        )
        _serves[key] = serve

        def _forget(done: asyncio.Task[dict]) -> None:
            if _serves.get(key) is done:
                del _serves[key]
            if not done.cancelled():
                done.exception()  # retrieved, so a read every caller left does not log it

        serve.add_done_callback(_forget)
    return await asyncio.shield(serve)


async def _saved_model(tenant: str, *, fresh: bool = False) -> SavedModel | None:
    """The tenant's saved model, or None before its first fit."""
    hit = None if fresh else _current_model.get((tenant,))
    if hit is not None:
        return hit["saved"]
    got = await get_storage_client().projection_model_get(tenant)
    saved: SavedModel | None = None
    version = got.get("model_version")
    if version and got.get("params_b64"):
        decoded = _decoded_models.get((tenant, version))
        if decoded is None:
            model = FieldModel.from_bytes(base64.b64decode(got["params_b64"]))
            decoded = {"saved": SavedModel(version=version, model=model)}
            _decoded_models.put((tenant, version), decoded)
        saved = decoded["saved"]
    _current_model.put((tenant,), {"saved": saved})
    return saved


async def _fit_or_wait(tenant: str, *, inline: bool) -> SavedModel | None:
    """Start the tenant's fit if this process is not already running it, and
    wait for it when ``inline``. None when the tenant has too few shared
    embeddings to fit.

    The task is registered before anything is awaited, so concurrent requests
    in this process share one claim and one fit rather than racing each other
    for the claim and answering that the map is being prepared.
    """
    task = _fits.get(tenant)
    if task is None:
        task = asyncio.create_task(_claim_and_fit(tenant))
        _fits[tenant] = task

        def _done(done: asyncio.Task[SavedModel | None]) -> None:
            if _fits.get(tenant) is done:
                del _fits[tenant]
            _current_model.pop((tenant,))
            if done.cancelled():
                return
            exc = done.exception()  # retrieved; ``_fit`` logged a failure
            if exc is None:
                if done.result() is None:
                    _record_fit_outcome(tenant, "too_few")
                else:
                    _fit_outcomes.pop(tenant, None)
            elif not isinstance(exc, FieldPreparingError):
                # Not a claim another process holds: this fit itself failed.
                _record_fit_outcome(tenant, "failed")

        task.add_done_callback(_done)
    if not inline:
        raise FieldPreparingError
    waiting = asyncio.timeout(FIT_INLINE_WAIT_SECONDS)
    try:
        async with waiting:
            return await asyncio.shield(task)
    except FieldPreparingError:
        raise
    except Exception as exc:
        if waiting.expired():
            # Stopped waiting; the shielded fit goes on and is saved.
            raise FieldPreparingError from None
        raise FieldBusyError from exc


def _fit_outcome(tenant: str) -> str | None:
    """``"failed"`` or ``"too_few"`` while the tenant's last fit holds it off."""
    hit = _fit_outcomes.get(tenant)
    if hit is None:
        return None
    if hit[0] < time.monotonic():
        _fit_outcomes.pop(tenant, None)
        return None
    return hit[1]


def _record_fit_outcome(tenant: str, outcome: str) -> None:
    _fit_outcomes[tenant] = (time.monotonic() + FIT_RETRY_SECONDS, outcome)
    _fit_outcomes.move_to_end(tenant)
    while len(_fit_outcomes) > 1024:
        _fit_outcomes.popitem(last=False)


async def _claim_and_fit(tenant: str) -> SavedModel | None:
    """Claim the tenant's fit in storage and run it. ``FieldPreparingError``
    when another process holds the claim."""
    sc = get_storage_client()
    token = uuid4().hex
    claim = await sc.projection_claim(tenant, token, FIT_LEASE_SECONDS)
    if not claim.get("claimed"):
        raise FieldPreparingError
    if claim.get("model_version"):
        # Saved since this process read the model (a replica behind the
        # primary): serve that one rather than fitting again.
        await sc.projection_release(tenant, token)
        saved = await _saved_model(tenant, fresh=True)
        if saved is None:
            raise FieldPreparingError
        return saved
    return await _fit(tenant, token)


@dataclass
class _Pages:
    rows: list[dict]
    entities: dict[str, dict]
    # Each row's embedding as float16, or None when the page did not carry one.
    vectors: list[np.ndarray | None]
    dim: int
    pages: int
    storage_s: float
    decode_s: float


def _stack(vectors: list[np.ndarray | None], rows: list[int]) -> np.ndarray:
    """The embeddings of ``rows``, every one of which has one, as a matrix."""
    picked = [vectors[i] for i in rows]
    return np.stack([v for v in picked if v is not None])


async def _page(body: dict, *, limit: int) -> _Pages:
    """Up to ``limit`` rows of the field page for ``body``, newest first."""
    sc = get_storage_client()
    out = _Pages(rows=[], entities={}, vectors=[], dim=0, pages=0, storage_s=0.0, decode_s=0.0)
    cursor: dict | None = None
    while len(out.rows) < limit:
        page_body = {**body, "limit": min(FIELD_PAGE_SIZE, limit - len(out.rows))}
        if cursor:
            page_body["cursor_ts"] = cursor["ts"]
            page_body["cursor_id"] = cursor["id"]
        asked = time.monotonic()
        page = await sc.memory_field_page(page_body)
        decoding = time.monotonic()
        out.storage_s += decoding - asked
        out.pages += 1
        page_rows = page.get("rows") or []
        for r in page_rows:
            vec = decode_embedding(r.pop("embedding_b64", None))
            if vec is not None and out.dim == 0:
                out.dim = int(vec.shape[0])
            # float16 holds the working set at half size (50k x 1024 = 100 MB);
            # the PCA itself runs in float64 chunk by chunk.
            out.vectors.append(
                vec.astype(np.float16) if vec is not None and vec.shape[0] == out.dim else None
            )
        out.rows.extend(page_rows)
        for e in page.get("entities") or []:
            out.entities.setdefault(e["id"], e)
        out.decode_s += time.monotonic() - decoding
        cursor = page.get("next_cursor")
        if not cursor or not page_rows:
            break
    return out


def _lost_claim(tenant: str) -> NoReturn:
    """A fit whose claim has gone stops: another process is fitting the
    tenant, so the map is being prepared. Never a failure, never too few."""
    logger.warning(
        "Memory map fit for tenant %s lost its claim before saving",
        tenant,
        extra={"path": "memory-field-fit-lost", "tenant_id": tenant},
    )
    raise FieldPreparingError


async def _fit(tenant: str, token: str) -> SavedModel | None:
    """Fit the tenant's layout under ``token``'s claim and save it.

    Pages the newest ``FIELD_MAX_LIMIT`` of the tenant's shared memories with
    their embeddings (agent-private memories do not shape the layout; they
    are placed when read), fits on a sample, places every row, and saves the
    positions and then the model. Logs one line saying where the time went.
    Gives the claim back on any failure, so the next request can fit again.
    """
    sc = get_storage_client()
    started = time.monotonic()
    try:
        async with _fit_slots:
            version = f"{FIELD_KIND}-{uuid4().hex[:12]}"
            got = await _page(
                {"tenant_id": tenant, "model_version": version, "embeddings": "all"}, limit=FIELD_MAX_LIMIT
            )
            embedded = np.array([i for i, v in enumerate(got.vectors) if v is not None], dtype=np.int64)
            if embedded.size < MIN_FIT:
                await sc.projection_release(tenant, token)
                return None
            vectors = _stack(got.vectors, embedded.tolist())
            sample = (
                np.arange(embedded.size)
                if embedded.size <= FIT_SAMPLE
                else np.unique(np.linspace(0, embedded.size - 1, FIT_SAMPLE).astype(np.int64))
            )
            laying_out = time.monotonic()
            model = await asyncio.to_thread(fit_model, vectors[sample])
            xy, cluster = await asyncio.to_thread(place, model, vectors)
            layout_s = time.monotonic() - laying_out

            saving = time.monotonic()
            items = [
                {
                    "memory_id": got.rows[i]["id"],
                    "x": float(xy[t, 0]),
                    "y": float(xy[t, 1]),
                    "cluster": int(cluster[t]),
                    "row_version": got.rows[i]["row_version"],
                }
                for t, i in enumerate(embedded.tolist())
            ]
            for start in range(0, len(items), POSITIONS_BATCH):
                batch = items[start : start + POSITIONS_BATCH]
                if await sc.projection_positions_save(tenant, version, batch, claim_token=token) is None:
                    # Refused: the claim has gone, so another process holds
                    # the tenant. The rows saved so far go with its model save.
                    _lost_claim(tenant)
            stored = await sc.projection_model_save(
                tenant,
                token,
                model_version=version,
                kind=FIELD_KIND,
                dim=model.dim,
                n_fit=int(sample.size),
                params=model.to_bytes(),
            )
            if not stored and (await sc.projection_model_get(tenant)).get("model_version") != version:
                # A fit that outlived its lease: another process took the
                # tenant over. Its positions go with the next model save.
                _lost_claim(tenant)
            finished = time.monotonic()
            ms = {
                "storage_ms": round(got.storage_s * 1000),
                "decode_ms": round(got.decode_s * 1000),
                "layout_ms": round(layout_s * 1000),
                "save_ms": round((finished - saving) * 1000),
                "total_ms": round((finished - started) * 1000),
            }
            logger.info(
                "Memory map fitted for tenant %s: %d points (%d embedded, %d sampled) in %d ms "
                "(storage %d ms over %d pages, decode %d ms, layout %d ms, save %d ms)",
                tenant,
                len(got.rows),
                int(embedded.size),
                int(sample.size),
                ms["total_ms"],
                ms["storage_ms"],
                got.pages,
                ms["decode_ms"],
                ms["layout_ms"],
                ms["save_ms"],
                extra={
                    "path": "memory-field-fit",
                    "tenant_id": tenant,
                    "model_version": version,
                    "points": len(got.rows),
                    "embedded": int(embedded.size),
                    "sample": int(sample.size),
                    "pages": got.pages,
                    **ms,
                },
            )
            return SavedModel(version=version, model=model)
    except FieldPreparingError:
        raise
    except Exception:
        logger.warning("Memory map fit for tenant %s failed", tenant, exc_info=True)
        with contextlib.suppress(Exception):
            await sc.projection_release(tenant, token)
        raise


async def _serve(
    scope: dict, key: tuple, saved: SavedModel | None, *, limit: int, total: int, bounded: bool = False
) -> dict:
    """Read the caller's newest ``limit`` rows and assemble their map.

    With a saved model, rows carry their saved positions and only rows with
    none carry an embedding: up to ``PLACE_PER_REQUEST`` of those are placed
    with the model and saved, so the next read finds them placed. Without one,
    the rows are laid out on this request (``build_field``); ``bounded`` (a
    tenant with embeddings enough to fit, but too few of them shared) holds
    one of the process's layout slots while it does.
    """
    if saved is None:
        async with _fit_slots if bounded else contextlib.nullcontext():
            got = await _page({**scope, "embeddings": "all"}, limit=limit)
            width = got.dim or 1
            vectors = np.zeros((len(got.rows), width), dtype=np.float16)
            for i, vec in enumerate(got.vectors):
                if vec is not None:
                    vectors[i] = vec
            has_vec = np.array([v is not None for v in got.vectors], dtype=bool)
            payload = await asyncio.to_thread(
                build_field, got.rows, vectors, has_vec, got.entities, total=total, signature=str(key[-3])
            )
        field_cache.put(key, payload)
        return payload

    got = await _page({**scope, "model_version": saved.version, "embeddings": "unplaced"}, limit=limit)
    n = len(got.rows)
    xs = [0.5] * n
    ys = [0.5] * n
    clusters = [-1] * n
    for i, r in enumerate(got.rows):
        if r.get("x") is not None:
            xs[i], ys[i], clusters[i] = float(r["x"]), float(r["y"]), int(r["cluster"])
    todo = [
        i
        for i, r in enumerate(got.rows)
        if r.get("x") is None and (vec := got.vectors[i]) is not None and vec.shape[0] == saved.model.dim
    ][:PLACE_PER_REQUEST]
    if todo:
        xy, cluster = await asyncio.to_thread(place, saved.model, _stack(got.vectors, todo))
        items = []
        for t, i in enumerate(todo):
            xs[i], ys[i], clusters[i] = float(xy[t, 0]), float(xy[t, 1]), int(cluster[t])
            items.append(
                {
                    "memory_id": got.rows[i]["id"],
                    "x": xs[i],
                    "y": ys[i],
                    "cluster": clusters[i],
                    "row_version": got.rows[i]["row_version"],
                }
            )
        sc = get_storage_client()
        for start in range(0, len(items), POSITIONS_BATCH):
            # None: a refit saved a new model since this read began, and the
            # next read places these under it.
            await sc.projection_positions_save(
                scope["tenant_id"], saved.version, items[start : start + POSITIONS_BATCH]
            )
    for i, r in enumerate(got.rows):
        if clusters[i] < 0:
            xs[i], ys[i] = _jitter(r["id"])

    k = int(saved.model.centroids.shape[0])
    members: list[list[int]] = [[] for _ in range(k)]
    for i, c in enumerate(clusters):
        if 0 <= c < k:
            members[c].append(i)
    anchors = np.clip(
        saved.model.anchors * saved.model.norm_scale + saved.model.norm_offset, _CLAMP_LO, _CLAMP_HI
    )
    layout = FieldLayout(
        x=xs,
        y=ys,
        cluster=clusters,
        anchors=[(float(a[0]), float(a[1])) for a in anchors],
        sizes=[len(m) for m in members],
        members=members,
    )
    version_src = "|".join(
        [
            FIELD_KIND,
            str(key[-3]),
            saved.version,
            *(f"{r['id']}:{c}" for r, c in zip(got.rows, clusters, strict=True)),
        ]
    )
    payload = _assemble_field(got.rows, layout, got.entities, total=total, version_src=version_src)
    field_cache.put(key, payload)
    return payload
