"""Protocol definitions for pluggable LLM, embedding, and STM backends.

These protocols define the contracts that provider implementations must satisfy.
Using typing.Protocol enables structural subtyping — implementations do not need
to explicitly inherit from these classes, they just need matching signatures.

``StorageBackend``, ``JobQueue``, ``IdentityResolver`` and ``ConflictResolver``
used to sit here too, with ``SearchFilters``, ``Identity``, ``ConflictResult``
and ``Resolution`` as their supporting types. Each had exactly one
implementation, no config name selected it, and nothing outside
``tests/test_provider_protocols.py`` constructed one — so the protocols
described a plug-in system that had no socket. They were removed with those
implementations rather than left as an invitation; ``STMBackend`` stays because
``settings.stm_backend`` really does choose between two live backends.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

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

    async def post_note(self, tenant_id: str, agent_id: str, entry: dict[str, Any]) -> bool:
        """Append a note to an agent's private list.

        Implementations SHOULD cap the list length and apply TTL.

        Returns True only when the entry is STORED, and False when it was not
        — an unreachable backend included. Reads here degrade to an empty
        list, which is an honest answer to "what notes are there"; a write has
        no equivalent, and returning None either way is what let a dropped
        write reach the caller as a success.
        """
        ...

    async def clear_notes(self, tenant_id: str, agent_id: str) -> bool:
        """Delete all notes for an agent.

        Returns True only when the delete reached the backend. A clear is a
        MUTATION, so it owes the same receipt a write does — and its silent
        failure is the more alarming of the two: the caller is told the notes
        are gone, and they reappear on the next read.
        """
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
    ) -> bool:
        """Append an entry to the fleet bulletin board.

        Implementations SHOULD cap the bulletin length and evict
        oldest entries when the limit is reached.

        Returns True only when the entry is STORED — see ``post_note``.
        """
        ...

    async def clear_bulletin(self, tenant_id: str, fleet_id: str) -> bool:
        """Delete all entries from a fleet bulletin board.

        Returns True only when the delete reached the backend — see
        ``clear_notes``.
        """
        ...
