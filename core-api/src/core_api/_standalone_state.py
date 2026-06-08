"""Shared standalone-mode state. Imported by both auth.py and standalone.py to avoid circular deps."""

# Module-level tenant_id set by init_standalone() at startup
standalone_tenant_id: str | None = None


def reset_standalone_state() -> None:
    """Reset the module-level standalone_tenant_id."""
    global standalone_tenant_id
    standalone_tenant_id = None
