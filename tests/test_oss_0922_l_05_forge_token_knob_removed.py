"""oss-0922-l-05 — ``skills_factory.forge.llm_tokens_per_run`` is gone.

It was declared in ``DEFAULT_SETTINGS`` with a default of 50,000, type-checked
as ``int`` in ``_LEAF_TYPES``, carried on the ``forge-distill-requested`` event
and on its publisher's signature — and read by nothing, anywhere in the tree.
A tenant could set it, the dashboard rendered it back, and it bounded nothing.

That is worse than an absent control, which is the whole reason this row was
filed: an operator who sets a token ceiling stops looking for one. Same family
as the dead sentinel checks (oss-0902-l-35, oss-0922-l-01).

WHY REMOVED RATHER THAN ENFORCED. Two reasons, and the first is the one that
settles it.

The unit is not available to count. ``LlmFn`` is ``Callable[[str],
Awaitable[str]]`` — prompt in, text out, no usage channel. Only the
OpenAI-compatible provider computes token usage at all
(``common/llm/providers/openai.py`` ``_usage_tokens``), and it LOGS the numbers;
``gemini.py`` and ``vertex.py`` never read usage. Enforcing a ceiling on that
plumbing would cap spend for OpenAI tenants and silently not cap it for
Gemini/Vertex tenants — reproducing this very defect per-provider, where it
would be considerably harder to notice.

And a token ceiling is the wrong shape for the loop it would govern. A run
cannot know a distill call's cost until it has paid for it. The budget check in
``run_forge_distill`` sits at the TOP of the loop precisely so a full budget
stops the run BEFORE buying a call it cannot use; a token counter could only
ever stop it afterwards, or stop it on an estimate — at which point the knob
again does not mean what it says.

WHAT BOUNDS FORGE SPEND NOW, stated plainly because leaving it to inference is
how this row happened: ``skills_factory.forge.max_clusters_per_run``, and
nothing else. It bounds ATTEMPTS — one distill LLM call each — not tokens and
not dollars. ``test_forge_spend_is_bounded_by_attempts_not_tokens`` below is
that statement in executable form, so the next knob promising a cost ceiling has
to argue with a failing test rather than slip in beside a gap.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from tests.conftest import get_test_auth

_REMOVED_KEY = "llm_tokens_per_run"


# ── the three layers it used to span ─────────────────────────────────────


@pytest.mark.unit
def test_the_settings_key_is_gone():
    from core_api.services.organization_settings import _LEAF_TYPES, DEFAULT_SETTINGS

    forge = DEFAULT_SETTINGS["skills_factory"]["forge"]
    assert _REMOVED_KEY not in forge, (
        "llm_tokens_per_run is back in DEFAULT_SETTINGS. Nothing reads it, so a "
        "tenant setting it would again believe forge spend is capped when it is "
        "not — see this module's docstring before re-adding it."
    )
    assert f"skills_factory.forge.{_REMOVED_KEY}" not in _LEAF_TYPES


@pytest.mark.unit
def test_the_event_payload_field_is_gone():
    from common.events.lifecycle_forge_request import LifecycleForgeDistillRequest

    assert _REMOVED_KEY not in LifecycleForgeDistillRequest.model_fields


@pytest.mark.unit
def test_the_publisher_kwarg_is_gone():
    from common.events.lifecycle_publishers import publish_forge_distill_request

    assert (
        _REMOVED_KEY not in inspect.signature(publish_forge_distill_request).parameters
    )


# ── the settings route now rejects it ────────────────────────────────────


async def test_a_write_to_the_removed_key_is_rejected(client):
    """Through the real route, not through ``_check_keys`` directly.

    A key can be absent from ``DEFAULT_SETTINGS`` and still be accepted on write
    if validation is bypassed anywhere on the path, and the inverse mistake —
    a key present in the resolver but absent from ``DEFAULT_SETTINGS``, so
    unsettable while reading as shipped — is exactly what pm-0918-c-03 cost. The
    route is where both of those show up; a direct call to the validator is
    where neither does.

    422 rather than a silent drop is the intended answer: a tenant who is still
    sending this key holds a belief about their spend that is wrong, and an
    error is what corrects it. Silently accepting-and-ignoring would preserve
    the original defect exactly.
    """
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"skills_factory": {"forge": {_REMOVED_KEY: 50_000}}},
        headers=headers,
    )
    assert resp.status_code == 422, (
        f"the removed knob is still writable (got {resp.status_code}) — a tenant "
        f"can set a token ceiling that bounds nothing: {resp.text}"
    )
    assert _REMOVED_KEY in resp.text, (
        "the rejection does not name the key, so an operator cannot tell which "
        f"of their settings was refused: {resp.text}"
    )


async def test_the_surviving_forge_knobs_still_write(client):
    """The control. A removal that broke the whole ``forge`` settings group
    would also make the test above pass, and for the wrong reason.
    """
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{uuid.uuid4().hex[:8]}")

    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"skills_factory": {"forge": {"max_clusters_per_run": 30}}},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    forge = reloaded.json()["skills_factory"]["forge"]
    assert forge["max_clusters_per_run"] == 30
    assert _REMOVED_KEY not in forge, (
        "the removed key is still rendered back to the dashboard — the knob "
        "would read as live to anyone looking at the settings surface"
    )


# ── the honest statement about what IS bounded ───────────────────────────


@pytest.mark.unit
def test_forge_spend_is_bounded_by_attempts_not_tokens():
    """The answer to "is there a per-tenant ceiling on Forge LLM spend?" — no,
    and attempts are the only lever.

    Written as a test rather than as a comment because a comment does not fail
    when someone adds ``max_cost_usd_per_run`` or ``token_budget`` to the forge
    block and wires it to nothing. That is the precise mistake this row records,
    and it is cheap to repeat: the settings layer accepts any declared key
    whether or not a consumer exists, so nothing else in the tree notices.

    The assertion is deliberately about the SHAPE of the knob names, not about
    a fixed list. A new forge knob is fine; a new forge knob that promises a
    token or cost ceiling is a claim, and the claim has to be true.

    THERE IS A SANCTIONED WAY TO ADD ONE, and this test is where to find it, so
    that "no cost ceiling" does not read as "a cost ceiling is impossible".
    ``agent_digest`` already ships one: ``max_cost_per_run_usd`` is turned into
    a CALL budget up front by dividing by ``_PER_CALL_COST_USD``, a hardcoded
    per-call estimate that its own comment calls "an estimate, not
    billing-grade" (``services/agent_digest.py:43-45, 244``). That works because
    the ceiling is resolved into calls BEFORE any are bought — which is the same
    shape as ``max_clusters_per_run``, just with a division in front of it.
    Forge has the call budget already and in the enforceable unit; a dollar knob
    on top would be a convenience over it, not a new guarantee. What is NOT
    available is the thing the removed key implied: counting actual tokens
    consumed. That needs a usage channel the ``LlmFn`` contract does not have
    and three of the four providers do not compute.
    """
    from core_api.services.forge.forge_service import ForgeConfig
    from core_api.services.organization_settings import DEFAULT_SETTINGS

    forge = DEFAULT_SETTINGS["skills_factory"]["forge"]

    spend_words = ("token", "cost", "usd", "dollar", "spend", "budget")
    claims_a_cost_ceiling = sorted(
        k for k in forge if any(w in k.lower() for w in spend_words)
    )
    assert not claims_a_cost_ceiling, (
        f"forge settings key(s) {claims_a_cost_ceiling} name a token or cost "
        "ceiling. Forge has no such ceiling: the LLM callable returns text with "
        "no usage channel, and only one of the four providers computes token "
        "counts at all. If one of these is genuinely enforced, add it to this "
        "test with the consumer that reads it; if it is not, it is "
        "oss-0922-l-05 again."
    )

    # What IS the ceiling, and it is read — ``run_forge_distill`` breaks the
    # cluster loop on it, so this is a live consumer and not another
    # declaration. Asserted through the source rather than by running a forge
    # run: the point is that the knob reaches the loop, and a run would need an
    # LLM call to prove far less than that.
    assert "max_clusters_per_run" in forge
    assert "max_clusters_per_run" in vars(ForgeConfig())

    from core_api.services.forge import forge_service

    loop_src = inspect.getsource(forge_service.run_forge_distill)
    assert "effective_max_clusters_per_run" in loop_src, (
        "the attempt ceiling no longer reaches the distill loop — which would "
        "leave Forge spend bounded by nothing at all"
    )
