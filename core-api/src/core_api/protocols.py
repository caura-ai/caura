"""Protocol definitions for pluggable LLM, embedding, and infrastructure backends.

These protocols define the contracts that provider implementations must satisfy.
Using typing.Protocol enables structural subtyping — implementations do not need
to explicitly inherit from these classes, they just need matching signatures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

# ---------------------------------------------------------------------------
# LLM Provider — moved to common.llm.protocols (CAURA-595).
# Embedding Provider — moved to common.embedding.protocols (CAURA-594).
# Re-export here so legacy
# ``from core_api.protocols import LLMProvider, EmbeddingProvider``
# imports keep working without forcing every caller to update at once.
# ---------------------------------------------------------------------------
from common.embedding.protocols import EmbeddingProvider  # noqa: F401
from common.llm.protocols import LLMProvider  # noqa: F401

# ---------------------------------------------------------------------------
# Supporting types for infrastructure protocols
# ---------------------------------------------------------------------------


@dataclass
class SearchFilters:
    """Backend-agnostic search filter parameters."""

    tenant_id: str
    fleet_ids: list[str] | None = None
    agent_id: str | None = None
    memory_type: str | None = None
    status: str | None = None
    valid_at: datetime | None = None


@dataclass
class Identity:
    """Resolved caller identity."""

    tenant_id: str
    user_id: str | None = None
    org_id: str | None = None
    roles: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConflictResult:
    """A detected contradiction between two memories."""

    existing_memory_id: UUID
    new_memory_id: UUID | None = None
    reason: str = ""  # "rdf_conflict", "semantic_conflict"
    existing_content_preview: str = ""
    confidence: float = 0.0


@dataclass
class Resolution:
    """Outcome of conflict resolution."""

    action: str  # "keep_both", "supersede", "merge", "reject"
    winner_id: UUID | None = None
    loser_id: UUID | None = None
    explanation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Storage Backend
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Pluggable persistence backend (PostgreSQL, AlloyDB, SQLite, etc.)."""

    async def store(
        self,
        tenant_id: str,
        content: str,
        embedding: list[float] | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Persist a memory and return its ID (as string).

        The backend is responsible only for storage. Enrichment,
        embedding generation, and deduplication happen in the service
        layer before this method is called.
        """
        ...

    async def get(
        self,
        tenant_id: str,
        memory_id: str,
    ) -> dict[str, Any] | None:
        """Retrieve a single memory by ID, or ``None`` if not found.

        Returns a plain dict representation of the stored memory.
        Tenant scoping MUST be enforced by the implementation.
        """
        ...

    async def update(
        self,
        tenant_id: str,
        memory_id: str,
        fields: dict[str, Any],
    ) -> None:
        """Update specified fields on a memory.

        Only the keys present in *fields* are modified.
        """
        ...

    async def delete(
        self,
        tenant_id: str,
        memory_id: str,
    ) -> bool:
        """Soft- or hard-delete a memory. Return ``True`` if it existed."""
        ...

    async def search(
        self,
        query_embedding: list[float],
        query_text: str,
        filters: SearchFilters,
        *,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Vector + keyword hybrid search.

        Implementations decide the ranking strategy (cosine distance,
        BM25, freshness decay, etc.). Each result dict MUST include
        at minimum ``'id'`` and ``'content'`` keys, and SHOULD include
        a ``'similarity'`` score.
        """
        ...

    async def graph_traverse(
        self,
        tenant_id: str,
        entity_id: str,
        *,
        hops: int = 1,
    ) -> list[dict[str, Any]]:
        """Traverse the entity-relation graph and return connected memories.

        Returns a list of dicts, each representing a memory reachable
        from *entity_id* within *hops* relation edges.  Backends without
        graph support SHOULD return an empty list.
        """
        ...


# ---------------------------------------------------------------------------
# Job Queue
# ---------------------------------------------------------------------------


@runtime_checkable
class JobQueue(Protocol):
    """Pluggable async job/task queue (Redis, Cloud Tasks, in-memory, etc.).

    Cron-style scheduling moved to the dedicated ``core-operations``
    service in CAURA-655 — the queue is now purely a fire-and-forget
    primitive.
    """

    async def enqueue(
        self,
        func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        """Submit a callable for async execution and return a job ID.

        *func* is an async callable. The queue implementation decides
        whether to run it in-process (``asyncio.create_task``) or
        serialize and dispatch to a worker (arq, Cloud Tasks, etc.).
        """
        ...


# ---------------------------------------------------------------------------
# Identity Resolver
# ---------------------------------------------------------------------------


@runtime_checkable
class IdentityResolver(Protocol):
    """Pluggable identity resolution strategy."""

    async def resolve(self, context: dict[str, Any]) -> Identity:
        """Resolve the caller's identity from request context.

        *context* is a backend-agnostic dict that may contain headers,
        tokens, API keys, or other authentication material.  The OSS
        ``ConfigIdentity`` reads from ``memclaw.toml``; business
        implementations validate JWTs or API key databases.

        Raises
        ------
        PermissionError
            If the context cannot be resolved to a valid identity.
        """
        ...


# ---------------------------------------------------------------------------
# Conflict Resolver
# ---------------------------------------------------------------------------


@runtime_checkable
class ConflictResolver(Protocol):
    """Pluggable memory conflict/contradiction resolution strategy."""

    async def resolve(self, conflict: ConflictResult) -> Resolution:
        """Decide how to handle a detected memory contradiction.

        The OSS ``ManualResolver`` returns ``action='keep_both'`` and
        logs the conflict.  Business implementations may apply trust
        ranking, recency heuristics, or policy chains to decide a
        winner.
        """
        ...


# ---------------------------------------------------------------------------
# Short-Term Memory Backend
# ---------------------------------------------------------------------------


@runtime_checkable
class STMBackend(Protocol):
    """Pluggable short-term memory backend (Redis, in-memory, etc.).

    All methods are tenant-scoped.  Notes are per-agent private;
    bulletin boards are per-fleet shared.
    """

    async def get_notes(self, tenant_id: str, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Retrieve an agent's private notes (newest first).

        Returns an empty list if no notes exist for the agent.
        """
        ...

    async def post_note(self, tenant_id: str, agent_id: str, entry: dict[str, Any]) -> None:
        """Append a note to an agent's private list.

        Implementations SHOULD cap the list length and apply TTL.
        """
        ...

    async def clear_notes(self, tenant_id: str, agent_id: str) -> None:
        """Delete all notes for an agent."""
        ...

    async def get_bulletin(self, tenant_id: str, fleet_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Read the fleet bulletin board (shared short-term state).

        Returns entries ordered by recency (newest first).
        Returns an empty list if no bulletin exists.
        """
        ...

    async def post_bulletin(
        self,
        tenant_id: str,
        fleet_id: str,
        entry: dict[str, Any],
    ) -> None:
        """Append an entry to the fleet bulletin board.

        Implementations SHOULD cap the bulletin length and evict
        oldest entries when the limit is reached.
        """
        ...

    async def clear_bulletin(self, tenant_id: str, fleet_id: str) -> None:
        """Delete all entries from a fleet bulletin board."""
        ...
