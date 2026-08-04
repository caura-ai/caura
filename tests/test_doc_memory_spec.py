"""Unit tests for ``resolve_doc_memory`` — the doc -> memory derivation rule.

Pure function, no I/O. This is the single source of truth shared by the MCP
``memclaw_doc(op="write")`` handler and the REST ``POST /documents`` route, so
these tests pin the contract both surfaces depend on.

Covers:
- The four skip conditions (skills / system collection / no body / over cap).
- Content is the doc body VERBATIM — byte-for-byte, whitespace and markdown
  preserved. This is the whole point of the feature; a regression here silently
  degrades every downstream consumer.
- A doc with NO ``summary`` still mints (summary gates *doc* indexing, not
  memory minting).
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
    assert spec.content == "body text"
    assert spec.source_uri == "memclaw-doc://runbooks/pg-tuning"
    assert spec.metadata["doc_collection"] == "runbooks"
    assert spec.metadata["doc_id"] == "pg-tuning"


def test_content_is_byte_for_byte_verbatim():
    """No prefix, no wrapper, no summary header, no whitespace normalisation.

    The memory is a faithful copy of the doc body, not a rendering of it.
    """
    body = "# Runbook\n\n## Vacuum\n\n  - run VACUUM ANALYZE nightly\n\n\ttabbed\n"
    spec = resolve_doc_memory("runbooks", "pg", _data(content=body))
    assert spec is not None
    assert spec.content == body
    # Guard against a summary prefix creeping back in.
    assert not spec.content.startswith(_data()["summary"])


def test_summary_is_carried_in_metadata_not_content():
    spec = resolve_doc_memory("runbooks", "pg", _data(content="body"))
    assert spec is not None
    assert spec.metadata["doc_summary"] == _data()["summary"]
    assert "summary" not in spec.content


def test_body_field_fallback():
    """``data["body"]`` is accepted when ``data["content"]`` is absent."""
    spec = resolve_doc_memory("notes", "n1", {"body": "via body field"})
    assert spec is not None
    assert spec.content == "via body field"


def test_content_wins_over_body_when_both_present():
    spec = resolve_doc_memory(
        "notes", "n1", {"content": "primary", "body": "secondary"}
    )
    assert spec is not None
    assert spec.content == "primary"


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
        {"summary": "S"},  # no body key at all
        {"summary": "S", "content": ""},  # empty
        {"summary": "S", "content": "   \n\t "},  # whitespace only
        {"summary": "S", "content": 42},  # non-string
        {"summary": "S", "content": None},
        {"plan": "business", "seats": 40},  # bulk structured record
    ],
)
def test_skips_when_no_usable_body(data):
    assert resolve_doc_memory("customers", "acme", data) is None


def test_bulk_structured_record_is_the_blast_radius_limit():
    """A structured record carries no body, so dropping the summary gate does
    NOT turn every customer/config row into a memory."""
    assert (
        resolve_doc_memory("customers", "acme", {"plan": "business", "seats": 40})
        is None
    )


# ── Summary is NOT required ───────────────────────────────────────────────────


def test_mints_without_summary():
    """``summary`` gates *doc* indexing (resolve_embed_source), not minting.

    A summary-less doc is invisible to ``op=search`` but its body is still
    worth recalling, so the memory is minted regardless.
    """
    spec = resolve_doc_memory("notes", "n1", {"content": "no summary here"})
    assert spec is not None
    assert spec.content == "no summary here"
    assert "doc_summary" not in spec.metadata


@pytest.mark.parametrize("summary", ["", "   ", None, 42, {"nested": "dict"}])
def test_unusable_summary_still_mints(summary):
    spec = resolve_doc_memory("notes", "n1", {"summary": summary, "content": "body"})
    assert spec is not None
    assert "doc_summary" not in spec.metadata


# ── Size boundary ─────────────────────────────────────────────────────────────


def test_body_at_exactly_the_cap_mints():
    spec = resolve_doc_memory("notes", "n1", {"content": "y" * DOC_MEMORY_MAX_CHARS})
    assert spec is not None
    assert len(spec.content) == DOC_MEMORY_MAX_CHARS


def test_body_one_char_over_the_cap_skips():
    assert (
        resolve_doc_memory("notes", "n1", {"content": "y" * (DOC_MEMORY_MAX_CHARS + 1)})
        is None
    )


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
    """The doc write is already committed by the time this runs, so an
    undecidable input must be a skip — never an exception."""
    for data in ({}, {"content": []}, {"content": {"a": 1}}, {"summary": object()}):
        assert resolve_doc_memory("c", "d", data) is None  # type: ignore[arg-type]


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
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert (
            resolve_doc_memory(
                "notes", "big", {"content": "y" * (DOC_MEMORY_MAX_CHARS + 1)}
            )
            is None
        )

    recs = [r for r in caplog.records if r.name == _LOGGER]
    assert any(r.levelno == logging.INFO for r in recs)
    msg = " ".join(r.getMessage() for r in recs)
    assert "notes/big" in msg
    assert str(DOC_MEMORY_MAX_CHARS + 1) in msg  # the actual size
    assert str(DOC_MEMORY_MAX_CHARS) in msg  # the limit it exceeded


@pytest.mark.parametrize(
    ("collection", "data"),
    [
        ("skills", {"content": "body"}),
        ("_keystones", {"content": "body"}),
        ("customers", {"plan": "business"}),
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
