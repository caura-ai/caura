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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core_api.services.usage_service import UsageCheckResult

logger = logging.getLogger(__name__)

# Type alias for the audit hook signature.
AuditHook = Callable[..., Awaitable[None]]
# Signature: (*, tenant_id, agent_id, action, resource_type, resource_id, detail) -> None

# Type alias for the usage-metering hook signature.
# Signature: (*, tenant_id, operation, count) -> UsageCheckResult | None
#
# ``type`` (PEP 695) rather than a plain assignment: it is evaluated lazily, so
# the ``usage_service`` import stays under TYPE_CHECKING and the cycle never
# forms. Worth it over ``Any`` — spelling the return type is what makes a
# platform-supplied implementation checkable at all.
type UsageHook = Callable[..., Awaitable[UsageCheckResult | None]]


@dataclass
class ServiceHooks:
    """Optional hooks injected by the platform layer at startup.

    When a hook is None, the corresponding operation is skipped.
    This enables the core engine to run without audit.
    """

    audit_log: AuditHook | None = None
    # METERING, not enforcement — the name is deliberate. See the
    # ``usage_service`` module docstring for why, and for the implementation
    # guidance (enqueue and return ``None``; report counters only if that costs
    # no round-trip — this hook runs on the write path).
    usage_meter: UsageHook | None = None


_hooks = ServiceHooks()


def configure_hooks(hooks: ServiceHooks) -> None:
    """Wire platform hooks. Called once at app startup."""
    global _hooks
    _hooks = hooks
    logger.info(
        "Service hooks configured: audit=%s usage=%s",
        hooks.audit_log is not None,
        hooks.usage_meter is not None,
    )


def get_hooks() -> ServiceHooks:
    """Get the current hooks instance."""
    return _hooks


def reset_hooks() -> None:
    """Reset to no-op hooks. Used in tests and OSS mode."""
    global _hooks
    _hooks = ServiceHooks()
