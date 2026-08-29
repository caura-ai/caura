"""Unit tests for ``resolve_doc_memory`` — the doc -> memory derivation rule.

Pure function, no I/O. This is the single source of truth shared by the MCP
``caura_doc(op="write")`` handler and the REST ``POST /documents`` route, so
these tests pin the contract both surfaces depend on.

Covers:
- The skip conditions (skills / system collection / empty render / over cap).
- CAURA-717: content is the WHOLE ``data`` payload rendered as text with
  ``summary`` FIRST — not a guessed body field. Summary-first is load-bearing:
  ``caura_insights`` reads only the first 500 chars, so whatever leads the render
  is what the reflection pass actually sees.
- The render is lossless: every non-empty field appears, string values unmodified.
- A doc with NO ``summary`` still mints — it just renders without the
  guaranteed-useful prefix.
- The ``DOC_MEMORY_MAX_CHARS`` boundary: at the cap mints, one over skips —
  never raises, so the document write it hangs off can never fail on size.
"""

from __future__ import annotations

import logging

import pytest

from core_api.constants import DOC_MEMORY_MAX_CHARS, MAX_CONTENT_LENGTH
from core_api.services.doc_indexing import DocMemorySpec, resolve_doc_memory

pytestmark = [pytest.mark.unit]


def _data(**extra) -> dict:
    d = {"summary": "Postgres tuning runbook: vacuum, autovacuum, work_mem."}
    d.update(extra)
    return d


# ── Happy path ────────────────────────────────────────────────────────────────


def test_mints_spec_for_ordinary_doc():
    spec = resolve_doc_memory("runbooks", "pg-tuning", _data(content="body text"))
    assert isinstance(spec, DocMemorySpec)
    assert "body text" in spec.content
    assert spec.source_uri == "caura-doc://runbooks/pg-tuning"
    assert spec.metadata["doc_collection"] == "runbooks"
    assert spec.metadata["doc_id"] == "pg-tuning"


def test_legacy_scheme_is_never_minted_but_stays_recognized():
    """Erni's decision on the doc-memory URI scheme: new mints use the Caura
    scheme; rows minted before the rename keep the old one in ``source_uri``
    forever (rule 2 — no data rewrite). Anything that ever grows a recognizer
    for doc-memory URIs must accept both, which this constant is the roster
    for. Pinned here so a tidy-up cannot delete the legacy scheme out from
    under data customers already hold.
    """
    from core_api.constants import DOC_MEMORY_URI_SCHEME, LEGACY_DOC_MEMORY_URI_SCHEMES

    assert DOC_MEMORY_URI_SCHEME == "caura-doc"
    assert "memclaw-doc" in LEGACY_DOC_MEMORY_URI_SCHEMES  # legacy-name-ok: compat pin
    # Minting must never use a legacy scheme.
    assert DOC_MEMORY_URI_SCHEME not in LEGACY_DOC_MEMORY_URI_SCHEMES


def test_summary_is_rendered_first_as_bare_prose():
    """Load-bearing: insights truncates to 500 chars, so the summary must lead.

    Emitted WITHOUT a ``summary:`` label so the prefix reads as natural prose to
    both the embedder and the LLM.
    """
    spec = resolve_doc_memory("runbooks", "pg", _data(content="body text"))
    assert spec is not None
    assert spec.content.startswith(_data()["summary"])
    assert not spec.content.startswith("summary:")


def test_render_is_lossless_and_keeps_string_values_intact():
    """Every non-empty field appears; string values are not reformatted."""
    body = "# Runbook\n\n  - run VACUUM ANALYZE nightly\n\ttabbed\n"
    spec = resolve_doc_memory(
        "runbooks", "pg", _data(content=body, owner="dba-team", priority=2)
    )
    assert spec is not None
    assert body in spec.content  # verbatim, whitespace preserved
    assert "owner: dba-team" in spec.content
    assert "priority: 2" in spec.content


def test_non_string_values_render_as_compact_json():
    spec = resolve_doc_memory(
        "meetings", "m1", _data(decisions=["ship it", "revisit Q4"], attendees={"a": 1})
    )
    assert spec is not None
    assert 'decisions: ["ship it", "revisit Q4"]' in spec.content
    assert 'attendees: {"a": 1}' in spec.content


def test_non_ascii_is_not_escaped():
    """This text is read by an LLM and by humans — \\uXXXX escapes would be noise."""
    spec = resolve_doc_memory("meetings", "m1", _data(participants=["Eyal", "ארקדי"]))
    assert spec is not None
    assert "ארקדי" in spec.content
    assert "\\u" not in spec.content


def test_empty_values_are_omitted_from_the_render():
    """Empty fields waste embedding tokens and prefix budget."""
    spec = resolve_doc_memory(
        "notes", "n1", _data(content="body", blank="", nothing=None, empty=[], void={})
    )
    assert spec is not None
    for key in ("blank:", "nothing:", "empty:", "void:"):
        assert key not in spec.content


@pytest.mark.parametrize("blank", ["   ", "\n", "\t", " \n\t "])
def test_whitespace_only_strings_count_as_empty(blank):
    """REGRESSION (CR): ``value == ""`` does not match ``"   "``, so a
    whitespace-only field rendered as a bare ``key:`` followed by blanks —
    exactly the noise the filter exists to remove."""
    spec = resolve_doc_memory("notes", "n1", _data(content="body", ws=blank))
    assert spec is not None
    assert "ws:" not in spec.content


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("duration_minutes", 0, "duration_minutes: 0"),
        ("archived", False, "archived: false"),
        ("score", 0.0, "score: 0.0"),
    ],
)
def test_falsy_but_real_values_are_kept(key, value, expected):
    """The empty check must not collapse into truthiness: ``0`` and ``False``
    are meaningful field values, not absent ones."""
    spec = resolve_doc_memory("notes", "n1", _data(**{key: value}))
    assert spec is not None
    assert expected in spec.content


def test_etoro_meeting_shape_mints_and_leads_with_substance():
    """REGRESSION (production, eToro): this exact shape has NO ``content`` or
    ``body`` key, so the old body-field rule skipped it and the feature could
    never fire for the only organic doc feed."""
    doc = {
        "url": "https://meet.example.com/rec/abc",
        "date": "2026-08-20",
        "title": "Product direction sync",
        "source": "zoom",
        "summary": "Team debated an SMB pivot toward organization-efficiency use cases.",
        "platform": "zoom",
        "decisions": ["pursue SMB segment"],
        "action_items": [{"owner": "Eyal", "task": "draft brief"}],
        "participants": ["Eyal", "Arkady"],
        "duration_minutes": 45,
    }
    spec = resolve_doc_memory("meeting-summaries", "m1", doc)
    assert spec is not None, "the eToro feed shape must mint"
    assert spec.content.startswith(doc["summary"])
    # The substance, not just the metadata, must survive into the memory.
    assert "pursue SMB segment" in spec.content
    assert "draft brief" in spec.content


def test_summary_is_carried_in_metadata_not_content():
    spec = resolve_doc_memory("runbooks", "pg", _data(content="body"))
    assert spec is not None
    assert spec.metadata["doc_summary"] == _data()["summary"]
    assert "summary" not in spec.content


def test_any_field_name_works_now():
    """No field name is privileged any more — the whole payload is rendered, so a
    doc using ``transcript`` / ``notes`` / anything else is no longer invisible."""
    for key in ("transcript", "notes", "markdown", "raw_text", "whatever"):
        spec = resolve_doc_memory("notes", "n1", {key: "the actual content"})
        assert spec is not None, f"data[{key!r}] must still mint"
        assert "the actual content" in spec.content


def test_updated_at_is_stamped_when_supplied():
    from datetime import UTC, datetime

    ts = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    spec = resolve_doc_memory("runbooks", "pg", _data(content="b"), updated_at=ts)
    assert spec is not None
    assert spec.metadata["doc_updated_at"] == ts.isoformat()


def test_updated_at_omitted_when_not_supplied():
    spec = resolve_doc_memory("runbooks", "pg", _data(content="b"))
    assert spec is not None
    assert "doc_updated_at" not in spec.metadata


# ── Skip conditions ───────────────────────────────────────────────────────────


def test_skips_skills_collection():
    """Skills have their own discovery path AND a staged -> active approval
    lifecycle. Minting would make an unapproved skill's body recallable,
    routing around that lifecycle."""
    assert resolve_doc_memory("skills", "my-skill", _data(content="body")) is None


@pytest.mark.parametrize("collection", ["_keystones", "_internal", "_"])
def test_skips_system_collections(collection):
    assert resolve_doc_memory(collection, "x", _data(content="body")) is None


@pytest.mark.parametrize(
    "data",
    [
        {},  # empty payload
        {"blank": ""},  # only empty values
        {"nothing": None},
        {"empty": [], "void": {}},
    ],
)
def test_skips_only_when_the_render_is_empty(data):
    """The only content-based skip left: nothing usable to render."""
    assert resolve_doc_memory("customers", "acme", data) is None


@pytest.mark.parametrize(
    "data",
    [
        {"summary": "S"},  # summary alone is enough
        {"summary": "S", "content": 42},  # non-string values still render
        {"plan": "business", "seats": 40},  # bulk structured record
    ],
)
def test_bodyless_docs_now_mint(data):
    """CAURA-717 behaviour change: the old rule skipped anything without a
    ``content``/``body`` key, which silently excluded eToro's entire doc feed.
    Structured records now mint too — the accepted cost of not guessing."""
    assert resolve_doc_memory("customers", "acme", data) is not None


def test_structured_record_renders_its_fields():
    spec = resolve_doc_memory("customers", "acme", {"plan": "business", "seats": 40})
    assert spec is not None
    assert "plan: business" in spec.content
    assert "seats: 40" in spec.content


# ── Summary is NOT required ───────────────────────────────────────────────────


def test_mints_without_summary():
    """``summary`` gates *doc* indexing (resolve_embed_source), not minting.

    A summary-less doc is invisible to ``op=search`` but its body is still
    worth recalling, so the memory is minted regardless.
    """
    spec = resolve_doc_memory("notes", "n1", {"content": "no summary here"})
    assert spec is not None
    assert "no summary here" in spec.content
    assert "doc_summary" not in spec.metadata


@pytest.mark.parametrize("summary", ["", "   ", None, 42, {"nested": "dict"}])
def test_unusable_summary_still_mints(summary):
    spec = resolve_doc_memory("notes", "n1", {"summary": summary, "content": "body"})
    assert spec is not None
    assert "doc_summary" not in spec.metadata


# ── Size boundary ─────────────────────────────────────────────────────────────


def test_render_at_exactly_the_cap_mints():
    """The cap applies to the RENDERED content, not to one field, so size it from
    the render: ``"content: " + body`` is 9 chars longer than the body."""
    body = "y" * (DOC_MEMORY_MAX_CHARS - len("content: "))
    spec = resolve_doc_memory("notes", "n1", {"content": body})
    assert spec is not None
    assert len(spec.content) == DOC_MEMORY_MAX_CHARS


def test_render_one_char_over_the_cap_skips():
    body = "y" * (DOC_MEMORY_MAX_CHARS - len("content: ") + 1)
    assert resolve_doc_memory("notes", "n1", {"content": body}) is None


def test_oversized_body_skips_rather_than_truncating():
    """Truncation is the failure mode we are avoiding: a truncated body reads as
    complete and produces confidently wrong downstream conclusions."""
    huge = "y" * (DOC_MEMORY_MAX_CHARS * 3)
    assert resolve_doc_memory("notes", "n1", {"content": huge}) is None


def test_cap_cannot_exceed_the_memory_schema_limit():
    """A spec longer than ``MemoryCreate.content``'s ``max_length`` would 422 the
    write, so the cap must never drift above it."""
    assert DOC_MEMORY_MAX_CHARS <= MAX_CONTENT_LENGTH


def test_never_raises_on_hostile_input():
    """The doc write is already committed by the time this runs, so nothing here
    may raise — an undecidable input either renders or skips, never throws.

    ``object()`` is not JSON-serialisable; ``_render_value``'s ``default=str``
    is what keeps it from blowing up.
    """
    for data in (
        {},
        {"content": []},
        {"content": {"a": 1}},
        {"summary": object()},
        {"weird": object()},
        {"nested": {"deep": [{"a": object()}]}},
    ):
        resolve_doc_memory("c", "d", data)  # type: ignore[arg-type]  # must not raise


# ── Every skip is logged ──────────────────────────────────────────────────────
#
# Found in the wet test on ``eyal-wet-tests``: a 17,660-char plan document was
# stored with no memory and NO log line at all. All four skip reasons — plus a
# mint that threw — look identical from the outside, so an operator had no way
# to tell which fired. These pin that each skip says so, at a level chosen for
# its volume.


_LOGGER = "core_api.services.doc_indexing"


def test_oversize_skip_logs_at_info(caplog):
    """INFO, not DEBUG: this is the surprising one. The doc is stored and
    searchable, but its body is silently unreachable by recall."""
    # Sized from the RENDER, not the field: ``"content: " + body`` is 9 chars
    # longer than the body, so the reported size is the rendered length.
    body = "y" * (DOC_MEMORY_MAX_CHARS - len("content: ") + 1)
    rendered_len = len("content: ") + len(body)
    assert rendered_len == DOC_MEMORY_MAX_CHARS + 1

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert resolve_doc_memory("notes", "big", {"content": body}) is None

    recs = [r for r in caplog.records if r.name == _LOGGER]
    assert any(r.levelno == logging.INFO for r in recs)
    msg = " ".join(r.getMessage() for r in recs)
    assert "notes/big" in msg
    assert str(rendered_len) in msg  # the actual rendered size
    assert str(DOC_MEMORY_MAX_CHARS) in msg  # the limit it exceeded


@pytest.mark.parametrize(
    ("collection", "data"),
    [
        ("skills", {"content": "body"}),
        ("_keystones", {"content": "body"}),
        ("customers", {}),  # empty render — the only content-based skip left
    ],
)
def test_routine_skips_log_at_debug_not_info(caplog, collection, data):
    """DEBUG deliberately: every bulk structured record hits the no-body branch,
    so INFO here would be pure noise on a high-volume doc sync."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert resolve_doc_memory(collection, "x", data) is None

    recs = [r for r in caplog.records if r.name == _LOGGER]
    assert recs, "a skip must never be silent"
    assert all(r.levelno == logging.DEBUG for r in recs)


def test_happy_path_logs_no_skip(caplog):
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert resolve_doc_memory("notes", "n1", {"content": "body"}) is not None

    assert not [r for r in caplog.records if r.name == _LOGGER]


# ── Type ──────────────────────────────────────────────────────────────────────


def test_there_is_no_doc_memory_type_constant():
    """Doc memories pass no ``memory_type`` — the classifier assigns one per
    document. A ``DOC_MEMORY_TYPE`` constant reappearing would signal that
    someone re-pinned it and flattened every document to a single type.

    (``reference`` was never an option either: it is not a MemoryType, and the
    ``memories_memory_type_check`` CHECK constraint from migration 013 rejects
    it.)
    """
    import core_api.constants as consts

    assert not hasattr(consts, "DOC_MEMORY_TYPE")
