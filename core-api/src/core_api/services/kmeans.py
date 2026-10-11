"""Dependency-light k-means shared by the insights discover mode and the
memory field layout. numpy only — no sklearn."""

from __future__ import annotations

from typing import Any

import numpy as np


def numpy_kmeans(data: Any, k: int, max_iters: int = 20) -> tuple[Any, Any]:
    """Simple k-means clustering using only numpy.

    Deterministic for the same input: centroids are seeded from a fixed-seed
    generator. Returns ``(labels, centroids)``; a cluster that empties out is
    re-seeded from a random row, so callers must tolerate (and may drop)
    clusters with no members.
    """
    n = data.shape[0]
    # Initialize centroids via random sampling
    rng = np.random.default_rng(42)  # deterministic for reproducibility
    indices = rng.choice(n, size=k, replace=False)
    centroids = data[indices].copy()

    # Initialize to -1 (sentinel) so the convergence check on the first
    # iteration doesn't false-positive when all points happen to be assigned
    # to cluster 0.
    labels = np.full(n, -1, dtype=np.int32)
    for _ in range(max_iters):
        # Assign each point to nearest centroid (squared-distance, no huge intermediate)
        data_sq = np.sum(data**2, axis=1, keepdims=True)
        cent_sq = np.sum(centroids**2, axis=1)[None, :]
        dists = data_sq + cent_sq - 2.0 * (data @ centroids.T)
        new_labels = np.argmin(dists, axis=1).astype(np.int32)

        if np.array_equal(labels, new_labels):
            break
        labels = new_labels

        # Update centroids
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = data[mask].mean(axis=0)
            else:
                centroids[j] = data[rng.integers(n)]

    return labels, centroids
