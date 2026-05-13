import hashlib
import hmac
import logging

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from core_api.config import settings
from core_api.constants import API_KEY_HEADER
from core_api.db.session import set_current_tenant

logger = logging.getLogger(__name__)

api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def get_admin_key() -> str | None:
    """Return the configured admin key (prefers admin_api_key, falls back to legacy api_key)."""
    return settings.admin_api_key or settings.api_key


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AuthContext:
    """Holds the authenticated identity.

    OSS auth paths:
    1. Admin API key (ADMIN_API_KEY)      → is_admin=True, tenant_id=None
    2. MemClaw API key (MEMCLAW_API_KEY)  → gates all non-admin access when set
    3. Standalone mode                     → tenant_id from config, org_role="admin"
    4. X-Tenant-ID header (enterprise)    → tenant_id from header
    """

    def __init__(
        self,
        tenant_id: str | None,
        is_demo: bool = False,
        is_admin: bool = False,
        user_id: str | None = None,
        org_id: str | None = None,
        org_role: str | None = None,
        agent_id: str | None = None,
        is_read_only: bool = False,
        is_install_credential: bool = False,
        install_uuid: str | None = None,
    ):
        self.tenant_id = tenant_id
        self.is_demo = is_demo
        self.is_admin = is_admin
        self.user_id = user_id
        self.org_id = org_id
        self.org_role = org_role  # "admin" | "member" | None
        self.agent_id = agent_id  # enterprise: set from X-Agent-ID header
        # Set by the enterprise gateway when the org has exceeded plan limits
        # after a subscription cancellation. Blocks creates/updates but allows
        # deletes (so users can reduce usage) and reads.
        self.is_read_only = is_read_only
        # True when the gateway authenticated the call with a memclawd
        # install credential (mci_ prefix). Drives bulk-write relaxation
        # for broker-mode callers — they don't drive an
        # ``X-Bulk-Attempt-Id`` header and don't have an ``agent_id``
        # on the wire.
        self.is_install_credential = is_install_credential
        self.install_uuid = install_uuid

    def enforce_read_only(self) -> None:
        """Raise 403 if this is a demo key (read-only sandbox)."""
        if self.is_demo:
            raise HTTPException(status_code=403, detail="Demo sandbox is read-only.")

    def enforce_usage_limits(self) -> None:
        """Raise 403 if the org is over its plan limits (read-only mode).

        Use on create/update endpoints. Do NOT use on delete endpoints — users
        in read-only mode must be able to delete data to get back under limits.
        Demo mode (is_demo) is enforced separately via enforce_read_only().
        """
        if self.is_read_only:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Organization is in read-only mode: usage exceeds plan limits. "
                    "Upgrade your plan or delete data to restore write access."
                ),
            )

    def enforce_admin(self) -> None:
        """Raise 403 unless the caller is the system super admin."""
        if not self.is_admin:
            raise HTTPException(status_code=403, detail="Admin access required")

    def enforce_org_admin(self) -> None:
        """Raise 403 unless the caller is an org admin (or super admin)."""
        if self.is_admin:
            return
        if self.org_role != "admin":
            raise HTTPException(status_code=403, detail="Org admin access required")

    def enforce_tenant(self, requested_tenant: str) -> None:
        """Raise 403 if the request tenant doesn't match the key's tenant."""
        if self.is_admin:
            return  # super admin bypass
        if self.tenant_id != requested_tenant:
            raise HTTPException(
                status_code=403,
                detail=f"API key is not authorized for tenant '{requested_tenant}'",
            )


async def get_auth_context(
    request: Request,
    key: str | None = Security(api_key_header),
) -> AuthContext:
    admin_key = get_admin_key()
    # Enterprise gateway injects X-Agent-ID when caller uses an mca_ agent key.
    agent_id = request.headers.get("x-agent-id") or None
    # Enterprise gateway injects X-Org-Read-Only: true when the org has
    # exceeded plan limits after a subscription cancellation. In standalone
    # and OSS-direct paths the header is absent, so enforcement is a no-op.
    is_read_only = request.headers.get("x-org-read-only", "").lower() == "true"

    # ── Path 1: Admin API key ──
    if key and admin_key and hmac.compare_digest(key, admin_key):
        set_current_tenant(None)  # Admin — RLS bypass
        return AuthContext(tenant_id=None, is_admin=True)

    # ── Path 2: MEMCLAW_API_KEY gate (optional, for network-exposed OSS) ──
    mclaw_key = settings.memclaw_api_key
    if mclaw_key:
        if key and hmac.compare_digest(key, mclaw_key):
            # Valid memclaw key — resolve tenant from standalone or header
            if settings.is_standalone:
                from core_api.standalone import get_standalone_tenant_id

                tenant_id = get_standalone_tenant_id()
                set_current_tenant(tenant_id)
                return AuthContext(tenant_id=tenant_id, org_role="admin", agent_id=agent_id)
            tenant_id = request.headers.get("x-tenant-id")
            if tenant_id:
                set_current_tenant(tenant_id)
                return AuthContext(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    is_read_only=is_read_only,
                )
            set_current_tenant(None)
            return AuthContext(tenant_id=None, agent_id=agent_id)
        # Key configured but not provided or wrong — reject
        if not key:
            raise HTTPException(
                status_code=401,
                detail="Missing API key. Include X-API-Key header.",
            )
        raise HTTPException(status_code=401, detail="Invalid API key.")

    # ── Path 3: Standalone mode (no key required) ──
    if settings.is_standalone:
        from core_api.standalone import get_standalone_tenant_id

        tenant_id = get_standalone_tenant_id()
        set_current_tenant(tenant_id)
        return AuthContext(tenant_id=tenant_id, org_role="admin")

    # ── Path 4: X-Tenant-ID header (set by enterprise nginx / ingress) ──
    tenant_id = request.headers.get("x-tenant-id")
    if tenant_id:
        # The gateway's /_auth subrequest plumbs the api_key's ``kind``
        # so this layer can branch on credential provenance without
        # an extra DB hop. ``install_credential`` is what memclawd
        # uses; ``user_api_key`` (the default) is the dashboard /
        # SDK path.
        credential_kind = (request.headers.get("x-caura-credential-kind") or "").lower()
        is_install_credential = credential_kind == "install_credential"
        install_uuid = request.headers.get("x-install-uuid") or None
        set_current_tenant(tenant_id)
        return AuthContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_read_only=is_read_only,
            is_install_credential=is_install_credential,
            install_uuid=install_uuid,
        )

    # No tenant header + no admin key configured = reject.
    # Unscoped access without authentication is not allowed.
    raise HTTPException(
        status_code=401,
        detail="Missing API key or X-Tenant-ID header.",
    )
