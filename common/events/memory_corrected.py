"""Typed payload for the ``<brand>.memory.corrected`` topic (CP-08, CP-09).

core-api publishes one of these after a person, or an agent at
``trust_level >= 3``, corrects a memory:

- ``content_edit``: ``PATCH /memories/{id}`` (or MCP ``caura_manage``
  op=update) changed the content;
- ``status``: the status moved to ``outdated``, ``cancelled`` or ``deleted``,
  over the status route, MCP op=transition, a status in an update, or a delete;
- ``unverify``: a ``human_verified`` memory had its verification withdrawn;
- ``conflict_resolve``: a reviewer decided a conflict
  (``PATCH /conflicts/{id}/resolve``).

Automatic supersede by the contradiction detector or the near-duplicate merge
is NOT a correction and publishes nothing.

``old_summary`` / ``new_summary`` carry the text before and after, each cut to
:data:`SUMMARY_MAX_CHARS`; for ``status`` and ``unverify`` they carry the old
and new status instead. This text rides on the event only: the audit entry
written alongside (``memory.corrected``) drops it, as every memory audit row
drops content (``audit_service.log_action``).

CONSUMER RULE: ``old_summary`` is the text that was wrong. A consumer may show
it only to an agent that already read the memory (``memory_exposure``), never
to an agent or person who did not, because the old text can be the very thing
the correction removed (a leaked secret, a wrong figure, a person's data).

The envelope is :class:`common.events.base.Event`; this is the shape of its
``payload`` dict.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

#: Characters kept of each summary. Enough for a notice to say what changed,
#: short enough that a long memory does not ride the bus in full.
SUMMARY_MAX_CHARS = 280

CorrectionKind = Literal["content_edit", "status", "unverify", "conflict_resolve"]


def summarize(text: str | None) -> str | None:
    """``text`` cut to :data:`SUMMARY_MAX_CHARS`, with an ellipsis when cut."""
    if text is None:
        return None
    text = str(text)
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    return text[: SUMMARY_MAX_CHARS - 1] + "…"


class CorrectionActor(BaseModel):
    """Who corrected the memory."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    # ``user`` (a signed-in person), ``agent``, or the credential kind when
    # neither is known (``admin_key``, ``tenant_key``, ``person``).
    kind: str
    # The user id or agent id; None for a key that names neither.
    id: str | None = None


class MemoryCorrected(BaseModel):
    """Announce that a person (or a trusted agent) corrected a memory."""

    # ``extra="ignore"``: a consumer schema that a later publisher may extend.
    model_config = ConfigDict(frozen=True, extra="ignore")

    memory_id: UUID
    tenant_id: str
    fleet_id: str | None = None
    actor: CorrectionActor
    kind: CorrectionKind
    corrected_at: datetime
    old_summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)
    new_summary: str | None = Field(default=None, max_length=SUMMARY_MAX_CHARS)
    # Kind-specific facts: ``old_status``/``new_status`` for ``status``;
    # ``conflict_id``, ``review_status``, ``resolution_action`` and
    # ``related_memory_id`` for ``conflict_resolve``.
    detail: dict = Field(default_factory=dict)
