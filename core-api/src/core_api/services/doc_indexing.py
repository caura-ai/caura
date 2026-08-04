"""Doc-write derivation rules: what gets embedded, and what memory to mint.

Single source of truth shared by the MCP ``memclaw_doc(op="write")``
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
- A doc write also mints a memory carrying the document body verbatim,
  because the two stores are not cross-searched: ``memclaw_recall``
  never returns documents, and only ``data["summary"]`` is embedded on
  the document row. The minted memory is what makes the *body*
  reachable by meaning.
- Independent of the embed rule: a doc with no ``summary`` is stored
  unindexed (invisible to ``op=search``) yet still mints a memory, so
  recall can reach the body even when doc-search cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from core_api.constants import DOC_MEMORY_MAX_CHARS, DOC_MEMORY_URI_SCHEME

logger = logging.getLogger(__name__)

SKILLS_COLLECTION = "skills"

# Field names checked, in order, for the document body. ``content`` is the
# convention advertised in the ``memclaw_doc`` tool description ("The full body
# lives wherever the caller puts it (e.g. data['content'])"); ``body`` is
# accepted as the obvious synonym.
_BODY_FIELDS: tuple[str, ...] = ("content", "body")


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

    ``content`` is the document body VERBATIM — no prefix, no wrapper, no
    summary header. Whitespace and markdown structure are preserved exactly as
    the caller stored them, so the memory is a faithful copy of the doc rather
    than a rendering of it.
    """

    content: str
    source_uri: str
    metadata: dict


def _resolve_body(data: dict) -> str | None:
    """First non-empty string among ``_BODY_FIELDS``, else ``None``."""
    for field in _BODY_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value.strip():
            return value
    return None


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

    Skips when:
      * ``collection == "skills"`` — skills have their own discovery path
        (``op=search collection=skills``) plus a staged -> active approval
        lifecycle. Minting a memory would make an unapproved skill's body
        recallable, routing around that lifecycle.
      * ``collection`` starts with ``_`` — system-managed (e.g. ``_keystones``);
        storage rejects public writes to these anyway.
      * no non-empty body — nothing to carry. This is also what keeps bulk
        structured records (``{"plan": "business", "seats": 40}``) from minting
        memories: they have no body field.
      * body longer than ``DOC_MEMORY_MAX_CHARS`` — would not fit in a memory.
        Skipped rather than truncated: a truncated body reads as complete.

    ``data["summary"]`` is deliberately NOT required. It gates *doc* indexing
    (``resolve_embed_source``), not memory minting — a summary-less doc is
    invisible to ``op=search`` but its body is still worth recalling. When a
    summary is present it is carried in metadata for provenance.

    Every skip is logged, because all four look identical from the outside — a
    document with no memory gives an operator no clue which rule fired, or
    whether the mint threw instead. Levels are deliberately split: the size skip
    is INFO (rare, surprising, and the operator probably wants the body indexed),
    the rest are DEBUG (routine and high-volume — every bulk structured record
    hits the no-body branch, and logging those at INFO would be pure noise).
    """
    if collection == SKILLS_COLLECTION or collection.startswith("_"):
        logger.debug(
            "doc_memory: no mint for %s/%s — collection is skills or system-managed",
            collection,
            doc_id,
        )
        return None

    body = _resolve_body(data)
    if body is None:
        logger.debug(
            "doc_memory: no mint for %s/%s — no non-empty %s field",
            collection,
            doc_id,
            " / ".join(f"data[{f!r}]" for f in _BODY_FIELDS),
        )
        return None
    if len(body) > DOC_MEMORY_MAX_CHARS:
        # INFO: the doc IS stored and (with a summary) searchable, but its body
        # is unreachable by recall. Skipped rather than truncated — a truncated
        # body reads as complete and yields confidently wrong conclusions.
        logger.info(
            "doc_memory: no mint for %s/%s — body is %d chars, over the %d limit "
            "(document stored; body not recallable)",
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
