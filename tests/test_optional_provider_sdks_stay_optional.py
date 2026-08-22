"""A provider whose SDK is optional must still be importable without it.

``vertex.py`` and ``gemini.py`` defer their SDK imports into methods,
against this repo's top-level-imports rule. That is deliberate, and it is
load-bearing rather than stylistic:

* ``google-cloud-aiplatform`` is an EXTRA for core-worker
  (``vertex = [...]``), and only a hard dependency for core-api.
* ``google-genai`` does not appear in core-worker's ``pyproject.toml`` at
  ALL — not required, not optional. core-worker never has it.

So hoisting either import to module scope makes that provider module
unimportable in core-worker, and ``common/llm/_platform.py`` imports
``vertex`` inside a ``try`` on the strength of exactly this property.

A comment cannot stop that hoist; this can. Without the test, the rule
argues for the change and nothing argues back until something fails at
deploy time in the one install that lacks the package.

What SHOULD fail is calling the provider, not importing the module. An
install without Vertex configured should never touch either.
"""

from __future__ import annotations

import importlib
import sys

import pytest

# (module under test, the class it must still expose, SDK roots to hide)
CASES = [
    pytest.param(
        "common.llm.providers.vertex",
        "VertexLLMProvider",
        ("vertexai", "google.cloud.aiplatform"),
        id="vertex-without-aiplatform",
    ),
    pytest.param(
        "common.llm.providers.gemini",
        "GeminiLLMProvider",
        ("google.genai",),
        id="gemini-without-genai",
    ),
]


class _Blocked:
    """Meta-path finder that makes chosen packages look uninstalled.

    RAISING from ``find_spec`` is what makes this absence rather than a
    redirect. Falling through instead (the implicit ``None`` below, the
    finder protocol's "not mine") hands the name to the next finder on the
    path, which resolves it from site-packages — where these packages DO
    exist in the dev environment, so the test would prove nothing.
    """

    def __init__(self, roots: tuple[str, ...]) -> None:
        self._roots = roots

    def find_spec(self, name, path=None, target=None):
        if any(name == r or name.startswith(r + ".") for r in self._roots):
            raise ModuleNotFoundError(
                f"No module named {name!r} (simulated: extra not installed)"
            )


@pytest.mark.unit
@pytest.mark.parametrize(("module_name", "class_name", "sdk_roots"), CASES)
def test_module_imports_without_its_sdk(
    module_name, class_name, sdk_roots, monkeypatch
):
    # Evict the target and the SDKs so the import genuinely re-executes;
    # monkeypatch restores every entry it removed at teardown, so a later
    # test importing these normally is unaffected.
    for name in list(sys.modules):
        if name == module_name or any(
            name == r or name.startswith(r + ".") for r in sdk_roots
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "meta_path", [_Blocked(sdk_roots), *sys.meta_path])

    module = importlib.import_module(module_name)

    assert hasattr(module, class_name), (
        f"{module_name} imported but {class_name} is missing — the SDK import "
        "must be inside the methods that use it, not at module scope"
    )


@pytest.mark.unit
@pytest.mark.parametrize(("module_name", "class_name", "sdk_roots"), CASES)
def test_the_blocker_would_actually_catch_a_hoist(
    module_name, class_name, sdk_roots, monkeypatch
):
    """The control, so the test above cannot pass vacuously.

    If the blocker did not really hide these packages, the assertion above
    would hold no matter where the imports lived and the guard would be
    decorative.
    """
    monkeypatch.setattr(sys, "meta_path", [_Blocked(sdk_roots), *sys.meta_path])

    for root in sdk_roots:
        for name in list(sys.modules):
            if name == root or name.startswith(root + "."):
                monkeypatch.delitem(sys.modules, name, raising=False)
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(root)
