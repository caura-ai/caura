"""09/02 M-45 — the ingest parent document's summary was never embedded.

`ingest_commit` built a summary of the batch and set `data["summary"]`, with
the comment "triggers embedding population in storage". It does not.

Storage's `document_upsert` takes `embedding` as an **opt-in caller parameter**
and computes nothing itself. The parent doc is written through
`sc.upsert_document` directly, so it never passes through the REST/MCP doc path
that calls `resolve_embed_source` and embeds — a fact `doc_indexing`'s own
docstring states ("server-written collections ... never reach this code path").

So the summary was stored as text and never indexed: the promised semantic
search over ingest batches could not work, for any batch, ever.
"""

import ast
import inspect

import pytest

from core_api.services import ingest_service

pytestmark = pytest.mark.unit


def _commit_code() -> str:
    """`_write_parent_ingest_document` source, docstrings/comments stripped.

    The fix is *described* in a comment that names the very symbols it adds, so
    a plain substring search would pass on the prose alone. Reads the helper
    rather than ``ingest_commit``: the parent-document write lives there, which
    I discovered by these tests failing against a fix that was already correct.
    """
    src = inspect.getsource(ingest_service._write_parent_ingest_document)
    tree = ast.parse(src.lstrip())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


def test_the_parent_summary_is_embedded():
    """The defect: a summary was computed, stored, and never indexed."""
    code = _commit_code()
    assert "get_embedding" in code


def test_the_embedding_is_attached_to_the_upserted_payload():
    """Computing a vector and not passing it would fix nothing — storage only
    stores what the caller hands it."""
    code = _commit_code()
    assert "payload['embedding']" in code or 'payload["embedding"]' in code


def test_it_embeds_the_summary_and_not_the_whole_payload():
    """The summary exists precisely because the raw batch is too large and too
    metadata-heavy to make a useful vector."""
    code = _commit_code()
    idx = code.index("get_embedding")
    call = code[idx : idx + 120]
    assert "summary" in call


def test_the_embedding_runs_on_the_background_budget():
    """The REST doc route uses ``background=False`` because a client blocks on
    it and gets a 502 when the vector is missing, so it must not sit on the
    reduced deferred budget. Nobody waits on this one."""
    code = _commit_code()
    idx = code.index("get_embedding")
    assert "background=True" in code[idx : idx + 160]


def test_an_embedding_failure_does_not_fail_the_ingest():
    """The ingest has already committed by this point. Losing the batch over
    its index entry would trade a missing search result for lost memories, and
    the parent write is explicitly best-effort."""
    code = _commit_code()
    idx = code.index("get_embedding")
    window = code[max(0, idx - 400) : idx + 400]
    assert "try:" in window and "except" in window


def test_the_false_comment_is_gone():
    """The original line asserted storage would do the embedding. Leaving that
    claim in place next to the real fix would re-teach the wrong model."""
    src = inspect.getsource(ingest_service.ingest_commit)
    assert "triggers embedding population in storage" not in src


def test_only_the_xmax_endpoint_can_carry_a_vector():
    """The detail that made the first version of this fix silently useless.

    ``document_upsert`` has NO ``embedding`` parameter — only
    ``document_upsert_returning_xmax`` does. Attaching a vector to the payload
    and posting it to ``POST /documents`` drops it on the floor, with a 200 and
    no warning. ``routes/documents.py`` already branches for exactly this.
    """
    from core_storage_api.services.postgres_service import PostgresService

    plain = inspect.signature(PostgresService.document_upsert).parameters
    xmax = inspect.signature(PostgresService.document_upsert_returning_xmax).parameters
    assert "embedding" not in plain, (
        "document_upsert grew an embedding parameter — the endpoint switch in "
        "_write_parent_ingest_document can be simplified"
    )
    assert "embedding" in xmax and xmax["embedding"].default is None


def test_the_vector_is_sent_to_the_endpoint_that_accepts_it():
    """A vector posted to the plain endpoint is silently discarded."""
    code = _commit_code()
    assert "upsert_document_xmax" in code
    idx = code.index("upsert_document_xmax")
    assert "embedding" in code[max(0, idx - 200) : idx]
