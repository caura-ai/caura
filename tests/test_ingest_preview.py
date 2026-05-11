"""Tests for ingest_preview validator + provenance changes in PR #3.

Covers:
- P1.2 ``source_uri`` stamping on every preview fact
- P2.1 ``content_length`` reflects post-truncate; ``truncated`` + ``original_length`` flags
- P2.3 Whitespace / too-short content short-circuit (no LLM)
- P2.4 Meta-fact regex filter inside ``_chunk_content``
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core_api.schemas import IngestRequest
from core_api.services import ingest_service


@pytest.fixture
def fake_chunker(monkeypatch):
    """Stand in for ``_chunk_content`` — record what was passed, return canned facts.

    Returns a SimpleNamespace with:
      - ``calls``: list[(text_arg, focus_arg)]
      - ``return_facts``: list[dict] — what to return on next call (configurable)
    """
    state = SimpleNamespace(
        calls=[],
        return_facts=[{"content": "default test fact", "suggested_type": "fact"}],
    )

    async def _fake(text, focus=None, tenant_config=None):
        state.calls.append((text, focus))
        return list(state.return_facts)

    monkeypatch.setattr(ingest_service, "_chunk_content", _fake)
    return state


@pytest.fixture
def fake_tenant_config(monkeypatch):
    """Stub resolve_config so preview doesn't need a real DB session."""

    async def _fake(db, tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    monkeypatch.setattr(ingest_service, "resolve_config", _fake)


# ---------------------------------------------------------------------------
# P1.2 — source_uri stamping
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_source_uri_stamped_text_input(fake_chunker, fake_tenant_config):
    """When the caller passes ``content`` (not url), every returned fact carries
    ``source_uri='text-input'``."""
    req = IngestRequest(
        tenant_id="t1", content="A meaningful sentence about distributed systems."
    )
    fake_chunker.return_facts = [
        {"content": "Fact A", "suggested_type": "fact"},
        {"content": "Fact B", "suggested_type": "decision"},
    ]
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert len(resp["facts"]) == 2
    for f in resp["facts"]:
        assert f["source_uri"] == "text-input"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_source_uri_stamped_url(fake_chunker, fake_tenant_config, monkeypatch):
    """When the caller passes ``url``, every fact carries that URL as ``source_uri``."""

    async def _stub_fetch(url):
        return "Fetched body content with enough characters to clear the short-circuit."

    monkeypatch.setattr(ingest_service, "_fetch_url_text", _stub_fetch)
    req = IngestRequest(tenant_id="t1", url="https://example.com/article")
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["facts"]
    for f in resp["facts"]:
        assert f["source_uri"] == "https://example.com/article"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_source_uri_in_fact_not_overwritten(
    fake_chunker, fake_tenant_config
):
    """``setdefault`` shouldn't clobber a source_uri the chunker explicitly set
    (defensive — current chunker doesn't, but the contract should hold)."""
    fake_chunker.return_facts = [
        {
            "content": "Fact with explicit provenance",
            "suggested_type": "fact",
            "source_uri": "custom://override",
        }
    ]
    req = IngestRequest(tenant_id="t1", content="A meaningful sentence here.")
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["facts"][0]["source_uri"] == "custom://override"


# ---------------------------------------------------------------------------
# P2.1 — content_length truth + truncated flag
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_length_under_cap_no_truncated_flag(
    fake_chunker, fake_tenant_config
):
    """Input well under cap → content_length = len(input), no truncated flag."""
    text = "A" * 5_000
    req = IngestRequest(tenant_id="t1", content=text)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["content_length"] == 5_000
    assert "truncated" not in resp
    assert "original_length" not in resp


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_length_over_cap_truncated_with_original(
    fake_chunker, fake_tenant_config
):
    """Input over cap → content_length = cap, truncated=True, original_length=full."""
    full_text = "X" * 80_000
    req = IngestRequest(tenant_id="t1", content=full_text)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["content_length"] == ingest_service._INGEST_MAX_CONTENT_CHARS  # 50_000
    assert resp["truncated"] is True
    assert resp["original_length"] == 80_000
    # And the chunker only got the truncated content
    text_passed, _ = fake_chunker.calls[-1]
    assert len(text_passed) == ingest_service._INGEST_MAX_CONTENT_CHARS


# ---------------------------------------------------------------------------
# P2.3 — short-circuit on whitespace / too-short content
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["", "    ", "  \n\n\t  ", "hi", "ok", "x" * 19],
)
async def test_short_circuit_skips_llm(fake_chunker, fake_tenant_config, content):
    """Whitespace-only or sub-MIN content returns immediately with skipped_reason."""
    # Need to bypass IngestRequest's own validation by giving SOMETHING non-None.
    # Empty string is allowed by Pydantic; shorter inputs hit the short-circuit.
    if not content:
        # empty string → IngestPreview's own 400 path, not the short-circuit
        return
    req = IngestRequest(tenant_id="t1", content=content)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["facts"] == []
    assert resp["chunk_ms"] == 0
    assert resp["skipped_reason"] == "content_too_short"
    # The LLM (fake_chunker) was NEVER called
    assert fake_chunker.calls == [], (
        f"_chunk_content was called for short-circuited input {content!r}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_circuit_threshold_boundary(fake_chunker, fake_tenant_config):
    """Exactly MIN chars → goes to LLM (boundary is < not <=)."""
    content = "x" * ingest_service._INGEST_MIN_CONTENT_CHARS  # exactly 20
    req = IngestRequest(tenant_id="t1", content=content)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert "skipped_reason" not in resp
    assert fake_chunker.calls, "Expected _chunk_content to be called at threshold"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_circuit_returns_truncated_flag_too(
    fake_chunker, fake_tenant_config
):
    """A pathological caller could submit truncatable + tiny-after-strip input.
    The skipped_reason response still surfaces the truncated/original_length flags."""
    # ~60k chars of all whitespace — meaningful pre-strip but useless to LLM
    content = " " * 60_000
    req = IngestRequest(tenant_id="t1", content=content)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["skipped_reason"] == "content_too_short"
    assert resp["truncated"] is True
    assert resp["original_length"] == 60_000


# ---------------------------------------------------------------------------
# P2.4 — meta-fact filter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_meta_fact_regex_drops_self_referential():
    """The regex matches obvious meta-fact patterns. Tested directly because
    _chunk_content's behavior is easier to verify on the raw filter."""
    drop_these = [
        "The provided content consists of the word 'hi'.",
        "The content begins with the greeting 'hi'.",
        "The content starts with the PDF header '%PDF-1.4'.",
        "The user text is short.",
        "This text is about climate change.",
        "This content describes a meeting.",
        "  The provided text says nothing.",  # leading whitespace ok
        "the input content is malformed",  # case insensitive
    ]
    for body in drop_these:
        assert ingest_service._META_FACT_RE.search(body), f"Should match: {body!r}"

    keep_these = [
        "The user can paste any text into the box.",  # 'the user' but not meta
        "The book costs €30.",
        "Sarah will write the migration script.",
        "The Eiffel Tower stands 330 meters tall.",
        # NB: "The content of the agreement was disputed in court." would be
        # ambiguous; with our current regex, "the content of" doesn't match
        # because it's not followed by begins/starts/consists/is. Verify.
        "The content of the agreement was disputed in court.",
    ]
    for body in keep_these:
        assert not ingest_service._META_FACT_RE.search(body), (
            f"Should NOT match: {body!r}"
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_chunk_content_filters_meta_facts(monkeypatch):
    """End-to-end: _chunk_content drops meta-fact items but keeps real ones."""

    async def fake_with_fallback(*, primary_provider_name, call_fn, fake_fn, **kw):
        # Return a mix of meta-facts and real facts
        return {
            "facts": [
                {
                    "content": "The Eiffel Tower stands 330 meters tall.",
                    "suggested_type": "fact",
                },
                {
                    "content": "The provided content consists of two paragraphs.",
                    "suggested_type": "fact",
                },
                {"content": "Iron melts at 1538 Celsius.", "suggested_type": "fact"},
                {
                    "content": "This document describes a project plan.",
                    "suggested_type": "fact",
                },
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake_with_fallback)
    tenant_config = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tenant_config)

    contents = [f["content"] for f in facts]
    assert "The Eiffel Tower stands 330 meters tall." in contents
    assert "Iron melts at 1538 Celsius." in contents
    # Meta-facts dropped
    assert all("The provided content" not in c for c in contents)
    assert all("This document describes" not in c for c in contents)
    assert len(facts) == 2
