"""pm-0918-c-04 — the atomic-fact fan-out is switchable per tenant.

A70 shipped the fan-out on a measurement that said it almost never fires, taken
on conversational content. The row asked whether it should be gated off for
document-shaped writes, on the theory that 2,000-character chunks are the shape
it fires on.

**That question is still open.** The attempt to settle it against the local
corpus failed: every fan-out child in that database came from benchmark
conversation data, the non-benchmark slice produced none at all, and the corpus
predates A70 — so it holds no worker-path fan-out, and pre-A70 deferred writes
discarded their facts, which reads identically to "did not fan out". See
`docs/atomic-fact-fanout/pm-c04-fanout-rate-findings.md`, including what the
first version of that document got wrong.

So what ships is a switch, not a threshold — there is no evidence for where a
threshold would go, which is different from evidence that none helps. The
switch stands on its own: a tenant whose results are crowded by fan-out children
can turn them off for its own store without a deploy.
"""

import inspect

import pytest

from core_api.services.organization_settings import ResolvedConfig

pytestmark = pytest.mark.unit


def _cfg(**enrichment):
    return ResolvedConfig({"enrichment": enrichment} if enrichment else {})


# ── the switch ───────────────────────────────────────────────────────────


def test_the_fanout_is_on_by_default():
    """Today's behaviour. The write-side volume is small — 703 of 60,029
    parents fanned out — so switching it off by default would change what every
    tenant's store contains to fix a problem measured on one."""
    assert _cfg().atomic_fact_fanout_enabled is True


def test_a_tenant_can_switch_it_off():
    assert _cfg(atomic_fact_fanout_enabled=False).atomic_fact_fanout_enabled is False


def test_an_explicit_true_is_honoured():
    """A tenant that turns it back on must not be read as 'unset'."""
    assert _cfg(atomic_fact_fanout_enabled=True).atomic_fact_fanout_enabled is True


def test_the_switch_can_actually_be_written():
    """The switch has to survive a settings WRITE, not just a resolver read.

    Caught by review, and it made the whole feature inert: `_check_keys`
    validates a settings payload against `DEFAULT_SETTINGS`, so a knob that
    exists only as a `ResolvedConfig` property is READ-ONLY — the resolver
    happily returns its default while every attempt to set it raises
    `Unknown settings key(s)`.

    Every other test in this file builds `ResolvedConfig` directly, which
    bypasses that validation entirely and passes against a switch nobody can
    switch. This one goes through the door a tenant goes through.
    """
    from core_api.services.organization_settings import DEFAULT_SETTINGS, _check_keys

    _check_keys({"enrichment": {"atomic_fact_fanout_enabled": False}}, DEFAULT_SETTINGS)


def test_the_switch_is_type_checked_on_write():
    """Registered in `_LEAF_TYPES` like every other boolean knob, so a string
    "false" — which is truthy, and would silently leave the fan-out ON — is
    refused at the boundary rather than resolved."""
    from core_api.services.organization_settings import _LEAF_TYPES

    assert _LEAF_TYPES["enrichment.atomic_fact_fanout_enabled"] is bool


# ── the gate itself ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_disabled_tenant_creates_no_children():
    """The whole point. A disabled tenant must reach neither the embedder nor
    ``create_memory`` — the saving is the children not being embedded, so a
    gate that ran the batch and discarded it would save nothing."""
    from core_api.services.memory_service import fan_out_atomic_facts

    class _Cfg:
        atomic_fact_fanout_enabled = False

    class _Storage:
        def __getattr__(self, name):
            raise AssertionError(
                f"storage was called ({name}) with the fan-out disabled"
            )

    class _Fact:
        content = "Rachel got engaged on May 15th"
        suggested_type = "fact"
        retrieval_hint = ""

    counts = await fan_out_atomic_facts(
        _Storage(),
        atomic_facts=[_Fact()],
        memory_id="00000000-0000-0000-0000-000000000001",
        tenant_id="t1",
        fleet_id=None,
        agent_id="a1",
        parent_metadata={},
        parent_visibility="scope_team",
        parent_weight=0.5,
        parent_ts_start=None,
        tenant_config=_Cfg(),
    )

    assert counts == {"created": 0, "deduped": 0, "unembedded": 0}


@pytest.mark.asyncio
async def test_the_disabled_result_lets_the_worker_clear_its_marker():
    """Returning zeroed counts rather than raising is load-bearing.

    ``consumer._fan_out_persisted_atomic_facts`` preserves the ``atomic_facts``
    marker on an exception (so a redelivery can retry) and clears it otherwise.
    Raising here would not loop forever — the consumer catches and returns
    without nacking — but it would leave the marker set on every write by a
    tenant that deliberately switched the fan-out off, so each redelivery
    re-enters a fan-out that is disabled.
    """
    from core_api.services.memory_service import fan_out_atomic_facts

    class _Cfg:
        atomic_fact_fanout_enabled = False

    class _Fact:
        content = "x"
        suggested_type = "fact"
        retrieval_hint = ""

    # Must return, not raise.
    await fan_out_atomic_facts(
        object(),
        atomic_facts=[_Fact()],
        memory_id="00000000-0000-0000-0000-000000000001",
        tenant_id="t1",
        fleet_id=None,
        agent_id="a1",
        parent_metadata={},
        parent_visibility="scope_team",
        parent_weight=0.5,
        parent_ts_start=None,
        tenant_config=_Cfg(),
    )


def test_a_config_without_the_knob_keeps_fanning_out():
    """Older config objects and existing test doubles predate this knob. They
    must resolve to today's behaviour rather than raising ``AttributeError``
    inside a fire-and-forget enrichment task — the same reasoning
    ``_run_crystallization`` documents for ``crystallizer_min_cluster_size``."""
    from types import SimpleNamespace

    from core_api.services.memory_service import fan_out_atomic_facts

    src = inspect.getsource(fan_out_atomic_facts)
    assert 'getattr(tenant_config, "atomic_fact_fanout_enabled", True)' in src

    legacy = SimpleNamespace()  # no such attribute at all
    assert getattr(legacy, "atomic_fact_fanout_enabled", True) is True


def test_the_gate_sits_at_the_shared_chokepoint_not_a_call_site():
    """Both the synchronous path and the worker path funnel through this one
    function — that is why it exists rather than being inlined twice. A switch
    honoured by only one of them would silently become a per-write-mode
    difference, which is the exact class of bug A70 was fixing when it lifted
    this function out of ``_enrich_memory_background``."""
    from core_api import consumer
    from core_api.services import memory_service

    assert "atomic_fact_fanout_enabled" in inspect.getsource(
        memory_service.fan_out_atomic_facts
    )
    # Neither caller re-implements the decision.
    assert "atomic_fact_fanout_enabled" not in inspect.getsource(
        consumer._fan_out_persisted_atomic_facts
    )
