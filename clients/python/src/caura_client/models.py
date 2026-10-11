"""Lightweight result types returned by the client.

These are thin, tolerant wrappers over the API JSON — the most common fields are
promoted to attributes, and the full payload is always available on ``.raw`` so
nothing is lost.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Memory:
    """A single memory, as returned by write and search."""

    id: str | None
    content: str
    title: str | None = None
    memory_type: str | None = None
    tenant_id: str | None = None
    agent_id: str | None = None
    weight: float | None = None
    similarity: float | None = None
    metadata: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Memory:
        return cls(
            id=data.get("id"),
            content=data.get("content", ""),
            title=data.get("title"),
            memory_type=data.get("memory_type"),
            tenant_id=data.get("tenant_id"),
            agent_id=data.get("agent_id"),
            weight=data.get("weight"),
            similarity=data.get("similarity"),
            metadata=data.get("metadata"),
            raw=data,
        )


class SearchResult(list[Memory]):
    """The memories ``search`` ranked, plus the response envelope around them.

    A ``list`` of ``Memory``, so code that iterates, indexes or compares the
    result works as before. The envelope rides along on attributes (L-94):

    - ``recall_tracked``: whether this search reinforced the memories it
      returned (``None`` when the server did not say).
    - ``diagnostic``: the retrieval trace a ``diagnostic=True`` search returns,
      else ``None``.
    - ``warnings``: coded caveats about the result set, such as a parameter the
      server ignored, else ``None``.
    - ``raw``: the whole response body.
    """

    def __init__(
        self,
        memories: Iterable[Memory] = (),
        *,
        recall_tracked: bool | None = None,
        diagnostic: dict[str, Any] | None = None,
        warnings: list[dict[str, Any]] | None = None,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(memories)
        self.recall_tracked = recall_tracked
        self.diagnostic = diagnostic
        self.warnings = warnings
        self.raw = raw if raw is not None else {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SearchResult:
        """Build from a ``POST /api/v1/search`` body."""
        return cls(
            (Memory.from_dict(m) for m in data.get("items") or []),
            recall_tracked=data.get("recall_tracked"),
            diagnostic=data.get("diagnostic"),
            warnings=data.get("warnings"),
            raw=data,
        )


@dataclass
class RecallResult:
    """The LLM-synthesized context brief returned by ``recall``."""

    summary: str | None
    supporting_memories: list[Memory]
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecallResult:
        """Build from a ``POST /api/v1/recall`` body.

        The wire key is ``memories``; the server aliases the identical list under
        ``items`` as well, for consumers written against ``/search``'s shape, so
        either is accepted.

        H-01: this used to read ``supporting_memories`` — a key the server has
        never emitted in any commit. It was invented in this SDK and mirrored into
        the TypeScript one, so every ``recall()`` returned an empty list while
        ``summary`` kept working, and the test mocked the invented shape so the
        suite stayed green against a broken contract.

        The ATTRIBUTE keeps the name ``supporting_memories``: that is published
        API and renaming it would break callers. Only the wire key was wrong.
        """
        raw = data.get("memories")
        if raw is None:
            raw = data.get("items")
        memories = [Memory.from_dict(m) for m in (raw or [])]
        return cls(summary=data.get("summary"), supporting_memories=memories, raw=data)
