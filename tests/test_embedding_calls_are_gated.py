"""CI guard: no embed call outside ``common/embedding`` skips the gate.

``EMBEDDING_MAX_CONCURRENCY`` only bounds the embedding backend if every
caller actually passes through the semaphore in
``common.embedding._service``. The service-level entrypoints
(:func:`get_embedding`, :func:`get_embeddings_batch`,
:func:`get_query_embedding`) do that for you. A caller that instead holds an
``EmbeddingProvider`` — from ``get_platform_embedding()`` or
``get_embedding_provider()`` — and calls ``.embed()`` on it reaches the
backend having passed no cap at all, which is how core-worker's
deferred-embed consumer went unbounded until caura#830's follow-up.

This is a guard rather than a review note on purpose. The same class of
defect took six review cycles on #830, five of them finding a *different*
call site that had inherited the fault, and the lesson recorded there was to
make the mistake structurally impossible instead of auditing for it. A
reviewer cannot see that a new ``.embed()`` is ungated; this test can.

To satisfy it, wrap the call:

    embedding = await call_embedding_gated(
        lambda: provider.embed(text), background=True
    )

Exemptions: ``common/embedding`` itself (it *is* the gate, and providers
legitimately call their own ``embed`` internally — e.g. ``embed_query``
delegating to ``embed``), tests, and benchmark/diagnostic scripts, which
talk to an ``--embed-url`` directly by design and are not request-path code.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Provider-protocol methods that reach the backend.
_EMBED_METHODS = {"embed", "embed_batch", "embed_query"}
# The wrapper that applies the gate; anything lexically inside one of its
# calls is by definition gated.
_GATE_FUNC = "call_embedding_gated"

_EXEMPT_PARTS = {
    ".venv",
    "site-packages",
    "node_modules",
    "__pycache__",
    "tests",
    "test",
    "e2e",
    "scripts",
    "migrations",
    "clients",
}


def _iter_service_py():
    for p in REPO_ROOT.rglob("*.py"):
        rel = p.relative_to(REPO_ROOT)
        if set(rel.parts) & _EXEMPT_PARTS:
            continue
        # common/embedding is the gate's own home.
        if rel.parts[:2] == ("common", "embedding"):
            continue
        yield p


def _violations(path: pathlib.Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    # Every node lexically inside a call_embedding_gated(...) call is gated,
    # which covers the ``lambda: provider.embed(text)`` form.
    gated: set[int] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _GATE_FUNC
        ):
            gated.update(id(n) for n in ast.walk(node))

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or id(node) in gated:
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in _EMBED_METHODS:
            hits.append((node.lineno, f".{f.attr}()"))
    return hits


def test_every_embed_call_outside_common_embedding_is_gated():
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in _iter_service_py():
        v = _violations(path)
        if v:
            offenders[str(path.relative_to(REPO_ROOT))] = v
    assert not offenders, (
        "Ungated embed call found outside common/embedding. Route it through "
        "get_embedding/get_embeddings_batch/get_query_embedding, or wrap it in "
        "call_embedding_gated(lambda: ..., background=...). Offenders:\n"
        + "\n".join(f"  {f}: {hits}" for f, hits in sorted(offenders.items()))
    )
