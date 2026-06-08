"""Standalone mode helpers: single-tenant, auth-free local/on-prem usage."""

import logging

from core_api import _standalone_state

logger = logging.getLogger(__name__)

_DEFAULT_TENANT_ID = "default"


def init_standalone() -> str:
    """Initialise standalone mode with a fixed tenant id. Returns the tenant_id."""
    _standalone_state.standalone_tenant_id = _DEFAULT_TENANT_ID
    logger.info("[startup] Standalone mode initialised — tenant_id=%s", _DEFAULT_TENANT_ID)
    return _DEFAULT_TENANT_ID


def get_standalone_tenant_id() -> str:
    """Return the cached standalone tenant_id, or raise if not initialised."""
    tid = _standalone_state.standalone_tenant_id
    if tid is None:
        raise RuntimeError("Standalone mode not initialised. Call init_standalone() during startup.")
    return tid
