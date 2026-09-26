"""A41 — confirmation-gated recall boost (``recall_boost_source``), core-api side.

The loop being closed: TrackRecalls bumps ``recall_count`` for every RETURNED
row (used or not), and ``recall_count`` feeds ``recall_boost`` back into the
rank score — returned → boosted → returned, with no usefulness signal. A26/#411
could only dampen it (cap 1.5→1.1, window 90→14 days).

The fix wires the one genuine usefulness signal the platform already has —
evolve outcome reports, where an agent explicitly names the memories it acted
on in ``related_ids`` — into a second, confirmed-use counter
(``metadata._system.recall_used_count``), and adds a tenant-level knob,
``recall_boost_source``, that switches WHICH counter the boost reads:

* 0 (default): ``recall_count`` — today's behaviour, byte-identical SQL.
* 1: the confirmed-use counter — the boost then compounds only retrievals an
  agent actually reported acting on.

Both counters accrue regardless of the knob (returned = ``recall_count``,
used = the metadata counter), so the returned-vs-used comparison — the
evidence for flipping any default — is measurable BEFORE any tenant flips
(A50: every boost must earn its weight on the workload that needs it).

This file covers the core-api half: knob table wiring, resolution precedence,
org-settings validation, the env-var global default, and the evolve path
sending ``mark_used``. The storage half (statement shapes, the atomic counter
bump) is pinned in
``core-storage-api/tests/test_a41_recall_boost_source_statement.py`` and the
integration cases in ``tests/test_ph5b_evolve_storage.py``.

Pure logic — no DB.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import core_api.constants as core_constants

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Knob table + wire contract
# ---------------------------------------------------------------------------


def test_knob_is_declared_and_crosses_the_wire():
    """``sql=True`` and optional: storage reads it with ``sp.get(..., 0)``, so a
    payload from an older core-api (key absent) keeps today's behaviour."""
    from common.constants import (
        SEARCH_KNOBS,
        SQL_SCORING_PARAM_KEYS,
        SQL_SCORING_REQUIRED_KEYS,
    )

    knob = SEARCH_KNOBS["recall_boost_source"]
    assert knob.value_type is int
    assert knob.bounds == (0, 1)
    assert "recall_boost_source" in SQL_SCORING_PARAM_KEYS
    assert "recall_boost_source" not in SQL_SCORING_REQUIRED_KEYS


def test_knob_is_not_agent_tunable():
    """Tenant-level A/B knob, like ``score_formula``: flipping it reshuffles
    ranking for every caller in the tenant, so it stays off ``caura_tune`` and
    ``SearchProfileUpdate`` (the full pinned split lives in
    ``test_search_default_profile.py``)."""
    from common.constants import AGENT_TUNABLE_KEYS

    assert "recall_boost_source" not in AGENT_TUNABLE_KEYS


# ---------------------------------------------------------------------------
# Resolution: constant default → tenant default_profile override
# ---------------------------------------------------------------------------


def _resolve(tenant_config=None):
    from core_api.services.memory_service import resolve_search_params

    return resolve_search_params(None, query="q", top_k=5, tenant_config=tenant_config)


def test_resolves_to_zero_by_default_and_reaches_the_projection():
    """Default 0 = bump-on-return feeds the boost — today's behaviour — and the
    key is present in the resolved dict, because both search-path builders
    project ``search_params`` through ``SQL_SCORING_PARAM_KEYS`` with INDEXED
    access: a knob missing here would be a KeyError, not a silent default."""
    params = _resolve()
    assert params["recall_boost_source"] == 0


def test_tenant_default_profile_flips_it():
    from core_api.services.organization_settings import ResolvedConfig

    tc = ResolvedConfig({"search": {"default_profile": {"recall_boost_source": 1}}})
    assert _resolve(tenant_config=tc)["recall_boost_source"] == 1


def test_validate_default_profile_bounds_recall_boost_source():
    from core_api.services.organization_settings import _validate_default_search_profile

    _validate_default_search_profile(
        {"search": {"default_profile": {"recall_boost_source": 1}}}
    )
    _validate_default_search_profile(
        {"search": {"default_profile": {"recall_boost_source": 0}}}
    )
    with pytest.raises(ValueError, match="in \\[0, 1\\]"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"recall_boost_source": 2}}}
        )
    # bool is an int subclass; the validator must not let ``True`` through as 1.
    with pytest.raises(ValueError, match="must be int"):
        _validate_default_search_profile(
            {"search": {"default_profile": {"recall_boost_source": True}}}
        )


# ---------------------------------------------------------------------------
# Env-var global default (CAURA_RECALL_BOOST_SOURCE, #1441 defensive parse)
# ---------------------------------------------------------------------------


def _exec_constants_fresh() -> ModuleType:
    """Execute ``core_api/constants.py`` as a NEW module object.

    Import-time behaviour can only be exercised by running the module body
    again; a fresh, unregistered module leaves the real module — and every
    ``from``-import snapshot other modules hold — untouched (same technique as
    ``test_embedding_timeout_env_parse.py``).
    """
    path = Path(core_constants.__file__)
    spec = importlib.util.spec_from_file_location("_fresh_core_constants", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_env_unset_defaults_to_returned_source():
    env = {k: v for k, v in os.environ.items() if k != "CAURA_RECALL_BOOST_SOURCE"}
    with patch.dict(os.environ, env, clear=True):
        assert _exec_constants_fresh().RECALL_BOOST_SOURCE == 0


def test_env_one_flips_the_global_default():
    with patch.dict(os.environ, {"CAURA_RECALL_BOOST_SOURCE": "1"}):
        assert _exec_constants_fresh().RECALL_BOOST_SOURCE == 1


@pytest.mark.parametrize("bad", ["yes", "1.0", ""])
def test_env_garbage_falls_back_and_names_the_var(bad, capsys):
    """A misconfigured value must not crash import (#1441) — it falls back to
    the return-fed source with a stderr WARN naming the knob."""
    with patch.dict(os.environ, {"CAURA_RECALL_BOOST_SOURCE": bad}):
        assert _exec_constants_fresh().RECALL_BOOST_SOURCE == 0
    assert "CAURA_RECALL_BOOST_SOURCE" in capsys.readouterr().err


def test_env_other_ints_fail_closed_to_returned_source():
    """Anything other than 1 keeps the return-fed source — mirrors storage's
    own ``== 1`` read, so a typo can only reproduce today's behaviour."""
    with patch.dict(os.environ, {"CAURA_RECALL_BOOST_SOURCE": "5"}):
        assert _exec_constants_fresh().RECALL_BOOST_SOURCE == 0


# ---------------------------------------------------------------------------
# Evolve path: the confirmation signal is sent (mark_used)
# ---------------------------------------------------------------------------


def _stub_storage_client():
    sc = MagicMock()
    sc.evolve_apply_weights = AsyncMock(
        return_value={
            "adjustments": [
                {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "old_weight": 0.5,
                    "new_weight": 0.6,
                }
            ],
            "backfilled": False,
        }
    )
    return sc


@pytest.mark.parametrize("outcome_type", ["success", "failure", "partial"])
async def test_adjust_weights_marks_related_ids_used(outcome_type):
    """EVERY outcome type confirms USE: success, failure and partial all say
    the agent acted on the memory. Valence stays in ``weight`` (the delta this
    same call applies); use lives in the counter — mirroring click-through
    semantics, which also can't see how the click went."""
    from core_api.services.evolve_service import _adjust_weights

    sc = _stub_storage_client()
    with patch("core_api.clients.storage_client.get_storage_client", return_value=sc):
        skip, processed, _adjustments = await _adjust_weights(
            "t1",
            ["00000000-0000-0000-0000-000000000001"],
            outcome_type,
            "a1",
        )

    assert skip is None
    assert processed == ["00000000-0000-0000-0000-000000000001"]
    kwargs = sc.evolve_apply_weights.await_args.kwargs
    assert kwargs["mark_used"] is True


async def test_storage_client_sends_mark_used_on_the_wire():
    """The typed client must actually put ``mark_used`` in the POST body — an
    older storage server ignores the unknown key (deploy-skew safe), but a
    client that never sends it silently loses the signal."""
    from core_api.clients.storage_client import CoreStorageClient

    client = CoreStorageClient(
        base_url="http://storage.test",
        read_url="",
        http=MagicMock(),
        read_http=MagicMock(),
    )
    with patch.object(client, "_post", new=AsyncMock(return_value={})) as post:
        await client.evolve_apply_weights(
            tenant_id="t1",
            ids=["00000000-0000-0000-0000-000000000001"],
            delta=0.1,
            floor=0.0,
            cap=1.0,
            mark_used=True,
        )
    path, payload = post.await_args.args[0], post.await_args.args[1]
    assert path == "/evolve/apply-weights"
    assert payload["mark_used"] is True
