"""TLS policy for the Postgres connections this service opens.

Deliberately a module of its own, importable with no service configuration at
all: it constructs no ``Settings`` and imports nothing that does. ``config.py``
supplies the configured flag; ``scripts/preflight_012.py`` reaches the same
policy through the environment when service config cannot be built in the
invoking shell, which is precisely the case its ``--dsn`` flag exists for.
Keeping the policy here is what lets both do that without a second copy of it.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

# asyncpg's TLS modes, weakest first. ``allow`` and ``prefer`` will silently
# fall back to cleartext, so neither satisfies a setting named "require".
TLS_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")

_TRUTHY = {"1", "true", "yes", "on"}


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


def tls_connect_args(dsn: str | None, *, require: bool) -> dict[str, object]:
    """asyncpg connect kwargs for a TLS policy that is a FLOOR, not an override.

    SQLAlchemy merges ``connect_args`` **on top of** the parameters it parses
    out of the URL (``immutabledict(cparams).union(connect_args)`` in
    ``sqlalchemy.engine.create``), so the kwarg wins over the DSN. Returning
    ``{"ssl": "require"}`` unconditionally would therefore downgrade an
    operator who set ``POSTGRES_REQUIRE_SSL=true`` *and* asked for
    ``?ssl=verify-full``, turning certificate verification off in silence —
    which is the failure this setting exists to stop. So:

    - DSN already at ``require`` or stronger -> hands off, it stays.
    - DSN weaker, unrecognised, or absent -> ``require``, because none of those
      is evidence the connection will actually be encrypted.

    Empty when TLS is off, so the engine call is byte-identical to what it was
    and the default deployment is unchanged.
    """
    if not require:
        return {}
    mode = dsn_tls_mode(dsn)
    if mode is not None and TLS_MODES.index(mode) >= TLS_MODES.index("require"):
        return {}
    return {"ssl": "require"}


def tls_required_from_env() -> bool:
    """``POSTGRES_REQUIRE_SSL`` read straight from the environment.

    Only for callers that cannot construct ``Settings`` — see this module's
    docstring. Everything else must go through ``config.db_connect_args`` so
    the value comes from the one validated place.
    """
    return os.environ.get("POSTGRES_REQUIRE_SSL", "").strip().lower() in _TRUTHY
