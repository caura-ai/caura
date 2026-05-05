"""SoT registry invariants — structural lints over ``core_api.tools``.

Every assertion here is meant to make a design contract concrete so
future refactors can't silently break it:

- Every spec name has the ``memclaw_`` prefix.
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
    "memclaw_recall",
    "memclaw_write",
    "memclaw_manage",
    "memclaw_doc",
    "memclaw_list",
    "memclaw_entity_get",
    "memclaw_tune",
    "memclaw_insights",
    "memclaw_evolve",
    "memclaw_stats",
}

EXPECTED_PLACEHOLDERS: set[str] = set()


# memclaw_insights and memclaw_evolve landed in d6f212b as live MCP-only
# tools — implementation is real, but they stay plugin_exposed=False until
# REST endpoints land (the plugin dispatches via REST).


def test_all_specs_have_memclaw_prefix():
    from core_api.tools import REGISTRY

    for name in REGISTRY:
        assert name.startswith("memclaw_"), f"Spec name '{name}' lacks memclaw_ prefix"


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

    manage_ops = {op.name for op in REGISTRY["memclaw_manage"].ops}
    doc_ops = {op.name for op in REGISTRY["memclaw_doc"].ops}
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


def test_descriptions_no_leftover_tool_descriptions_import():
    """After Phase 4, no spec module should import TOOL_DESCRIPTIONS."""
    import pkgutil
    from pathlib import Path

    import core_api.tools as pkg

    tool_dir = Path(pkg.__file__).parent
    for info in pkgutil.iter_modules([str(tool_dir)]):
        if not info.name.startswith("memclaw_"):
            continue
        src = (tool_dir / f"{info.name}.py").read_text()
        assert "TOOL_DESCRIPTIONS" not in src, (
            f"{info.name}.py still imports TOOL_DESCRIPTIONS; "
            "descriptions should live inline in each spec module."
        )


def test_registry_size_matches_expected_surface():
    """Registry size must match the expected plugin-exposed + placeholder sets."""
    from core_api.tools import REGISTRY

    assert len(REGISTRY) == len(EXPECTED_PLUGIN_EXPOSED | EXPECTED_PLACEHOLDERS)


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
