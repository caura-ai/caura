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
        memories = [Memory.from_dict(m) for m in (data.get("supporting_memories") or [])]
        return cls(summary=data.get("summary"), supporting_memories=memories, raw=data)
