"""ax-0917-h-08 — `/documents/search` returned count:0 for a document that exists.

An agent probe created a document in collection `ax_probe`, queried it seconds
later using words from its own body, and got `{count: 0, results: [], items: []}`
with HTTP 200. Retried, same. It concluded search was broken.

Search was working. The document was never searchable.

`document_search` filters `embedding IS NOT NULL`, and a document is embedded
only when its write resolves an embed source — `data["summary"]`, or
`data["description"]` for skills (`resolve_embed_source`). A document written
without one is stored, readable by id, and **permanently invisible to search**.

That is correct by design. What was wrong is that nobody could tell: the write
said 200, the read by id worked, and the search returned a plain zero. The
write path even recorded `indexed` in its audit row — the caller was the one
party who could not see it.

Fixed at both ends: the write response now carries `indexed`, and a
zero-result search explains itself when the scope held unsearchable documents.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


# ── the write tells you up front ─────────────────────────────────────────


def test_the_write_response_exposes_whether_the_doc_is_searchable():
    from core_api.routes.documents import DocOut

    assert "indexed" in DocOut.model_fields


def test_indexed_defaults_to_none_not_false():
    """READ paths (GET, query, list) do not know a row's embedding state.
    Defaulting to False would assert something untrue on every read."""
    from core_api.routes.documents import DocOut

    assert DocOut.model_fields["indexed"].default is None


def test_the_write_route_sets_it_from_the_resolved_embedding():
    """The write route is the one place that knows for certain — it just
    resolved the embed source."""
    from core_api.routes import documents

    src = inspect.getsource(documents.upsert_document)
    assert "out.indexed = embedding is not None" in src


def test_the_audit_row_and_the_response_agree():
    """The audit already logged ``indexed``; response and audit must not be
    able to disagree about the same write."""
    from core_api.routes import documents

    src = inspect.getsource(documents.upsert_document)
    assert '"indexed": embedding is not None' in src
    assert "out.indexed = embedding is not None" in src


# ── a zero-result search explains itself ─────────────────────────────────


def test_a_zero_result_search_reports_unsearchable_documents():
    from core_api.routes import documents

    src = inspect.getsource(documents.search_documents)
    assert "unindexed_count" in src


def test_the_count_is_only_paid_for_when_there_are_no_hits():
    """On the normal path this must cost nothing — the number is only
    interesting when it explains a zero."""
    from core_api.routes import documents

    src = inspect.getsource(documents.search_documents)
    assert "if not items:" in src
    idx = src.index("if not items:")
    assert "count_unindexed_documents" in src[idx : idx + 600]


def test_a_failure_to_count_does_not_fail_the_search():
    """Diagnostics must never turn a successful empty search into an error —
    the caller still gets its correct zero."""
    from core_api.routes import documents

    src = inspect.getsource(documents.search_documents)
    idx = src.index("count_unindexed_documents")
    assert "except Exception:" in src[idx : idx + 500]


def test_the_note_names_the_field_that_makes_a_doc_searchable():
    """A count alone does not tell an agent what to DO. The remedy is to pass
    data.summary, so the message has to say so."""
    from core_api.routes import documents

    src = inspect.getsource(documents.search_documents)
    assert "data.summary" in src


# ── the storage counter matches the search's own scope ───────────────────


def test_the_counter_mirrors_the_search_predicates():
    """A count taken over a different scope than the search would answer a
    question the caller did not ask."""
    from core_storage_api.services.postgres_service import PostgresService

    count_src = inspect.getsource(PostgresService.document_count_unindexed)
    for predicate in ("tenant_pred", "Document.collection", "Document.fleet_id"):
        assert predicate in count_src


def test_the_counter_selects_exactly_the_rows_search_skips():
    """``document_search`` takes ``embedding IS NOT NULL``; this must take the
    complement, or the number does not explain the zero."""
    from core_storage_api.services.postgres_service import PostgresService

    assert "Document.embedding.is_(None)" in inspect.getsource(
        PostgresService.document_count_unindexed
    )
    assert "Document.embedding.is_not(None)" in inspect.getsource(
        PostgresService.document_search
    )


def test_the_counter_is_tenant_scoped():
    from core_storage_api.services.postgres_service import PostgresService

    src = inspect.getsource(PostgresService.document_count_unindexed)
    assert "readable_tenant_ids" in src
    assert "Document.tenant_id" in src
