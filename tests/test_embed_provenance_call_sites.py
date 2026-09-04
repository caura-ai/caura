"""CI guard: no ``_schedule_embed_or_reembed`` call omits ``content_hash``.

``content_hash`` is that shim's provenance argument, and omitting it fails
silently in a way no reviewer or runtime test on the changed path will show
you. The shim publishes ``EMBED_REQUESTED`` with ``content_hash=None``,
``handle_embed_request`` forwards the None to ``update_memory_embedding``, and
that writes the vector while deliberately leaving ``embedded_content_hash``
NULL — "unknown provenance is honest, a wrong hash is not". The row lands in
the ``unknown_provenance`` bucket, which is documented as meaning "written
before migration 037" and which no sweep targets: both embedding backfills
select ``embedding IS NULL``, and these rows have an embedding. So the row
leaves the staleness detector's reach permanently, with no error, no log and
no failed request.

That is not hypothetical. Five sibling call sites in
``_reembed_memories_bulk`` shared this omission and put 241 rows of one
tenant's data into that bucket before anyone noticed, measured 2026-09-04.

This is a guard rather than a review note for the same reason
``test_embedding_calls_are_gated.py`` is: the identical shape — one fault
inherited across sibling call sites — took six review cycles on caura#830,
five of them finding a *different* site. A reviewer cannot see that a new
call omits a keyword argument; this test can. It also covers the case the
runtime tests structurally cannot: a SIXTH call site added later that
bypasses the shared helper.

To satisfy it, pass the hash of the text being embedded:

    _schedule_embed_or_reembed(
        memory_id, content, tenant_id,
        content_hash=_content_hash(tenant_id, fleet_id, content),
    )

``_content_hash`` covers ``tenant:fleet:content``, so the fleet has to be the
one the row carries — a hash computed without it is present, wrong, and reads
downstream as verified freshness, which is worse than the NULL it replaced.

Exemptions: tests (which call the shim directly to exercise its defaults) and
the shim's own definition.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SHIM = "_schedule_embed_or_reembed"
_REQUIRED_KWARG = "content_hash"

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
        yield p


def _violations(path: pathlib.Path) -> list[int]:
    """Line numbers of ``_SHIM`` calls that do not pass ``_REQUIRED_KWARG``.

    ``**kwargs`` forwarding counts as passing: a caller that splats a dict may
    well carry the hash, and this guard cannot see inside it. That is a
    deliberate false-negative — the alternative is failing every legitimate
    forwarder, which would get the guard deleted rather than obeyed.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return []

    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name != _SHIM:
            continue
        passed = {kw.arg for kw in node.keywords}
        # ``kw.arg is None`` is ``**kwargs``; treat it as opaque-but-ok.
        if _REQUIRED_KWARG in passed or None in passed:
            continue
        hits.append(node.lineno)
    return hits


def test_schedule_embed_or_reembed_call_sites_pass_hash():
    offenders: dict[str, list[int]] = {}
    for path in _iter_service_py():
        v = _violations(path)
        if v:
            offenders[str(path.relative_to(REPO_ROOT))] = v
    assert not offenders, (
        f"{_SHIM} called without {_REQUIRED_KWARG}. The embedding will be written "
        "with NULL provenance, landing the row in unknown_provenance where no sweep "
        "will ever re-embed it — silently. Pass "
        "content_hash=_content_hash(tenant_id, fleet_id, content). Offenders:\n"
        + "\n".join(f"  {f}: lines {v}" for f, v in sorted(offenders.items()))
    )


def test_guard_detects_a_missing_hash():
    """The guard must actually fail on a bad call site.

    Without this, a mistake in ``_violations`` (a wrong node type, a renamed
    shim) makes the guard above pass unconditionally and report a clean tree
    forever — the same false-clean failure mode the guard itself exists to
    catch. Both directions are asserted against source that is never
    executed.
    """
    bad = ast.parse(f"{_SHIM}(mid, content, tenant_id, is_failure_fallback=True)")
    good = ast.parse(f"{_SHIM}(mid, content, tenant_id, {_REQUIRED_KWARG}=h)")
    splat = ast.parse(f"{_SHIM}(mid, content, tenant_id, **kw)")

    def _scan(tree):
        return [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == _SHIM
            and _REQUIRED_KWARG not in {kw.arg for kw in n.keywords}
            and None not in {kw.arg for kw in n.keywords}
        ]

    assert _scan(bad) == [1], "guard would not have caught the omission it exists for"
    assert _scan(good) == [], "guard would reject a correct call site"
    assert _scan(splat) == [], "guard would reject **kwargs forwarding"
