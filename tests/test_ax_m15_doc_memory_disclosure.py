"""ax-0917-m-15 — a document write mints a memory, and no caller was told.

`POST /documents` (and MCP `caura_doc op=write`) also mints a memory carrying
the document's `data`, so the body becomes reachable by `caura_recall`. That is
deliberate and load-bearing — the two stores are not cross-searched and only
`data["summary"]` is embedded on the document row, so without the mint a
document's body is findable by meaning nowhere (`services.doc_indexing`).

What was wrong is that no caller-facing surface said so. The route docstring —
which IS the OpenAPI description — read "Upsert a document. If collection+doc_id
exists, data is replaced." and stopped. The MCP tool description said the
opposite of the truth by implication: "Documents are NOT for unstructured
knowledge — use caura_write for memories." `docs/api-reference.md` said "Store
or update a structured JSON document". An agent probe wrote a document and then
found its own document's content coming back from a memory search it never
asked to populate.

Same shape as ax-0917-h-08 (`indexed`): the write path knew something the
caller could not see. The fix is disclosure only — no behaviour changed.

Two things the original finding got wrong, pinned here so the text does not
drift back to them:

* The mint is NOT gated on `data.summary`. `summary` gates *document* indexing
  (`resolve_embed_source`); a summary-less document is invisible to
  `op=search` and mints anyway. The trigger is every write whose `data`
  renders non-empty and fits, outside `skills` / `_`-prefixed collections.
* The memory is not a pointer. Since CAURA-717 it carries the whole `data`
  payload rendered as text (`DocMemorySpec.content`); only `source_uri` points.

The finding's other half — the mint landing under the pre-rename service-agent
id — is NOT covered here because it was already fixed: `DOC_INDEXER_AGENT_ID` is
`caura-doc-indexer` (#1676) and migration 051 (#1683) moves the pre-existing
rows. `tests/test_doc_memory_sync.py` pins that end, and does it through
`agent_ids.LEGACY_DOC_INDEXER_AGENT_ID` rather than a fresh literal — spelling
the old id out again here would mint exactly the debt the legacy-name ratchet
exists to stop.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Any of these, matched case-insensitively, counts as naming the side effect.
# A surface may word it its own way; what it may not do is stay silent.
_MINT_WORDS = ("mint", "minted", "mints")


def _mentions_mint(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _MINT_WORDS) and "memor" in lowered


# ── REST: the route docstring is the OpenAPI description ──────────────────


def test_the_write_route_description_states_the_mint():
    """``upsert_document``'s docstring is what OpenAPI serves for
    ``POST /documents`` — the only place a REST caller reads."""
    from core_api.routes import documents

    assert _mentions_mint(documents.upsert_document.__doc__ or "")


def test_the_write_route_description_says_summary_does_not_gate_it():
    """The trigger the finding misstated. A caller who reads "mints a memory"
    next to ``data["summary"]`` will assume the summary is the trigger; it is
    not, and that assumption is what makes the side effect surprising."""
    from core_api.routes import documents

    doc = (documents.upsert_document.__doc__ or "").lower()
    assert "independent of" in doc and "still mints" in doc


def test_the_delete_route_description_states_the_unmint():
    from core_api.routes import documents

    assert "un-mint" in (documents.delete_document.__doc__ or "").lower()


def test_the_route_docstrings_describe_code_that_is_still_there():
    """Disclosure that outlives the behaviour is worse than none. If the mint
    call leaves the write path, this test fails and the prose goes with it."""
    from core_api.routes import documents

    assert "safe_sync_doc_memory" in inspect.getsource(documents.upsert_document)
    assert "safe_unmint_doc_memory" in inspect.getsource(documents.delete_document)


# ── MCP: the tool description is the only text an agent caller sees ───────


def test_the_mcp_tool_description_states_the_mint():
    """``mcp_register`` publishes ``spec.description`` and nothing else, so a
    disclosure that lives only on the op specs never reaches an MCP client —
    which is exactly the caller that hit this."""
    from core_api.tools._registry import get_spec

    assert _mentions_mint(get_spec("caura_doc").description)


def test_the_mcp_write_op_description_states_the_mint():
    from core_api.tools._registry import get_spec

    (write_op,) = [op for op in get_spec("caura_doc").ops if op.name == "write"]
    assert _mentions_mint(write_op.description)


def test_the_mcp_delete_op_description_states_the_unmint():
    from core_api.tools._registry import get_spec

    (delete_op,) = [op for op in get_spec("caura_doc").ops if op.name == "delete"]
    assert "un-mint" in delete_op.description.lower()


# ── docs ──────────────────────────────────────────────────────────────────


def test_the_api_reference_row_states_the_mint():
    """A reader who never opens the OpenAPI schema gets the table instead."""
    rows = [
        line
        for line in Path(__file__)
        .parents[1]
        .joinpath("docs/api-reference.md")
        .read_text()
        .splitlines()
        if line.startswith("| `/documents` | POST |")
    ]
    assert len(rows) == 1
    assert _mentions_mint(rows[0])
