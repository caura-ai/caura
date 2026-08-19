"""Lightweight result types returned by the client.

These are thin, tolerant wrappers over the API JSON — the most common fields are
promoted to attributes, and the full payload is always available on ``.raw`` so
nothing is lost.
"""

from __future__ import annotations

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
