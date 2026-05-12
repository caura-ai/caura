"""Resolve which string in a doc's ``data`` payload gets embedded.

Single source of truth shared by the MCP ``memclaw_doc(op="write")``
handler and the REST ``POST /documents`` route. Keeping the rule in
one place prevents the two surfaces from drifting on what counts as
the embed source — they handle the resulting error in their own
native style (JSON envelope vs. HTTPException) but agree on the rule.

Contract:
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
"""

from __future__ import annotations

SKILLS_COLLECTION = "skills"


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
