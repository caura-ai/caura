"""Request-scoped tenant context (RLS identity).

Set by the auth middleware before any storage access; read by paths that need
the caller's home tenant + cross-tenant readable set. Lives outside ``db/``
because core-api no longer holds a DB session — all database access goes through
core-storage-api over HTTP (rule 6440b9a6).
"""

import contextvars

# Context variable for RLS: set by auth middleware before storage access.
_current_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_tenant_id", default=None
)
# Additional readable tenants (writes still go to ``_current_tenant_id``).
# Set when the caller authenticated with a credential authorized for
# cross-tenant reads. The list always includes the home tenant_id when
# populated; an empty list means "single-tenant key, no widening".
_readable_tenant_ids: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "_readable_tenant_ids", default=None
)


def set_current_tenant(tenant_id: str | None) -> None:
    """Set the tenant_id for RLS enforcement in the current request context."""
    _current_tenant_id.set(tenant_id)


def get_current_tenant() -> str | None:
    """Get the tenant_id for the current request context."""
    return _current_tenant_id.get()


def set_readable_tenants(tenant_ids: list[str] | None) -> None:
    """Set the set of tenants the current caller may READ from.

    A single-tenant caller does not need to call this — reads default to
    ``_current_tenant_id`` only. Cross-tenant credentials populate this list so
    read paths can widen scope while writes remain pinned to the home tenant_id.
    """
    _readable_tenant_ids.set(list(tenant_ids) if tenant_ids else [])


def get_readable_tenants() -> list[str]:
    """Get the cross-tenant read set for the current request context."""
    return _readable_tenant_ids.get() or []
