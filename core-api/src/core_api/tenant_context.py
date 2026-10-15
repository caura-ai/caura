"""Request-scoped tenant context (RLS identity).

Set by the auth middleware before any storage access; read by paths that need
the caller's home tenant. The cross-tenant readable set travels on
``AuthContext.readable_tenant_ids``: a context var for it was written on every
request and read by nothing (L-214). Lives outside ``db/``
because core-api no longer holds a DB session — all database access goes through
core-storage-api over HTTP (rule 6440b9a6).
"""

import contextvars

# Context variable for RLS: set by auth middleware before storage access.
_current_tenant_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_current_tenant_id", default=None
)


def set_current_tenant(tenant_id: str | None) -> None:
    """Set the tenant_id for RLS enforcement in the current request context."""
    _current_tenant_id.set(tenant_id)


def get_current_tenant() -> str | None:
    """Get the tenant_id for the current request context."""
    return _current_tenant_id.get()
