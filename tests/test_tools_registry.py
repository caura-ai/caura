"""SoT registry invariants — structural lints over ``core_api.tools``.

Every assertion here is meant to make a design contract concrete so
future refactors can't silently break it:

- Every spec name has the ``caura_`` prefix.
- Trust values live in [0, 3].
- ``impl_status="reserved"`` implies ``plugin_exposed=False``.
- Every ``OpSpec.required_params`` references a real handler parameter.
- Every non-reserved spec has a real, callable async handler.
- No name collisions.
- The plugin-exposed set matches the expected v1.0 surface.
"""

from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


EXPECTED_PLUGIN_EXPOSED = {
    "caura_recall",
    "caura_write",
    "caura_manage",
    "caura_doc",
    "caura_list",
    "caura_entity_get",
    "caura_tune",
    "caura_insights",
    "caura_evolve",
    "caura_stats",
    # Read tool is plugin-exposed so the plugin can auto-inject keystone
    # rules at session start (CAURA-000). The write tool
    # (``caura_keystones_set``) is MCP-only — authoring is an
    # admin/governance path, not an agent path.
    "caura_keystones",
}

# MCP-only tools — not surfaced through the plugin. ``caura_keystones_set``
# is the write half of the keystone pair; admins/governance use it from
# REST/MCP, agents do not.
EXPECTED_MCP_ONLY = {
    "caura_keystones_set",
}

EXPECTED_PLACEHOLDERS: set[str] = set()


# caura_insights and caura_evolve landed in d6f212b as live MCP-only
# tools — implementation is real, but they stay plugin_exposed=False until
# REST endpoints land (the plugin dispatches via REST).


def test_all_specs_have_caura_prefix():
    from core_api.tools import REGISTRY

    for name in REGISTRY:
        assert name.startswith("caura_"), f"Spec name '{name}' lacks caura_ prefix"


def test_all_trust_values_in_0_3():
    from core_api.tools import REGISTRY

    for spec in REGISTRY.values():
        assert spec.trust_required in (0, 1, 2, 3), (
            f"{spec.name}: trust_required={spec.trust_required} out of range"
        )


def test_reserved_implies_not_plugin_exposed():
    from core_api.tools import REGISTRY

    for spec in REGISTRY.values():
        if spec.impl_status == "reserved":
            assert spec.plugin_exposed is False, (
                f"{spec.name}: reserved specs must set plugin_exposed=False"
            )


def test_all_non_reserved_specs_have_callable_handler():
    from core_api.tools import REGISTRY

    for spec in REGISTRY.values():
        if spec.impl_status == "reserved":
            # Placeholder stubs still need handlers — they return NOT_IMPLEMENTED.
            pass
        assert spec.handler is not None, f"{spec.name}: handler is None"
        assert inspect.iscoroutinefunction(spec.handler), (
            f"{spec.name}: handler must be an async function"
        )


def test_op_required_params_reference_real_handler_params():
    from core_api.tools import REGISTRY

    for spec in REGISTRY.values():
        if not spec.ops:
            continue
        sig = inspect.signature(spec.handler)
        param_names = set(sig.parameters.keys())
        for op in spec.ops:
            for required in op.required_params:
                assert required in param_names, (
                    f"{spec.name} op={op.name}: required_param '{required}' "
                    f"is not a parameter of the handler (has {sorted(param_names)})"
                )


def test_no_name_collisions():
    from core_api.tools import REGISTRY

    names = [spec.name for spec in REGISTRY.values()]
    assert len(names) == len(set(names))


def test_plugin_exposed_set_is_v1_surface():
    from core_api.tools import REGISTRY

    exposed = {name for name, spec in REGISTRY.items() if spec.plugin_exposed}
    assert exposed == EXPECTED_PLUGIN_EXPOSED


def test_placeholders_are_reserved_set():
    from core_api.tools import REGISTRY

    reserved = {
        name for name, spec in REGISTRY.items() if spec.impl_status == "reserved"
    }
    assert reserved == EXPECTED_PLACEHOLDERS


def test_op_dispatched_tools_have_expected_op_sets():
    from core_api.tools import REGISTRY

    manage_ops = {op.name for op in REGISTRY["caura_manage"].ops}
    doc_ops = {op.name for op in REGISTRY["caura_doc"].ops}
    assert manage_ops == {"read", "update", "transition", "delete"}
    assert doc_ops == {"write", "read", "query", "delete", "list_collections", "search"}


def test_placeholder_tools_declare_not_implemented_error_code():
    from core_api.tools import REGISTRY

    for name in EXPECTED_PLACEHOLDERS:
        spec = REGISTRY[name]
        assert "NOT_IMPLEMENTED" in spec.error_codes, (
            f"{name}: must advertise NOT_IMPLEMENTED in error_codes"
        )


def test_descriptions_are_non_empty():
    from core_api.tools import REGISTRY

    for spec in REGISTRY.values():
        assert isinstance(spec.description, str)
        assert spec.description.strip(), f"{spec.name}: description is empty"


def test_query_param_scope_tools_do_not_claim_a_default():
    """``caura_list`` / ``caura_stats`` must not call any ``scope`` value "the
    default" (#908).

    One description string is served to BOTH surfaces — it feeds
    ``/tool-descriptions`` and the generated ``plugin/tools.json`` — and only
    MCP defaults ``scope``. ``mcp_server.caura_list`` declares
    ``scope=... = "agent"``; ``GET /memories`` declares it
    ``Query(default=None)`` and consults the trust ladder only when the caller
    supplied one, so an omitted ``scope`` there takes its author filter from
    ``written_by ?? agent_id`` and skips ``require_trust``. Naming a default
    without naming the surface is therefore false for every plugin caller.

    ``caura_insights`` / ``caura_evolve`` are excluded on purpose: their
    ``scope`` arrives in a request body whose model really does
    ``Field(default="agent")``, so the claim is true on every surface there.
    Mirrors the plugin-side guard in ``plugin/src/tool-definitions.test.ts``.
    """
    from core_api.tools import REGISTRY

    for name in ("caura_list", "caura_stats"):
        description = REGISTRY[name].description
        assert "(default)" not in description, (
            f"{name}: description calls a scope value \"(default)\", but an "
            "omitted scope is not scope='agent' on the REST/plugin path. "
            "State the ladder per value and name the per-surface default."
        )


def test_descriptions_no_leftover_tool_descriptions_import():
    """After Phase 4, no spec module should import TOOL_DESCRIPTIONS."""
    import pkgutil
    from pathlib import Path

    import core_api.tools as pkg

    tool_dir = Path(pkg.__file__).parent
    # ``caura_`` is the canonical module prefix; there are no legacy-named
    # spec modules (the legacy tool names are dispatch aliases in
    # mcp_server, not modules), so this filter is the whole surface.
    spec_modules = [
        info.name
        for info in pkgutil.iter_modules([str(tool_dir)])
        if info.name.startswith("caura_")
    ]
    assert spec_modules, "no caura_* spec modules found — the guard went vacuous"
    for name in spec_modules:
        src = (tool_dir / f"{name}.py").read_text()
        assert "TOOL_DESCRIPTIONS" not in src, (
            f"{name}.py still imports TOOL_DESCRIPTIONS; "
            "descriptions should live inline in each spec module."
        )


def test_registry_size_matches_expected_surface():
    """Registry size must match the expected plugin-exposed + MCP-only +
    placeholder sets — the three disjoint categories that span the surface."""
    from core_api.tools import REGISTRY

    assert len(REGISTRY) == len(
        EXPECTED_PLUGIN_EXPOSED | EXPECTED_MCP_ONLY | EXPECTED_PLACEHOLDERS
    )


def test_mcp_only_tools_are_not_plugin_exposed():
    """Tools in EXPECTED_MCP_ONLY are present in the registry and stay off
    the plugin surface — locks the boundary between agent-facing and
    admin/governance tools."""
    from core_api.tools import REGISTRY

    for name in EXPECTED_MCP_ONLY:
        assert name in REGISTRY, f"{name}: expected MCP-only tool missing from REGISTRY"
        assert REGISTRY[name].plugin_exposed is False, (
            f"{name}: MCP-only tools must not be plugin-exposed"
        )


def test_every_spec_renders_to_descriptor_json():
    """``to_descriptor_json`` handles every spec without errors."""
    from core_api.tools import REGISTRY, to_descriptor_json

    for spec in REGISTRY.values():
        d = to_descriptor_json(spec)
        assert d["name"] == spec.name
        assert d["trust_required"] == spec.trust_required
        assert d["impl_status"] == spec.impl_status
        assert d["plugin_exposed"] == spec.plugin_exposed
        assert isinstance(d["params"], list)
        assert isinstance(d["ops"], list)
