"""
Service hooks — decouples core services from platform concerns.

In business mode, hooks are wired at startup to audit logging. In OSS mode
(hooks not configured), audit is silently skipped, allowing the core engine to
run standalone.

Note: Trust enforcement (enforce_update) is access control and always runs
directly — it is not a hook. Recall tracking is no longer a hook either: it
routes directly through the storage client (increment_recall) at each call site.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Type alias for the audit hook signature.
AuditHook = Callable[..., Awaitable[None]]
# Signature: (*, tenant_id, agent_id, action, resource_type, resource_id, detail) -> None


@dataclass
class ServiceHooks:
    """Optional hooks injected by the platform layer at startup.

    When a hook is None, the corresponding operation is skipped.
    This enables the core engine to run without audit.
    """

    audit_log: AuditHook | None = None


_hooks = ServiceHooks()


def configure_hooks(hooks: ServiceHooks) -> None:
    """Wire platform hooks. Called once at app startup."""
    global _hooks
    _hooks = hooks
    logger.info("Service hooks configured: audit=%s", hooks.audit_log is not None)


def get_hooks() -> ServiceHooks:
    """Get the current hooks instance."""
    return _hooks


def reset_hooks() -> None:
    """Reset to no-op hooks. Used in tests and OSS mode."""
    global _hooks
    _hooks = ServiceHooks()
