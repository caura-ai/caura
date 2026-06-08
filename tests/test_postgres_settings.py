"""Pin the POSTGRES_* env-var contract and the legacy ALLOYDB_* alias.

The Settings class was renamed from ``alloydb_*`` to ``postgres_*`` fields
with Pydantic ``AliasChoices`` so existing deployments using the legacy
``ALLOYDB_*`` env var names keep working transparently. These tests pin
that contract so a future refactor can't silently regress it.

A separate regression test guards the production-safety check in
``app.py`` that uses ``"postgres_password"`` as a string-keyed lookup —
the exact bug a careless rename would re-introduce.

Tests instantiate fresh ``Settings()`` directly (Pydantic reads env at
instance time). They deliberately avoid ``importlib.reload`` of the
config module — that mutates the module-level ``settings`` global and
leaks state into sibling test files (e.g. ``test_storage_client_routing``).
"""

import pytest

from core_api.config import Settings


_PG_KEYS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_DATABASE",
    "POSTGRES_USE_IAM_AUTH",
    "POSTGRES_REQUIRE_SSL",
    "ALLOYDB_HOST",
    "ALLOYDB_PORT",
    "ALLOYDB_USER",
    "ALLOYDB_PASSWORD",
    "ALLOYDB_DATABASE",
    "ALLOYDB_USE_IAM_AUTH",
    "ALLOYDB_REQUIRE_SSL",
)


@pytest.fixture
def clean_pg_env(monkeypatch):
    """Strip every Postgres / AlloyDB env var so each test starts cold."""
    for k in _PG_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def _fresh_settings() -> Settings:
    """A fresh Settings instance built from the current env. No reload."""
    return Settings()


def test_canonical_postgres_env_vars_resolve(clean_pg_env):
    clean_pg_env.setenv("POSTGRES_HOST", "pg.example")
    clean_pg_env.setenv("POSTGRES_PORT", "6543")
    clean_pg_env.setenv("POSTGRES_USER", "alice")
    clean_pg_env.setenv("POSTGRES_PASSWORD", "s3cret")
    clean_pg_env.setenv("POSTGRES_DB", "mydb")
    s = _fresh_settings()
    assert s.postgres_host == "pg.example"
    assert s.postgres_port == 6543
    assert s.postgres_user == "alice"
    # postgres_password is SecretStr — unwrap to compare.
    assert s.postgres_password.get_secret_value() == "s3cret"
    assert s.postgres_database == "mydb"


def test_legacy_alloydb_env_vars_still_work(clean_pg_env):
    """Backwards-compat — pre-1.0 deploys using ALLOYDB_* must keep working."""
    clean_pg_env.setenv("ALLOYDB_HOST", "legacy.example")
    clean_pg_env.setenv("ALLOYDB_PORT", "5432")
    clean_pg_env.setenv("ALLOYDB_USER", "bob")
    clean_pg_env.setenv("ALLOYDB_PASSWORD", "legacy")
    clean_pg_env.setenv("ALLOYDB_DATABASE", "legacydb")
    s = _fresh_settings()
    assert s.postgres_host == "legacy.example"
    assert s.postgres_user == "bob"
    assert s.postgres_password.get_secret_value() == "legacy"
    assert s.postgres_database == "legacydb"


def test_postgres_database_alias_accepts_legacy_long_form(clean_pg_env):
    """``POSTGRES_DATABASE`` (the long form, used in early drafts) still works."""
    clean_pg_env.setenv("POSTGRES_DATABASE", "longform")
    s = _fresh_settings()
    assert s.postgres_database == "longform"


def test_canonical_wins_when_both_set(clean_pg_env):
    """If both POSTGRES_HOST and ALLOYDB_HOST are set, canonical wins."""
    clean_pg_env.setenv("POSTGRES_HOST", "canonical.example")
    clean_pg_env.setenv("ALLOYDB_HOST", "legacy.example")
    s = _fresh_settings()
    assert s.postgres_host == "canonical.example"


def test_database_url_uses_renamed_fields(clean_pg_env):
    """``Settings.database_url`` must read postgres_* fields, not the old names."""
    clean_pg_env.setenv("POSTGRES_HOST", "h")
    clean_pg_env.setenv("POSTGRES_USER", "u")
    clean_pg_env.setenv("POSTGRES_PASSWORD", "p")
    clean_pg_env.setenv("POSTGRES_DB", "d")
    clean_pg_env.setenv("POSTGRES_PORT", "5433")
    s = _fresh_settings()
    assert s.database_url == "postgresql+asyncpg://u:p@h:5433/d"


def test_database_url_quotes_special_chars_in_credentials(clean_pg_env):
    """User and password with URL-special chars must be percent-encoded.

    Without quote_plus, a password containing ``@``, ``:``, ``/``, ``?``,
    or ``#`` produces a malformed URL and an opaque asyncpg connection
    error. quote_plus encodes them so the URL parses correctly.
    """
    clean_pg_env.setenv("POSTGRES_HOST", "h")
    clean_pg_env.setenv("POSTGRES_USER", "alice@admin")
    clean_pg_env.setenv("POSTGRES_PASSWORD", "p@ss:w/d?#")
    clean_pg_env.setenv("POSTGRES_DB", "d")
    s = _fresh_settings()
    # @ → %40, : → %3A, / → %2F, ? → %3F, # → %23
    assert "alice%40admin" in s.database_url
    assert "p%40ss%3Aw%2Fd%3F%23" in s.database_url
    # The host:port@/db separators stay literal — only credentials are encoded.
    assert "@h:5432/d" in s.database_url


def test_database_url_encodes_spaces_as_percent20_not_plus(clean_pg_env):
    """Spaces in credentials must be ``%20`` (URL-encoding), not ``+``
    (form-encoding). ``quote_plus`` would emit ``+``, which is invalid in
    a URL's userinfo section. ``quote(s, safe='')`` is the correct choice.
    Pinning this so a future ``quote_plus`` regression can't sneak back in.
    """
    clean_pg_env.setenv("POSTGRES_HOST", "h")
    clean_pg_env.setenv("POSTGRES_USER", "alice user")
    clean_pg_env.setenv("POSTGRES_PASSWORD", "correct horse battery staple")
    clean_pg_env.setenv("POSTGRES_DB", "d")
    s = _fresh_settings()
    assert "alice%20user" in s.database_url
    assert "correct%20horse%20battery%20staple" in s.database_url
    # No ``+`` should appear in the userinfo segment (between ``://`` and
    # ``@``). The driver prefix ``postgresql+asyncpg`` legitimately
    # contains a ``+`` and lives before ``://``, so we slice carefully.
    userinfo = s.database_url.split("://", 1)[1].split("@", 1)[0]
    assert "+" not in userinfo, (
        f"userinfo uses form-encoding (+) instead of URL-encoding (%20): {userinfo!r}"
    )


def test_app_dangerous_defaults_check_uses_renamed_field():
    """Regression for app.py's production-safety check.

    The check uses ``getattr(app_settings, var, None)`` with a string key
    pulled from a literal dict. After the alloydb_*→postgres_* rename, the
    string ``"alloydb_password"`` resolved to None and silently bypassed
    the safety guard. This test pins the literal as ``"postgres_password"``
    so it cannot regress.
    """
    import inspect

    import core_api.app as app_module

    src = inspect.getsource(app_module)
    # Must reference the renamed field.
    assert '"postgres_password"' in src or "'postgres_password'" in src, (
        "core_api.app must guard postgres_password against the default 'changeme' "
        "in production. The string-literal field name must match the renamed "
        "Settings attribute."
    )
    # Must NOT reference the old field — that would silently no-op via getattr.
    assert '"alloydb_password"' not in src and "'alloydb_password'" not in src, (
        "Stale 'alloydb_password' string literal — would silently bypass the "
        "production safety check after the postgres_* rename."
    )
