"""ax-0917-h-06 — a bulk caller could not tell its writes weren't searchable yet.

`embedding_pending` is public API: `MemoryOut` documents it, `caura_write`'s
tool description tells agents to read it, and core-worker clears it when the
vector lands. The single-write path sets it in `write_memory_row`.

**The bulk path never did** — and `MemoryOut`'s own comment admitted it: *"the
bulk path sets neither flag — so a bulk caller cannot read pendingness off its
own write response."*

That's the wrong way round. Bulk is where the deferred window is **longest** —
thousands of rows queued behind one backfill — so it's the caller most in need
of the signal, and the only one without it.

An agent probe wrote a memory, searched ~2 minutes later, found other memories
but not its own, and had nothing in the write response to explain why. Until
the vector lands a row is reachable by FTS and by the non-semantic list, but
not by semantic similarity — so a paraphrase search comes back empty while an
exact-words search does not.
"""

import ast
import inspect

import pytest

pytestmark = pytest.mark.unit


def _bulk_code() -> str:
    """`create_memories_bulk` with comments and docstrings stripped.

    The comment explaining this fix names `embedding_pending` several times, so
    a raw-source search would pass on the prose alone.
    """
    from core_api.services import memory_service

    tree = ast.parse(inspect.getsource(memory_service.create_memories_bulk).lstrip())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


def test_the_bulk_path_flags_a_deferred_embedding():
    """The fix."""
    code = _bulk_code()
    assert "embedding_pending" in code


def test_the_flag_is_conditional_on_the_vector_being_absent():
    """Setting it unconditionally would be worse than not setting it — a
    strong-mode bulk item embeds inline and is immediately searchable, so
    flagging it pending would send callers to wait for nothing."""
    code = _bulk_code()
    idx = code.index("embedding_pending")
    window = code[max(0, idx - 200) : idx]
    assert "embeddings[i] is None" in window


def test_it_goes_through_the_system_namespace_like_every_other_writer():
    """C25: platform-written keys live under their own namespace. Writing the
    key directly into caller metadata would put it where a caller could forge
    it, and would diverge from how the single-write path sets the same flag."""
    code = _bulk_code()
    idx = code.index("embedding_pending")
    assert "set_system_value" in code[max(0, idx - 100) : idx + 60]


def test_the_single_write_path_still_sets_it_the_same_way():
    """The two paths must agree: a caller should not have to know which one
    ran to know what the flag means."""
    from core_api.pipeline.steps.write import write_memory_row

    src = inspect.getsource(write_memory_row)
    assert 'set_system_value(metadata, "embedding_pending", True)' in src


def test_the_documented_contract_no_longer_says_bulk_is_exempt():
    """`MemoryOut`'s comment told callers not to expect this flag from bulk.
    Leaving that in place next to the fix would keep teaching the old rule."""
    from core_api import schemas

    src = inspect.getsource(schemas)
    assert "the bulk path sets neither" not in src


def test_enrichment_pending_is_still_not_claimed_for_bulk():
    """Bulk enrichment defers unconditionally, so there is no inline case for
    that flag to distinguish — setting it would be noise, not signal."""
    code = _bulk_code()
    assert "enrichment_pending" not in code
