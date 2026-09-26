"""09/02 M-42 — ingest minted server-reserved memory types from caller content.

`MEMORY_TYPES_WRITE` is the set a caller may CREATE: the full vocabulary minus
server-reserved types (`outcome`, `insight`, `rule` — "authored only by internal
flows and rejected at the write boundary") and classifier-deprecated ones
(`semantic`, `intention`, `commitment`, `cancellation`).

`ingest_commit` validated `suggested_type` against `MEMORY_TYPES` — all
fourteen — so those seven passed straight through to
`memory_type=fact.suggested_type`. Auto-chunk children therefore minted
reserved-type rows from caller content, which is the exact thing
`MEMORY_TYPES_WRITE` exists to prevent.

Worse, the extraction prompt *actively offered* them, and gave `outcome`
preferential instruction: "if you'd write 'X happened' ... use this (not
'fact')". The system was reliably producing values its own write boundary
forbids.
"""

import re
from pathlib import Path

import pytest

from common.enrichment.constants import (
    CLASSIFIER_DEPRECATED_MEMORY_TYPES,
    DEFAULT_MEMORY_TYPE,
    SERVER_RESERVED_MEMORY_TYPES,
)
from core_api.constants import MEMORY_TYPES, MEMORY_TYPES_WRITE
from core_api.services import ingest_service

pytestmark = pytest.mark.unit

_NON_WRITEABLE = sorted(
    set(SERVER_RESERVED_MEMORY_TYPES) | set(CLASSIFIER_DEPRECATED_MEMORY_TYPES)
)


def _prompt_block() -> str:
    """The prompt's memory_type section, read from source."""
    src = Path(ingest_service.__file__).read_text()
    return src[src.index("7. **memory_type.**") : src.index("## Quantity guidance")]


def _prompt_types() -> set[str]:
    """The memory_type vocabulary the extraction prompt offers the model."""
    block = _prompt_block()
    return {m.group(1) for m in re.finditer(r"^\s+- (\w+)\s+—", block, re.M)}


# ── the prompt must not ask for what the boundary forbids ────────────────


def test_the_prompt_offers_exactly_the_writeable_types():
    """Pinned as equality, not a subset: offering a forbidden type reliably
    produces it, and omitting a writeable one silently narrows extraction."""
    assert _prompt_types() == set(MEMORY_TYPES_WRITE)


@pytest.mark.parametrize("bad", _NON_WRITEABLE)
def test_the_prompt_no_longer_offers_a_non_writeable_type(bad):
    assert bad not in _prompt_types()


def test_outcome_is_not_recommended_over_fact():
    """The prompt used to say: 'if you'd write "X happened" ... use this (not
    "fact")' — steering the model to a server-reserved type by name."""
    assert "outcome" not in _prompt_block()


# ── and the boundary must not depend on the prompt behaving ──────────────


def test_commit_coerces_rather_than_rejecting_non_writeable_types():
    """Coercion is deliberate. The prompt itself produced these values, so a
    422 would reject the server's own prior output and break every preview
    generated before this fix that is still being round-tripped."""
    code = _commit_source()
    assert "MEMORY_TYPES_WRITE" in code
    assert "DEFAULT_MEMORY_TYPE" in code


def test_forged_types_outside_the_vocabulary_are_still_rejected():
    """The pre-existing 422 gate must survive. Coercion covers the band of
    VALID-but-not-writeable types; a slug outside the vocabulary entirely is
    malformed input and still gets a clean 422 rather than being silently
    turned into a fact."""
    code = _commit_source()
    assert "status_code=422" in code
    reject_at = code.index("status_code=422")
    coerce_at = code.index("MEMORY_TYPES_WRITE")
    assert reject_at < coerce_at, "the forged-input gate must run first"


def test_the_coercion_is_logged_with_the_offending_values():
    """A silently retyped memory is indistinguishable from one the model
    classified that way, so the log has to name what was changed."""
    code = _commit_source()
    idx = code.index("MEMORY_TYPES_WRITE")
    assert "logger.info" in code[idx : idx + 900]


def _commit_source() -> str:
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ingest_service.ingest_commit).lstrip())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


# ── the sets themselves ──────────────────────────────────────────────────


def test_the_writeable_set_really_excludes_reserved_and_deprecated():
    """Guards the premise. If MEMORY_TYPES_WRITE ever stopped excluding these,
    the coercion above would become a no-op without failing anything else."""
    for t in _NON_WRITEABLE:
        assert t not in MEMORY_TYPES_WRITE
        assert t in MEMORY_TYPES, f"{t} should still be a queryable historical type"


def test_the_default_is_itself_writeable():
    """Coercing to a type the boundary rejects would move the bug, not fix it."""
    assert DEFAULT_MEMORY_TYPE in MEMORY_TYPES_WRITE
