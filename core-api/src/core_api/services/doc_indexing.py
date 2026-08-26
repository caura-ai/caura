"""Doc-write derivation rules: what gets embedded, and what memory to mint.

Single source of truth shared by the MCP ``caura_doc(op="write")``
handler and the REST ``POST /documents`` route. Keeping the rules in
one place prevents the two surfaces from drifting — they handle the
resulting error in their own native style (JSON envelope vs.
HTTPException) but agree on the rules themselves.

Embed-source contract (``resolve_embed_source``):
- The only embeddable field is ``data["summary"]``.
- For ``collection == "skills"``, ``data["description"]`` is honored
  as a back-compat fallback so existing skill catalogs keep working
  without a migration. Server prefers ``summary`` when both are
  present.
- Skills writes MUST be indexed (catalog discoverability depends on
  it) — missing both fields raises.
- All other collections may omit a summary; the doc is then stored
  without an embedding (same shape as the old ``embed_field=None``
  path).

Doc-memory contract (``resolve_doc_memory``):
- A doc write also mints a memory carrying the document's data, because
  the two stores are not cross-searched: ``caura_recall`` never returns
  documents, and only ``data["summary"]`` is embedded on the document
  row. The minted memory is what makes the *content* reachable by
  meaning.
- CAURA-717: the memory carries the WHOLE ``data`` payload rendered as
  text, ``summary`` first — not one guessed field. See the note above
  ``_render_doc_data`` for why guessing could not be made correct.
- Independent of the embed rule: a doc with no ``summary`` is stored
  unindexed (invisible to ``op=search``) yet still mints a memory, so
  recall can reach the content even when doc-search cannot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime

from core_api.constants import DOC_MEMORY_MAX_CHARS, DOC_MEMORY_URI_SCHEME

logger = logging.getLogger(__name__)

SKILLS_COLLECTION = "skills"

# CAURA-717: there is deliberately no list of "body" field names any more.
#
# The previous rule looked for ``data["content"]`` then ``data["body"]`` and
# skipped the mint when neither was present. That was a guess against a store
# with NO schema: ``documents.data`` is free-form JSONB (``data: dict``, no
# required keys — even ``{}`` is a legal document), and the tool description
# says the body "lives wherever the caller puts it (e.g. data['content'])",
# offering ``content`` as an EXAMPLE rather than a contract.
#
# In production at eToro the only organic doc feed writes
# ``{url, date, title, source, summary, platform, decisions, action_items,
# participants, duration_minutes}`` — no ``content``, no ``body``. Every write
# returned 200 and silently minted nothing, so the feature could never fire for
# the one feed that had real content. Guessing field names cannot be made
# correct, only longer.
#
# The whole ``data`` payload is rendered instead — see ``_render_doc_data``.


class InvalidDocIndexingError(ValueError):
    """Raised when the caller's ``data`` violates the embed-source contract."""


def resolve_embed_source(collection: str, data: dict) -> str | None:
    """Return the string to embed for this doc, or ``None`` to skip indexing.

    Raises:
        InvalidDocIndexingError: when the contract is violated — e.g.,
            a skills write with neither ``summary`` nor ``description``,
            or a non-skills write that provides ``summary`` but with a
            non-string / empty-string value.
    """
    summary = data.get("summary")
    summary_ok = isinstance(summary, str) and summary.strip()

    if collection == SKILLS_COLLECTION:
        if summary_ok:
            return summary
        description = data.get("description")
        if isinstance(description, str) and description.strip():
            return description
        raise InvalidDocIndexingError(
            "collection='skills' requires data['summary'] (preferred) or "
            "data['description'] (back-compat) as a non-empty string."
        )

    if summary is None:
        return None
    if not summary_ok:
        raise InvalidDocIndexingError("data['summary'], when present, must be a non-empty string.")
    return summary


@dataclass(frozen=True)
class DocMemorySpec:
    """The memory to mint for one document write.

    ``content`` is the whole ``data`` payload rendered as text with ``summary``
    first. Lossless — every non-empty field is included, and string values pass
    through unmodified — but NOT byte-identical to any single field, because no
    single field reliably holds the body (see ``_render_doc_data``).
    """

    content: str
    source_uri: str
    metadata: dict


def _render_value(value: object) -> str:
    """One field's value as text. Strings pass through untouched."""
    if isinstance(value, str):
        return value
    # Lists / dicts / scalars: compact JSON keeps nested structure readable and
    # parseable without exploding into many lines. ``ensure_ascii=False`` so
    # non-ASCII content (names, non-English text) stays legible instead of
    # becoming \\uXXXX escapes — this text is read by an embedder and an LLM.
    # ``default=str`` so an unexpected non-serialisable value degrades to its
    # repr rather than raising, since this must never fail the doc write.
    return json.dumps(value, ensure_ascii=False, default=str)


def _is_empty(value: object) -> bool:
    """True for values that carry no information and should not be rendered.

    Empty fields are noise in an embedding and waste the 500-char prefix budget
    that ``caura_insights`` reads.

    Whitespace-only strings count as empty — ``"   \\n\\t"`` is not caught by
    ``value == ""`` and would otherwise render as a bare ``key:`` followed by
    blanks. This matches how ``summary`` is already tested (``summary.strip()``).

    Falsy-but-real values are deliberately NOT empty: ``0`` and ``False`` are
    meaningful field values (``duration_minutes: 0``, ``archived: false``), so
    this must not collapse into a plain truthiness check.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return value == [] or value == {}


def _render_doc_data(data: dict) -> str:
    """Render the whole document payload as text, ``summary`` first.

    Why summary first
    -----------------
    Downstream consumers read only a PREFIX of memory content: ``caura_insights``
    truncates to 500 chars (``sanitize_content``'s default). A raw
    ``json.dumps(data)`` spends that budget on whichever keys happen to come
    first — for eToro's meeting docs that is ``url`` / ``date`` / ``title`` /
    ``source``, roughly 250 chars of metadata, pushing the substance
    (``decisions``, ``action_items``) past the cut. Emitting ``summary`` first
    puts the most information-dense field inside every consumer's window.

    With no ``summary`` the remaining fields render in the caller's own
    insertion order — nothing is reordered or dropped, so the memory still
    carries the entire document, just without a guaranteed-useful prefix.

    Why ``key: value`` lines rather than JSON
    ----------------------------------------
    This text is embedded and fed to LLMs. Braces, brackets and quotes are
    tokens that carry no meaning for either, so they are pure cost. ``summary``
    is emitted as bare prose (no ``summary:`` label) so the prefix reads
    naturally to both.
    """
    parts: list[str] = []

    summary = data.get("summary")
    summary_emitted = isinstance(summary, str) and bool(summary.strip())
    if summary_emitted:
        parts.append(summary.strip())  # type: ignore[union-attr]

    for key, value in data.items():
        if key == "summary" and summary_emitted:
            continue  # already emitted as the bare prose prefix
        if _is_empty(value):
            continue
        parts.append(f"{key}: {_render_value(value)}")

    return "\n\n".join(parts)


def resolve_doc_memory(
    collection: str,
    doc_id: str,
    data: dict,
    *,
    updated_at: datetime | str | None = None,
) -> DocMemorySpec | None:
    """Return the memory spec for this doc write, or ``None`` to skip.

    Mirrors ``resolve_embed_source``'s role: ONE rule, both surfaces. Never
    raises — an undecidable input is a skip, because failing here must not fail
    the document write that triggered it.

    CAURA-717: the content is the WHOLE ``data`` payload rendered as text with
    ``summary`` first, not one guessed field, so there is no body-field
    requirement — a document mints whenever it carries any usable data.

    Skips when:
      * ``collection == "skills"`` — skills have their own discovery path
        (``op=search collection=skills``) plus a staged -> active approval
        lifecycle. Minting a memory would make an unapproved skill's body
        recallable, routing around that lifecycle.
      * ``collection`` starts with ``_`` — system-managed (e.g. ``_keystones``);
        storage rejects public writes to these anyway.
      * the render is empty — ``data`` is ``{}`` or holds only empty values.
      * the render is longer than ``DOC_MEMORY_MAX_CHARS`` — would not fit in a
        memory. Skipped rather than truncated: a truncated payload reads as
        complete and yields confidently wrong conclusions downstream.

    ``data["summary"]`` is NOT required. It gates *doc* indexing
    (``resolve_embed_source``), not minting — a summary-less doc is invisible to
    ``op=search`` but its content is still worth recalling; it just renders
    without the guaranteed-useful prefix. When present it is also copied to
    metadata for provenance.

    Two consequences of not guessing, both accepted:
      * bulk structured records (``{"plan": "business", "seats": 40}``) now mint,
        where the old body-field rule excluded them as a side effect. A sync that
        writes many small documents therefore produces one memory per document.
      * server-written collections are unaffected: ``ingest-sources``,
        ``interview_jobs`` and ``skills_rollback`` are written through
        ``sc.upsert_document`` directly and never reach this code path, so no
        ingested content is duplicated.

    Every skip is logged — they look identical from the outside, so a document
    with no memory otherwise gives an operator no clue which rule fired or
    whether the mint threw. Levels are split deliberately: the size skip is INFO
    (rare, surprising, and the operator probably wants that content recallable),
    the rest DEBUG (routine and high-volume).
    """
    if collection == SKILLS_COLLECTION or collection.startswith("_"):
        logger.debug(
            "doc_memory: no mint for %s/%s — collection is skills or system-managed",
            collection,
            doc_id,
        )
        return None

    body = _render_doc_data(data)
    if not body.strip():
        logger.debug(
            "doc_memory: no mint for %s/%s — data rendered empty (no usable fields)",
            collection,
            doc_id,
        )
        return None
    if len(body) > DOC_MEMORY_MAX_CHARS:
        # INFO: the doc IS stored and (with a summary) searchable, but its
        # content is unreachable by recall.
        logger.info(
            "doc_memory: no mint for %s/%s — rendered content is %d chars, over the "
            "%d limit (document stored; content not recallable)",
            collection,
            doc_id,
            len(body),
            DOC_MEMORY_MAX_CHARS,
        )
        return None

    metadata: dict = {"doc_collection": collection, "doc_id": doc_id}
    summary = data.get("summary")
    if isinstance(summary, str) and summary.strip():
        metadata["doc_summary"] = summary
    if updated_at is not None:
        metadata["doc_updated_at"] = (
            updated_at.isoformat() if isinstance(updated_at, datetime) else str(updated_at)
        )

    return DocMemorySpec(
        content=body,
        source_uri=f"{DOC_MEMORY_URI_SCHEME}://{collection}/{doc_id}",
        metadata=metadata,
    )
