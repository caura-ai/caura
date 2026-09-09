"""reg-a70 step 2a — the atomic-fact fan-out is now callable from both paths.

Pure refactor: ``fan_out_atomic_facts`` is the block that used to live inline in
``_enrich_memory_background``, unchanged. It moved because the SYNCHRONOUS path
was its only possible caller, which is precisely why a fast-mode write of
multi-claim content produced fewer memories than the same content in strong
mode — the async worker had no way to run it short of a second implementation.

The existing fan-out tests are the real proof that behaviour did not change.
These pin the seam itself: that the function exists with the contract the async
caller needs, that the sync path goes through it, and that the guarantees whose
absence caused incidents are still described where the next reader will look.
"""

import inspect

import pytest

from core_api.services import memory_service as ms

pytestmark = pytest.mark.unit


def test_the_fanout_is_a_module_level_function():
    """Inline in a 600-line background task, it was reachable from exactly one
    place. That was the defect, not a style issue."""
    assert callable(ms.fan_out_atomic_facts)


def test_it_takes_everything_it_needs_as_parameters():
    """No reliance on caller locals — otherwise the async path cannot call it."""
    params = inspect.signature(ms.fan_out_atomic_facts).parameters
    for name in (
        "atomic_facts",
        "memory_id",
        "tenant_id",
        "fleet_id",
        "agent_id",
        "parent_metadata",
        "parent_visibility",
        "parent_weight",
        "parent_ts_start",
        "tenant_config",
    ):
        assert name in params, f"{name} is not a parameter"


async def test_no_facts_is_a_cheap_no_op():
    """The async caller will invoke this on every enriched row, most of which
    have no atomic facts. It must not cost a storage call to decide that."""
    out = await ms.fan_out_atomic_facts(
        object(),  # a storage client that would explode if touched
        atomic_facts=[],
        memory_id="m1",
        tenant_id="t1",
        fleet_id=None,
        agent_id="a1",
        parent_metadata={},
        parent_visibility="scope_team",
        parent_weight=0.5,
        parent_ts_start=None,
        tenant_config=None,
    )
    assert out == {"created": 0, "deduped": 0, "unembedded": 0}


def test_it_reports_counts_the_caller_can_log():
    src = inspect.getsource(ms.fan_out_atomic_facts)
    assert '"created": fanout_created' in src
    assert '"deduped": fanout_deduped' in src
    assert '"unembedded": fanout_unembedded' in src


def test_the_sync_path_now_goes_through_it():
    """A refactor that leaves the old copy behind is two implementations, which
    is the thing this avoids."""
    src = inspect.getsource(ms._enrich_memory_background)
    assert "await fan_out_atomic_facts(" in src
    assert "fanout_created = 0" not in src, "the inline copy is still there"


def test_visibility_is_passed_in_not_re_read():
    """#808: a row read before the governance PATCH still carries the visibility
    ``keep_private`` just removed. The parameter is what stops a child
    inheriting a permission the policy revoked."""
    src = inspect.getsource(ms.fan_out_atomic_facts)
    assert 'mem.get("visibility")' not in src
    assert "parent_visibility" in src

    call = inspect.getsource(ms._enrich_memory_background)
    assert "parent_visibility = effective_visibility or" in call


@pytest.mark.parametrize(
    "marker",
    ["#808", "CAURA-222", "OSS #814", "CAURA-602", "2026-07-27"],
)
def test_the_incident_provenance_survived_the_move(marker):
    """Every guarantee in this function was paid for by an incident. A refactor
    that drops the reasons is how they get re-litigated and reverted."""
    src = inspect.getsource(ms.fan_out_atomic_facts)
    assert marker in src, f"{marker}'s rationale was lost in the extraction"
