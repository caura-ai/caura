"""A63 — the candidate floor the ROUTE applies must be the shared constant.

The `/memories/similar-candidates` route defaulted ``threshold`` to a
hardcoded ``0.7`` and ``limit`` to a hardcoded ``20``, while the service
signature defaulted to ``CONTRADICTION_SIMILARITY_THRESHOLD`` /
``CONTRADICTION_CANDIDATE_MAX``. No caller passes either value — the
contradiction detector sends neither — so the ROUTE's literals were the
values prod actually ran, and the constants were decorative. Lowering the
constant alone would have changed nothing.

That is the same third-place-duplication trap the scored-search route
documents at length (two ranking features shipped applying to one search
path only because a route-level allowlist shadowed the caller). These
tests pin the wiring so it cannot silently drift back.
"""

from __future__ import annotations

import inspect

import pytest

from common.constants import (
    CONTRADICTION_CANDIDATE_WINDOW,
    CONTRADICTION_SIMILARITY_THRESHOLD,
)

pytestmark = pytest.mark.unit


def _route_source() -> str:
    from core_storage_api.routers import memories as router_mod

    src = inspect.getsource(router_mod)
    start = src.index("async def find_similar_candidates")
    return src[start : src.index("@router.", start + 10)]


def test_route_defaults_come_from_constants_not_literals() -> None:
    src = _route_source()
    assert "CONTRADICTION_SIMILARITY_THRESHOLD" in src, (
        "the route must default the candidate floor to the shared constant; a "
        "literal here shadows it for every caller and makes the constant dead"
    )
    assert "CONTRADICTION_CANDIDATE_WINDOW" in src
    assert '"threshold", 0.7' not in src and "'threshold', 0.7" not in src
    assert '"limit", 20' not in src and "'limit', 20" not in src


def test_floor_admits_the_measured_real_updates() -> None:
    """The four paraphrased updates measured through the real pipeline
    (A63 probe, 2026-08-27) must clear the floor — they are the miss class
    this change exists to fix."""
    measured = [0.483, 0.503, 0.568, 0.607]
    assert all(sim >= CONTRADICTION_SIMILARITY_THRESHOLD for sim in measured), (
        f"floor {CONTRADICTION_SIMILARITY_THRESHOLD} would still drop "
        f"{[s for s in measured if s < CONTRADICTION_SIMILARITY_THRESHOLD]}"
    )


def test_window_is_what_bounds_judge_cost() -> None:
    """The cost argument for the lower floor rests on the window staying
    put: ORDER BY similarity LIMIT N means the floor cannot add an
    (N+1)-th candidate. If someone raises the window, judge cost per write
    rises with it and the E3/E4 measurements no longer describe prod."""
    assert CONTRADICTION_CANDIDATE_WINDOW == 20
