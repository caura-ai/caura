"""reg-d17 — tenant-settings operability.

Two behaviours were reported as silently corrupting any flip-then-measure
workflow. Investigating them changed the task:

* **Cross-worker invalidation already ships.** ``update_settings`` publishes
  ``Org.SETTINGS_CHANGED``, ``consumer.register_consumers`` subscribes
  ``handle_org_settings_changed`` with ``broadcast=True``, and ``app`` calls
  that at startup (CAURA-571). What is true is narrower: the bus defaults to
  ``inprocess``, where a broadcast cannot cross processes, so on that backend —
  local dev included — siblings still wait out the TTL.

* **Resetting a setting already works**, via an explicit ``null``. It was simply
  undocumented, so the shape operators reach for first (``{}``) looks like a
  clear, silently merges nothing, and the flip appears not to have taken.

So the fix is to PIN and DOCUMENT the mechanism rather than build a second one.
These tests exist because an undocumented behaviour is one refactor away from
being an unintentional one.
"""

import inspect

import pytest

from common.organization_settings_merge import deep_merge
from core_api.services.organization_settings import (
    ResolvedConfig,
    _validate_leaf_types,
)

pytestmark = pytest.mark.unit


# ── the reset shape ───────────────────────────────────────────────────────


def test_null_is_accepted_by_validation():
    """``_validate_leaf_types`` skips ``None`` deliberately (``v is not None``).
    If that guard ever tightens to reject null, the only reset shape goes with
    it — hence this test rather than a comment."""
    _validate_leaf_types(
        {"search": {"recall_boost": None, "strict_fleet_scoping": None}}
    )


def test_null_returns_a_leaf_to_its_default():
    stored = {"search": {"recall_boost": False}}
    assert ResolvedConfig(tenant_settings=stored).recall_boost is False
    reset = deep_merge(stored, {"search": {"recall_boost": None}})
    assert ResolvedConfig(tenant_settings=reset).recall_boost is True  # the default


def test_null_returns_a_whole_section_to_its_default():
    stored = {"search": {"default_profile": {"score_formula": 1, "fts_weight": 0.4}}}
    reset = deep_merge(stored, {"search": {"default_profile": None}})
    assert reset["search"]["default_profile"] is None


def test_an_empty_dict_is_a_no_op_not_a_clear():
    """The documented trap. This is the shape an operator tries first, and it
    leaves every key in place — the flip appears not to have taken."""
    stored = {"search": {"default_profile": {"score_formula": 1, "fts_weight": 0.4}}}
    after = deep_merge(stored, {"search": {"default_profile": {}}})
    assert after["search"]["default_profile"] == {"score_formula": 1, "fts_weight": 0.4}


def test_omitting_a_key_keeps_it():
    """Merge semantics: omission means "don't touch", never "clear"."""
    stored = {"search": {"recall_boost": False, "graph_retrieval": False}}
    after = deep_merge(stored, {"search": {"recall_boost": True}})
    assert after["search"]["graph_retrieval"] is False


# ── the contract is written down where callers look ───────────────────────


def test_the_route_documents_the_reset_shape():
    """An undocumented reset is why this was filed as "can never be reset"."""
    from core_api.routes import settings as route

    doc = inspect.getdoc(route.update_tenant_settings) or ""
    assert "null" in doc
    assert "NO-OP" in doc or "no-op" in doc


def test_the_route_documents_propagation_rather_than_promising_immediacy():
    from core_api.routes import settings as route

    doc = inspect.getdoc(route.update_tenant_settings) or ""
    assert "SETTINGS_CHANGED" in doc
    assert "TTL" in doc


def test_the_service_documents_the_same_contract():
    """Two layers, one contract — the merge happens here, so the rule belongs
    here too."""
    from core_api.services.organization_settings import update_settings

    doc = inspect.getdoc(update_settings) or ""
    assert "null" in doc and "no-op" in doc


# ── the cross-worker path the row assumed was missing ─────────────────────


def test_the_settings_changed_broadcast_is_published_on_write():
    from core_api.services.organization_settings import update_settings

    src = inspect.getsource(update_settings)
    assert "Topics.Org.SETTINGS_CHANGED" in src
    assert "invalidate_cache(tenant_id)" in src


def test_a_subscriber_exists_and_is_registered_broadcast():
    """Publishing to nobody would look identical to not publishing at all."""
    from core_api import consumer

    src = inspect.getsource(consumer.register_consumers)
    assert "Topics.Org.SETTINGS_CHANGED" in src
    assert "broadcast=True" in src
    assert callable(consumer.handle_org_settings_changed)


def test_the_handler_evicts_the_local_cache():
    src = inspect.getsource(
        __import__("core_api.consumer", fromlist=["x"]).handle_org_settings_changed
    )
    assert "invalidate_cache" in src
