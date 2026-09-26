"""A72 — the crystallizer could not see the pathology it is the safety net for.

The sweep clusters near-duplicates, LLM-re-extracts atomic facts, and archives
the sources. It missed composite crowding on two of the three grounds the report
names, both of which are constants:

* **the band** — `CRYSTALLIZER_DEDUP_THRESHOLD = 0.95` catches near-verbatim
  copies, while the composites that actually crowd recall sit around 0.75-0.90
  and pass underneath it untouched;
* **the cluster floor** — `CRYSTALLIZER_MIN_CLUSTER_SIZE = 3` skips the most
  common overlap of all, a pair, so two rows saying the same thing survive every
  sweep.

Both are now per-tenant and both default to the existing constant, so an
untouched tenant sweeps exactly as before. That default is not timidity: every
pair a wider band admits, and every cluster a lower floor admits, is another LLM
mergeability judgement. A tenant that wants the janitor to reach the crowding
band opts in and pays for it; nobody inherits that bill from a deploy.

The report's third ground — the once-a-day trigger — is **not** in this change.
It needs a crystallizer activity gate in storage plus a cron cadence change in
`core-operations`, which is a different service. See the PR.
"""

from types import SimpleNamespace

import pytest

from core_api.constants import (
    CRYSTALLIZER_DEDUP_THRESHOLD,
    CRYSTALLIZER_MIN_CLUSTER_SIZE,
)
from core_api.services.organization_settings import ResolvedConfig

pytestmark = pytest.mark.unit


def _cfg(**crystallizer):
    return (
        ResolvedConfig({"crystallizer": crystallizer})
        if crystallizer
        else ResolvedConfig({})
    )


# ── defaults preserve today's behaviour ───────────────────────────────────


def test_unset_resolves_to_the_shipped_constants():
    """The whole safety property. A tenant that never opts in must sweep the
    same band, admit the same clusters, and spend the same number of LLM calls
    as before this change."""
    cfg = _cfg()
    assert cfg.crystallizer_dedup_threshold == CRYSTALLIZER_DEDUP_THRESHOLD
    assert cfg.crystallizer_min_cluster_size == CRYSTALLIZER_MIN_CLUSTER_SIZE


def test_the_shipped_defaults_are_the_ones_the_report_criticises():
    """Pins the starting point, so a future default change is a deliberate act
    and not something that drifts in unnoticed."""
    assert CRYSTALLIZER_DEDUP_THRESHOLD == 0.95
    assert CRYSTALLIZER_MIN_CLUSTER_SIZE == 3


# ── the retune the report asks for ────────────────────────────────────────


def test_a_tenant_can_lower_the_band_into_the_crowding_range():
    """0.80-0.95 is where the composites live. This is the retune."""
    assert _cfg(dedup_threshold=0.80).crystallizer_dedup_threshold == 0.80


def test_a_tenant_can_admit_two_row_clusters():
    """The most common overlap is a pair, and at the default floor of 3 it is
    skipped entirely."""
    assert _cfg(min_cluster_size=2).crystallizer_min_cluster_size == 2


# ── bounds, and why each one is where it is ───────────────────────────────


@pytest.mark.parametrize("raw,expected", [(0.2, 0.5), (0.5, 0.5), (1.5, 1.0)])
def test_the_band_is_clamped(raw, expected):
    """Below 0.5 the sweep stops being a duplicate check and becomes a topic
    clusterer — it would hand unrelated rows to the re-extractor and merge them.
    Above 1.0 is not a cosine."""
    assert _cfg(dedup_threshold=raw).crystallizer_dedup_threshold == expected


@pytest.mark.parametrize("raw", [1, 0, -5])
def test_the_cluster_floor_never_drops_below_two(raw):
    """A cluster of one is not a cluster. Allowing it would feed single
    memories to the LLM re-extractor, which is pure cost for no merge."""
    assert _cfg(min_cluster_size=raw).crystallizer_min_cluster_size == 2


def test_string_values_are_coerced_not_crashed():
    """Settings arrive over the wire as JSON and a tenant may send "0.8"."""
    assert _cfg(dedup_threshold="0.8").crystallizer_dedup_threshold == 0.8
    assert _cfg(min_cluster_size="2").crystallizer_min_cluster_size == 2


# ── the service reads the knobs ───────────────────────────────────────────


def test_the_cluster_filter_uses_the_resolved_value():
    import inspect

    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs._run_crystallization)
    assert "crystallizer_min_cluster_size" in src
    assert "len(c) >= min_cluster" in src
    # the bare constant must no longer gate the filter, or the knob is inert
    assert "len(c) >= CRYSTALLIZER_MIN_CLUSTER_SIZE" not in src


def test_the_sweep_sends_the_resolved_threshold():
    import inspect

    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs._check_near_duplicates)
    assert '"threshold": threshold' in src
    assert '"threshold": CRYSTALLIZER_DEDUP_THRESHOLD' not in src


def test_a_config_without_the_knob_falls_back_rather_than_raising():
    """Older deploys and existing test doubles pass config objects that predate
    this knob. Matching ``getattr(tenant_config, "merge_near_duplicates", ...)``
    in DetectNearDuplicate, those resolve to today's behaviour instead of
    raising ``AttributeError`` inside a scheduled sweep."""
    import inspect

    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs._run_crystallization)
    assert 'getattr(config, "crystallizer_min_cluster_size"' in src

    legacy = SimpleNamespace()  # no crystallizer_* attributes at all
    assert (
        getattr(legacy, "crystallizer_min_cluster_size", CRYSTALLIZER_MIN_CLUSTER_SIZE)
        == 3
    )
