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


def test_the_switch_survives_the_settings_write_path():
    """The switch has to survive a settings WRITE, not just a resolver read.

    Caught by review, and it made the whole feature inert: `_check_keys`
    validates a payload against `DEFAULT_SETTINGS`, so a knob that exists only
    as a `ResolvedConfig` property is READ-ONLY — the resolver serves its
    default while `PUT /settings` 422s.

    Every other test in this file builds `ResolvedConfig` directly, which
    bypasses that validation and passes against a switch nobody can switch.
    Shape copied from `test_entity_retrieval_flag.test_settings_key_accepted_and_type_checked`,
    which is the established way to pin a new knob.
    """
    from core_api.services.organization_settings import (
        DEFAULT_SETTINGS,
        _check_keys,
        _validate_leaf_types,
    )

    payload = {"enrichment": {"atomic_fact_fanout_enabled": False}}
    _check_keys(payload, DEFAULT_SETTINGS)
    _validate_leaf_types(payload)

    # A string "false" is TRUTHY. Without the type check it would resolve to
    # "on" while the dashboard rendered the tenant's "off" back to them.
    with pytest.raises(ValueError, match="atomic_fact_fanout_enabled"):
        _validate_leaf_types({"enrichment": {"atomic_fact_fanout_enabled": "false"}})

    with pytest.raises(ValueError, match="Unknown settings key"):
        _check_keys(
            {"enrichment": {"atomic_fact_fanout_enable": False}}, DEFAULT_SETTINGS
        )


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


@pytest.mark.asyncio
async def test_a_config_without_the_knob_keeps_fanning_out():
    """Older config objects and existing test doubles predate this knob, and
    must resolve to today's behaviour rather than raising ``AttributeError``
    inside a fire-and-forget enrichment task.

    Asserted by CALLING the function with such an object, not by reading the
    source for a ``getattr`` — a source assertion passes just as well for a
    gate that reads the attribute and then does the wrong thing with it.
    """
    from types import SimpleNamespace

    from core_api.services.memory_service import fan_out_atomic_facts

    reached = {"storage": False}

    class _Storage:
        def __getattr__(self, name):
            reached["storage"] = True
            raise _Stop

    class _Stop(Exception):
        pass

    class _Fact:
        content = "x"
        suggested_type = "fact"
        retrieval_hint = ""

    with pytest.raises(_Stop):
        await fan_out_atomic_facts(
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
            tenant_config=SimpleNamespace(),  # no such attribute at all
        )
    # It got PAST the gate and reached storage — i.e. it defaulted to ON.
    assert reached["storage"] is True


@pytest.mark.asyncio
async def test_the_worker_path_honours_the_switch_too():
    """The gate lives in the shared function so BOTH callers get it. This
    exercises the worker caller end to end rather than asserting that a string
    appears in one function's source and not another's — an assertion that
    passes just as well for a gate returning the wrong answer.

    `consumer._fan_out_persisted_atomic_facts` is the path A70 added, and the
    one a bulk/deferred write takes.
    """
    from core_api import consumer

    class _Storage:
        def __getattr__(self, name):
            raise AssertionError(
                f"storage was called ({name}) with the fan-out disabled"
            )

    class _Payload:
        memory_id = "00000000-0000-0000-0000-000000000001"
        tenant_id = "t1"

    class _Outcome:
        visibility = "scope_team"

    memory = {
        "metadata_": {"atomic_facts": [{"content": "x", "suggested_type": "fact"}]},
        "fleet_id": None,
        "agent_id": "a1",
        "weight": 0.5,
        "ts_valid_start": None,
    }

    class _Cfg:
        atomic_fact_fanout_enabled = False

    async def _resolve(_tenant):
        return _Cfg()

    orig = consumer.resolve_config
    consumer.resolve_config = _resolve
    try:
        # Must not raise, and must not reach storage: a disabled tenant creates
        # no children on the worker path either.
        await consumer._fan_out_persisted_atomic_facts(
            _Storage(), memory, _Payload(), _Outcome()
        )
    finally:
        consumer.resolve_config = orig
