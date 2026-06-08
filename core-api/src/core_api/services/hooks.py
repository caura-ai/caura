"""
Service hooks — decouples core services from platform concerns.

In business mode, hooks are wired at startup to audit logging and recall
tracking. In OSS mode (hooks not configured), these operations are silently
skipped, allowing the core engine to run standalone.

Note: Trust enforcement (enforce_update) is access control and always runs
directly — it is not a hook.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Type aliases for hook signatures
AuditHook = Callable[..., Awaitable[None]]
# Signature: (db, *, tenant_id, agent_id, action, resource_type, resource_id, detail) -> None

RecallHook = Callable[["AsyncSession", list[UUID]], Awaitable[None]]
# Signature: (db, memory_ids) -> None


@dataclass
class ServiceHooks:
    """Optional hooks injected by the platform layer at startup.

    When a hook is None, the corresponding operation is skipped.
    This enables the core engine to run without audit or metering.
    """

    audit_log: AuditHook | None = None
    on_recall: RecallHook | None = None


_hooks = ServiceHooks()


def configure_hooks(hooks: ServiceHooks) -> None:
    """Wire platform hooks. Called once at app startup."""
    global _hooks
    _hooks = hooks
    logger.info(
        "Service hooks configured: audit=%s, recall=%s",
        hooks.audit_log is not None,
        hooks.on_recall is not None,
    )


def get_hooks() -> ServiceHooks:
    """Get the current hooks instance."""
    return _hooks


def reset_hooks() -> None:
    """Reset to no-op hooks. Used in tests and OSS mode."""
    global _hooks
    _hooks = ServiceHooks()
