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

    async def _fake(text, focus=None, tenant_config=None, breadcrumb=None):
        state.calls.append((text, focus))
        return list(state.return_facts)

    monkeypatch.setattr(ingest_service, "_chunk_content", _fake)
    return state


@pytest.fixture
def fake_tenant_config(monkeypatch):
    """Stub resolve_config + A2 cache lookup so preview doesn't need a real DB."""

    async def _fake(db, tenant_id):
        return SimpleNamespace(
            enrichment_provider="fake",
            enrichment_enabled=False,
            default_write_mode="fast",
        )

    async def _fake_no_cache(db, tenant_id, doc_hash):
        # A2: tests that don't care about caching get an unconditional miss.
        # The dedicated A2 tests below override this.
        return []

    monkeypatch.setattr(ingest_service, "resolve_config", _fake)
    monkeypatch.setattr(ingest_service, "_find_prior_ingest_by_doc_hash", _fake_no_cache)


# ---------------------------------------------------------------------------
# P1.2 — source_uri stamping
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_source_uri_stamped_text_input(fake_chunker, fake_tenant_config):
    """When the caller passes ``content`` (not url), every returned fact carries
    ``source_uri='text-input'``."""
    req = IngestRequest(tenant_id="t1", content="A meaningful sentence about distributed systems.")
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
async def test_existing_source_uri_in_fact_not_overwritten(fake_chunker, fake_tenant_config):
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
async def test_content_length_under_cap_no_truncated_flag(fake_chunker, fake_tenant_config):
    """Input well under cap → content_length = len(input), no truncated flag."""
    text = "A" * 5_000
    req = IngestRequest(tenant_id="t1", content=text)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["content_length"] == 5_000
    assert "truncated" not in resp
    assert "original_length" not in resp


@pytest.mark.unit
@pytest.mark.asyncio
async def test_large_content_now_fans_out_to_multiple_sections(fake_chunker, fake_tenant_config):
    """PR #7: the 50k-char truncate is gone. Large content goes through the
    block chunker which fans out to multiple per-section LLM calls. The
    response reports the full content_length and the section count."""
    # 80k chars of repeated paragraphs — the chunker should split into
    # multiple sections (paragraph-pack heuristic on the plain-text path).
    paragraph = (
        "This paragraph contains a useful fact about quartz oscillators. "
        "Quartz oscillators run at 32.768 kHz.\n\n"
    )
    full_text = paragraph * 800  # ~80k chars
    req = IngestRequest(tenant_id="t1", content=full_text)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["content_length"] == len(full_text)
    assert "truncated" not in resp, "post-PR#7 there's no truncate; response should not have the flag"
    assert resp.get("sections", 0) >= 2, "chunker should fan out an 80k-char doc"
    # _chunk_content called once per section
    assert len(fake_chunker.calls) == resp["sections"]


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
    assert fake_chunker.calls == [], f"_chunk_content was called for short-circuited input {content!r}"


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
async def test_whitespace_only_still_short_circuits_regardless_of_size(fake_chunker, fake_tenant_config):
    """Even a huge whitespace-only blob should hit the short-circuit, not the
    chunker. (Pre-PR#7 this test asserted truncated=True; post-PR#7 the response
    is just the plain skipped_reason since we no longer truncate.)"""
    content = " " * 60_000
    req = IngestRequest(tenant_id="t1", content=content)
    resp = await ingest_service.ingest_preview(db=None, request=req)

    assert resp["skipped_reason"] == "content_too_short"
    assert resp["chunk_ms"] == 0
    # No truncated/original_length post-PR#7 — those fields are gone.
    assert "truncated" not in resp
    assert "original_length" not in resp
    # And the chunker (LLM) was NOT called
    assert fake_chunker.calls == []


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
        assert not ingest_service._META_FACT_RE.search(body), f"Should NOT match: {body!r}"


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


# ---------------------------------------------------------------------------
# PR #5 — A1 salience floor + A5 short-fact + prompt tightening
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_salience_floor_drops_low_score_facts(monkeypatch):
    """Facts with salience below 0.5 are filtered out by the validator (A1)."""

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {
                    "content": "The Eiffel Tower stands 330 meters tall.",
                    "suggested_type": "fact",
                    "salience": 0.9,
                },
                {
                    "content": "Some boilerplate phrasing appears in the document.",
                    "suggested_type": "fact",
                    "salience": 0.2,  # below floor
                },
                {
                    "content": "Mercury orbits the Sun every 88 days.",
                    "suggested_type": "fact",
                    "salience": 0.7,
                },
                {
                    "content": "A trivial restatement of earlier material happens.",
                    "suggested_type": "fact",
                    "salience": 0.3,  # below floor
                },
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tc)

    assert len(facts) == 2
    assert all(f["salience"] >= ingest_service._SALIENCE_FLOOR for f in facts)
    contents = {f["content"] for f in facts}
    assert "The Eiffel Tower stands 330 meters tall." in contents
    assert "Mercury orbits the Sun every 88 days." in contents


@pytest.mark.unit
@pytest.mark.asyncio
async def test_salience_exactly_at_floor_is_kept(monkeypatch):
    """The threshold is strict less-than — 0.5 exactly survives."""

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {
                    "content": "A fact sitting exactly at the salience floor.",
                    "suggested_type": "fact",
                    "salience": 0.5,
                }
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tc)
    assert len(facts) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_salience_does_not_drop_fact_backward_compat(monkeypatch):
    """If the LLM (or an old prompt) omits salience, the fact passes through.
    Important during the prompt rollout window — don't accidentally drop
    everything because the model didn't include the new field yet."""

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {
                    "content": "A solid fact without salience scoring.",
                    "suggested_type": "fact",
                },
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tc)
    assert len(facts) == 1
    assert "salience" not in facts[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_salience_does_not_drop_fact(monkeypatch):
    """LLM occasionally emits garbage in the salience field (string, null).
    Parser fails gracefully and falls back to 'no salience filter applied'."""

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {
                    "content": "This fact carries a non-numeric salience field.",
                    "suggested_type": "fact",
                    "salience": "high",
                },
                {
                    "content": "This fact carries an explicit null salience value.",
                    "suggested_type": "fact",
                    "salience": None,
                },
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tc)
    assert len(facts) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_short_fact_filter_drops_sub_5_words(monkeypatch):
    """A5: facts with fewer than 5 words are dropped — likely headings/labels."""

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {"content": "Learn more", "suggested_type": "fact", "salience": 0.9},
                {"content": "Section 3", "suggested_type": "fact", "salience": 0.9},
                {
                    "content": "Iron melts at 1538 Celsius.",
                    "suggested_type": "fact",
                    "salience": 0.9,
                },
                {"content": "Yes", "suggested_type": "fact", "salience": 0.9},
                {
                    "content": "The user can pay via credit card or wire transfer.",
                    "suggested_type": "fact",
                    "salience": 0.9,
                },
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tc)

    contents = {f["content"] for f in facts}
    assert "Iron melts at 1538 Celsius." in contents
    assert "The user can pay via credit card or wire transfer." in contents
    assert "Learn more" not in contents
    assert "Section 3" not in contents
    assert "Yes" not in contents
    assert len(facts) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_three_filters_compose(monkeypatch, caplog):
    """Meta-fact + short + low-salience filters all apply to one batch.
    Confirms the validator counts each drop reason separately in the log."""
    import logging

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {
                    "content": "Mercury orbits the Sun every 88 days.",
                    "suggested_type": "fact",
                    "salience": 0.9,
                },
                {
                    "content": "The provided content consists of trivia.",
                    "suggested_type": "fact",
                    "salience": 0.9,
                },
                {"content": "Mars red", "suggested_type": "fact", "salience": 0.9},
                {
                    "content": "Some boilerplate phrasing appears here.",
                    "suggested_type": "fact",
                    "salience": 0.2,
                },
                {
                    "content": "Venus has the longest day in the solar system.",
                    "suggested_type": "fact",
                    "salience": 0.85,
                },
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    with caplog.at_level(logging.INFO, logger="core_api.services.ingest_service"):
        facts = await ingest_service._chunk_content("dummy", tenant_config=tc)

    assert len(facts) == 2
    assert "meta=1" in caplog.text
    assert "low_salience=1" in caplog.text
    assert "short=1" in caplog.text


@pytest.mark.unit
@pytest.mark.asyncio
async def test_salience_field_surfaces_on_returned_facts(monkeypatch):
    """Kept facts retain their salience score on the output. Callers
    (preview UI, agents) can sort/display/threshold further."""

    async def fake(*, primary_provider_name, call_fn, fake_fn, **kw):
        return {
            "facts": [
                {
                    "content": "Iron melts at 1538 Celsius.",
                    "suggested_type": "fact",
                    "salience": 0.85,
                }
            ]
        }

    monkeypatch.setattr(ingest_service, "call_with_fallback", fake)
    tc = SimpleNamespace(enrichment_provider="fake")
    facts = await ingest_service._chunk_content("dummy", tenant_config=tc)
    assert facts[0]["salience"] == 0.85
