"""F9 — an empty keystone set said nothing about why it was empty.

Agents are taught to call keystones at every session start and obey what comes
back. A tenant that never authored a rule paid that round-trip and received
``count: 0`` — a result that is indistinguishable, from the caller's side, from

* "this tenant has no standing policy",
* "authoring failed", and
* "you asked the wrong scope".

The first is the reasonable inference, so the agent stops asking and carries
standing constraints in recall instead — the exact thing keystones exist to
prevent. The empty result now names the one fact a caller cannot discover from
an empty array: that authoring is a separate, trust-gated step.

Emitted only when the set is empty, and only as a RESPONSE field, so tenants
with rules pay nothing and the ``tools/list`` token ceiling is untouched.
"""

import inspect

import pytest

from core_api.constants import KEYSTONES_EMPTY_HINT

pytestmark = pytest.mark.unit


# ── the text ──────────────────────────────────────────────────────────────


def test_the_hint_names_the_trust_gated_step():
    """The load-bearing fact. Without it the caller learns only that the array
    was empty, which it could already see."""
    lowered = KEYSTONES_EMPTY_HINT.lower()
    assert "caura_keystones_set" in lowered
    assert "trust" in lowered


def test_the_hint_says_empty_is_not_failure():
    """The ambiguity that made an empty result actionable in the wrong
    direction."""
    assert "does not mean the call failed" in KEYSTONES_EMPTY_HINT.lower()


def test_the_hint_stays_short():
    """It rides on a response an agent reads every session. Length is a real
    cost, and a hint nobody finishes reading is not a hint."""
    assert len(KEYSTONES_EMPTY_HINT) <= 400


# ── one text, two surfaces ────────────────────────────────────────────────


def test_both_surfaces_use_the_same_constant():
    """REST and MCP describing the same state differently is how a contract
    drifts. Neither may inline its own copy."""
    from core_api import mcp_server
    from core_api.routes import keystones

    for mod in (mcp_server, keystones):
        src = inspect.getsource(mod)
        assert "KEYSTONES_EMPTY_HINT" in src, mod.__name__
        # the literal text must live in constants.py, not be re-typed here
        assert "No keystone rules are authored" not in src, mod.__name__


# ── when it fires ─────────────────────────────────────────────────────────


def test_mcp_emits_the_hint_only_when_empty():
    from core_api import mcp_server

    src = inspect.getsource(mcp_server.caura_keystones)
    assert "if not rows:" in src
    assert src.index("if not rows:") < src.index('payload["hint"]')


def test_rest_emits_the_hint_only_on_the_envelope():
    """The bare array stays the default shape. Adding a key to it would change
    the wire contract for every existing consumer — which is exactly what the
    C30/D1 opt-in was designed to avoid."""
    from core_api.routes import keystones

    src = inspect.getsource(keystones.list_keystones)
    assert "if envelope:" in src
    # the hint is set inside the envelope branch, and the bare `return rows`
    # that follows it is untouched
    assert src.index("if envelope:") < src.index('body["hint"]')
    assert src.rstrip().endswith("return rows")


def test_a_populated_set_carries_no_hint():
    """A tenant that has rules must pay nothing for this. Guarded structurally:
    the assignment sits under the empty check on both surfaces."""
    from core_api import mcp_server
    from core_api.routes import keystones

    mcp_src = inspect.getsource(mcp_server.caura_keystones)
    rest_src = inspect.getsource(keystones.list_keystones)
    # exactly one assignment each, both guarded
    assert mcp_src.count('payload["hint"]') == 1
    assert rest_src.count('body["hint"]') == 1
    assert "if not rows:" in mcp_src
    assert "if not rows:" in rest_src
