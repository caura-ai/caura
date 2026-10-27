"""TLS policy for the Postgres connections this service opens.

Deliberately a module of its own, importable with no service configuration at
all: it constructs no ``Settings`` and imports nothing that does. ``config.py``
supplies the configured flag; ``scripts/preflight_012.py`` reaches the same
policy through the environment when service config cannot be built in the
invoking shell, which is precisely the case its ``--dsn`` flag exists for.
Keeping the policy here is what lets both do that without a second copy of it.
"""

from __future__ import annotations

import logging
import os
import ssl
from urllib.parse import parse_qs, urlsplit

# asyncpg's TLS modes, weakest first. ``allow`` and ``prefer`` will silently
# fall back to cleartext, so neither satisfies a setting named "require".
TLS_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")

_TRUTHY = {"1", "true", "yes", "on"}
_DEPLOYED_ENVIRONMENTS = {"production", "sandbox"}


def is_deployed_environment(environment: str) -> bool:
    """The environments whose default database policy requires verification."""
    return environment.strip().lower() in _DEPLOYED_ENVIRONMENTS


def dsn_tls_mode(dsn: str | None) -> str | None:
    """The ``ssl`` mode a DSN asks for itself, or ``None`` if it asks for none.

    Matching is case-insensitive, and the normalised name is only ever used to
    DECIDE — nothing here rewrites what the DSN says. That distinction matters:
    asyncpg's own parsing IS case-sensitive (measured: ``ssl="Verify-Full"``
    raises ``ClientConfigurationError: 'sslmode' parameter must be one of:
    disable, allow, ...``). So a mis-cased value stays mis-cased and the
    operator gets that error, naming the valid spellings. Treating it as
    unrecognised instead would have overridden it with ``require``, which
    connects — turning a precise, loud config error into a working connection
    at a weaker mode than was asked for.

    Only ``ssl`` is read. asyncpg's ``connect()`` has no ``sslmode`` parameter
    and no ``**kwargs``, so a ``?sslmode=`` on one of these URLs is already a
    TypeError at connect time with or without this setting — recognising it
    here would only paper over that.
    """
    if not dsn:
        return None
    mode = parse_qs(urlsplit(dsn).query).get("ssl", [None])[-1]
    if mode is None:
        return None
    normalised = mode.strip().lower()
    return normalised if normalised in TLS_MODES else None


def tls_connect_args(
    dsn: str | None, *, require: bool, mode: str | None = None, root_cert: str | None = None
) -> dict[str, object]:
    """Apply the TLS floor without weakening a stronger DSN policy.

    Verified modes use the supplied CA bundle or the system trust store.
    Explicit local opt-outs preserve asyncpg's own DSN behavior.
    """
    if mode is None:
        mode = tls_mode_from_env(require=require)
    if root_cert is None:
        root_cert = os.environ.get("POSTGRES_SSL_ROOT_CERT", "")
    if mode and mode not in TLS_MODES:
        raise ValueError("Invalid POSTGRES_SSL_MODE")
    floor = mode or ("require" if require else "")
    if require and floor and TLS_MODES.index(floor) < TLS_MODES.index("require"):
        floor = "require"
    if not floor:
        if dsn and urlsplit(dsn).hostname not in {"localhost", "127.0.0.1", "::1", "db", "postgres"}:
            requested = dsn_tls_mode(dsn)
            if requested is None or TLS_MODES.index(requested) < TLS_MODES.index("require"):
                logging.getLogger(__name__).warning(
                    "PostgreSQL transport may fall back to cleartext; configure verified TLS"
                )
        return {}
    requested = dsn_tls_mode(dsn)
    if requested is not None and TLS_MODES.index(requested) >= TLS_MODES.index(floor):
        # Preserve stronger DSN policy. A supplied CA bundle must still reach it.
        if not root_cert or requested not in {"verify-ca", "verify-full"}:
            return {}
        floor = requested
    if floor in {"verify-ca", "verify-full"}:
        context = ssl.create_default_context(cafile=root_cert or None)
        context.check_hostname = floor == "verify-full"
        return {"ssl": context}
    return {"ssl": floor}


def tls_required_from_env() -> bool:
    """``POSTGRES_REQUIRE_SSL`` read straight from the environment.

    Only for callers that cannot construct ``Settings`` — see this module's
    docstring. Everything else must go through ``config.db_connect_args`` so
    the value comes from the one validated place.
    """
    value = os.environ.get("POSTGRES_REQUIRE_SSL")
    if value is not None:
        return value.strip().lower() in _TRUTHY
    return is_deployed_environment(os.environ.get("ENVIRONMENT", "development"))


def tls_mode_from_env(*, require: bool | None = None) -> str:
    """Read an explicit mode, or derive its default from the caller's policy.

    An explicit mode independently requests TLS even when the require flag is
    false. With no mode configured, require=False must not be overridden by a
    second read of the environment's require/default policy.
    """
    configured = os.environ.get("POSTGRES_SSL_MODE", "")
    if configured:
        return configured
    required = tls_required_from_env() if require is None else require
    if required and is_deployed_environment(os.environ.get("ENVIRONMENT", "development")):
        return "verify-full"
    return ""
