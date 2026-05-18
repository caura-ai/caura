import hashlib
import hmac
import logging

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from core_api.config import settings
from core_api.constants import API_KEY_HEADER
from core_api.db.session import set_current_tenant, set_readable_tenants

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

    Multi-tenant reads:
    An agent may be authorized to read from tenants beyond its home tenant.
    ``readable_tenant_ids`` is the full set the caller may read from (always
    includes ``tenant_id``). Writes are always scoped to ``tenant_id``.
    ``capabilities`` constrains the mutation gate; when not None, ``write``
    must be in the set for any mutating call to succeed. Cross-tenant
    credentials typically carry ``{read, write}`` capabilities; the
    "writes pin to home" semantics come from ``enforce_tenant`` on the
    write target — not from a structural absence of write capability.
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
        readable_tenant_ids: list[str] | None = None,
        capabilities: set[str] | None = None,
        # Back-compat alias — older callers still pass ``scopes``.
        scopes: set[str] | None = None,
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
        # Tenants this caller may READ from. Always non-empty when tenant_id
        # is set; equal to ``[tenant_id]`` for single-tenant keys.
        if readable_tenant_ids:
            self.readable_tenant_ids = list(readable_tenant_ids)
            if tenant_id and tenant_id not in self.readable_tenant_ids:
                self.readable_tenant_ids.insert(0, tenant_id)
        else:
            self.readable_tenant_ids = [tenant_id] if tenant_id else []
        # Capability set. None = full (legacy/admin keys). When a set
        # is provided, callers must pass ``write`` for any mutating
        # operation. ``scopes`` is accepted as a back-compat alias so
        # older AuthContext(scopes=...) callers keep working.
        self.capabilities = capabilities if capabilities is not None else scopes
        # Legacy alias retained as a read-only view so old code that
        # still reads ``ctx.scopes`` keeps functioning during the
        # deprecation window. Aliasing rather than dual storage prevents
        # the two from drifting apart.
        self.scopes = self.capabilities

    @property
    def is_cross_tenant_read(self) -> bool:
        """True if this auth context can read from more than its home tenant."""
        return len(self.readable_tenant_ids) > 1

    def source_tenants_for_audit(self) -> list[str]:
        """Return tenants whose data was widened into for this request,
        excluding the home tenant.

        Hook for the per-use cross-tenant-read audit event. When the
        memory/document/keystone read sweep lands (the follow-up to
        this PR), recall/search/list handlers should:

          1. After serving a result that includes rows from multiple
             tenants, count results per tenant.
          2. Call this method to get the set of source tenants
             touched.
          3. Emit one audit event per source tenant via the
             enterprise audit publisher (not yet wired in OSS — the
             ``common.events.audit_publisher`` module is
             enterprise-only). For OSS-direct deployments without an
             audit publisher, this is a no-op and the call is silently
             skipped.

        Format of the audit event (when it lands):
          action=cross_tenant_read
          tenant_id=<source tenant>          # logged TO this tenant
          detail={
            home_tenant_id: self.tenant_id,
            home_agent_id: self.agent_id,
            query_summary: <truncated query>,
            result_count_from_this_tenant: <int>,
          }
        """
        if not self.is_cross_tenant_read or not self.tenant_id:
            return []
        return [t for t in self.readable_tenant_ids if t != self.tenant_id]

    def enforce_read_only(self) -> None:
        """Raise 403 if the caller is not allowed to mutate state.

        Two unconditional read-only signals, both checked here so every
        write-shaped endpoint that already calls this gate is covered
        without needing per-site edits:

        - ``is_demo`` → demo sandbox is read-only.
        - ``capabilities`` is set and does NOT include ``write`` → the
          credential is read-only by construction (a credential minted
          with capabilities={'read'} — e.g., a viewer or reporting
          credential). Legacy credentials (capabilities=None) pass
          through unchanged.

        Usage-limit / plan-cap enforcement is intentionally separate
        (``enforce_usage_limits``) because the delete path is allowed
        to bypass usage-limit blocks; demo + capability blocks have no
        such carve-out.

        Note: a cross-tenant credential with ``capabilities={read,
        write}`` PASSES this gate — its restriction is "writes pin to
        home_tenant_id", which is enforced by ``enforce_tenant`` on
        the write target, not here.
        """
        if self.is_demo:
            raise HTTPException(status_code=403, detail="Demo sandbox is read-only.")
        if self.capabilities is not None and "write" not in self.capabilities:
            raise HTTPException(
                status_code=403,
                detail="This API key is read-only and cannot perform write operations.",
            )

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

    def enforce_readable_tenant(self, requested_tenant: str) -> None:
        """Raise 403 unless the caller may READ from the requested tenant.

        Use on read-shaped endpoints that accept an explicit tenant_id. For
        single-tenant keys this is equivalent to ``enforce_tenant``; for
        cross-tenant keys it permits any tenant in ``readable_tenant_ids``.
        """
        if self.is_admin:
            return
        if requested_tenant not in self.readable_tenant_ids:
            raise HTTPException(
                status_code=403,
                detail=f"API key is not authorized to read tenant '{requested_tenant}'",
            )

    def enforce_write_scope(self) -> None:
        """Raise 403 if this credential's capabilities exclude ``write``.

        No-op for credentials without an explicit capability set (legacy
        + admin paths keep working unchanged). Standalone helper for
        the niche case where a handler wants a finer-grained check than
        ``enforce_read_only`` (which also covers ``is_demo``).
        """
        if self.capabilities is None:
            return
        if "write" not in self.capabilities:
            raise HTTPException(
                status_code=403,
                detail="This API key is read-only and cannot perform write operations.",
            )


def _parse_csv_header(value: str | None) -> list[str]:
    """Parse a comma-separated header value into a stripped, non-empty list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


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
    # Multi-tenant read support: the gateway plumbs the set of tenants this
    # caller may read from. Absent header → single-tenant key (defaults to
    # [tenant_id] inside AuthContext). Present → AuthContext.readable_tenant_ids
    # widens to the union, while writes still target tenant_id.
    readable_tenants = _parse_csv_header(request.headers.get("x-readable-tenant-ids"))
    # Capabilities plumbed alongside readable tenants. Empty/absent →
    # None (full scope, legacy behavior). X-Capabilities is the
    # canonical header from the unified auth-api; X-Key-Scopes is
    # accepted as a back-compat alias during the gateway rollout
    # window so an old gateway running against a new core-api (or
    # vice versa) doesn't break auth.
    capability_list = _parse_csv_header(
        request.headers.get("x-capabilities") or request.headers.get("x-key-scopes")
    )
    capabilities: set[str] | None = set(capability_list) if capability_list else None

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
        # When the gateway plumbs a multi-tenant read set, expose it to the
        # DB layer so reads (and downstream RLS policies, when configured)
        # can widen. The home tenant is prepended to keep the set complete.
        if readable_tenants:
            combined: list[str] = [tenant_id]
            for t in readable_tenants:
                if t != tenant_id:
                    combined.append(t)
            set_readable_tenants(combined)
        else:
            set_readable_tenants(None)
        return AuthContext(
            tenant_id=tenant_id,
            agent_id=agent_id,
            is_read_only=is_read_only,
            is_install_credential=is_install_credential,
            install_uuid=install_uuid,
            readable_tenant_ids=readable_tenants or None,
            capabilities=capabilities,
        )

    # No tenant header + no admin key configured = reject.
    # Unscoped access without authentication is not allowed.
    raise HTTPException(
        status_code=401,
        detail="Missing API key or X-Tenant-ID header.",
    )
