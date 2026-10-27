"""Connection defaults and migration credentials, without contacting Postgres."""

import ssl
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event

from core_storage_api import config
from core_storage_api.database import init

pytestmark = pytest.mark.unit
_RUNTIME = "postgresql+asyncpg://runtime:example@db.example/caura"
_OWNER = "postgresql+asyncpg://owner:example@db.example/caura"


@pytest.mark.parametrize("environment", ["production", "sandbox"])
def test_deployed_connections_verify_server(monkeypatch, environment):
    monkeypatch.delenv("POSTGRES_REQUIRE_SSL", raising=False)
    settings = config.Settings(
        _env_file=None, environment=environment, database_url=_RUNTIME
    )
    assert settings.postgres_require_ssl is True
    monkeypatch.setattr(config, "settings", settings)
    context = config.db_connect_args(_RUNTIME)["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_explicit_mode_uses_verified_context(monkeypatch):
    settings = config.Settings(_env_file=None, postgres_ssl_mode="verify-full")
    monkeypatch.setattr(config, "settings", settings)
    context = config.db_connect_args(_RUNTIME)["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname


async def test_boot_migrations_use_owner_but_requests_use_runtime(monkeypatch):
    settings = config.Settings(
        _env_file=None,
        database_url=_RUNTIME,
        migration_database_url=_OWNER,
        postgres_require_ssl=False,
    )
    monkeypatch.setattr(init, "settings", settings)
    monkeypatch.setattr(init, "_engine", None)
    captured = []
    engines = []
    original_build = init._build_engine

    class Captured(Exception):
        pass

    def build(url):
        engine = original_build(url)
        engines.append(engine)

        @event.listens_for(engine.sync_engine, "do_connect")
        def capture(dialect, record, args, kwargs):
            captured.append(kwargs["user"])
            raise Captured

        return engine

    monkeypatch.setattr(init, "_build_engine", build)
    try:
        with pytest.raises(Captured):
            await init.init_database()
        with pytest.raises(Captured):
            await init.get_engine().connect()
    finally:
        for engine in engines:
            await engine.dispose()
    assert captured == ["owner", "runtime"]


async def test_reader_never_opens_migration_credentials(monkeypatch):
    monkeypatch.setattr(init.settings, "core_storage_role", "reader")
    opened = AsyncMock(side_effect=AssertionError("reader attempted a migration"))
    monkeypatch.setattr(init, "get_engine", opened)
    await init.init_database()
    opened.assert_not_called()


def test_dev_compose_api_defaults_to_loopback():
    from pathlib import Path

    import yaml

    compose = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "docker-compose.dev.yml").read_text()
    )
    assert compose["services"]["core-api"]["ports"] == [
        "${API_BIND:-127.0.0.1}:${API_PORT:-8000}:8000"
    ]


async def test_owner_pool_closes_when_migration_fails(monkeypatch):
    from unittest.mock import Mock

    settings = config.Settings(_env_file=None, migration_database_url=_OWNER)
    monkeypatch.setattr(init, "settings", settings)
    engine = Mock(dispose=AsyncMock())
    monkeypatch.setattr(init, "_build_engine", lambda url: engine)
    monkeypatch.setattr(
        init, "_init_database", AsyncMock(side_effect=RuntimeError("migration failed"))
    )
    with pytest.raises(RuntimeError, match="migration failed"):
        await init.init_database()
    engine.dispose.assert_awaited_once()


def test_root_certificate_path_is_not_ignored(monkeypatch, tmp_path):
    settings = config.Settings(
        _env_file=None,
        postgres_ssl_mode="verify-full",
        postgres_ssl_root_cert=str(tmp_path / "missing-ca.pem"),
    )
    monkeypatch.setattr(config, "settings", settings)
    with pytest.raises(FileNotFoundError):
        config.db_connect_args(_RUNTIME)


@pytest.fixture
def clean_tls_env(monkeypatch):
    for name in (
        "ENVIRONMENT",
        "POSTGRES_REQUIRE_SSL",
        "POSTGRES_SSL_MODE",
        "POSTGRES_SSL_ROOT_CERT",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("environment_flag", [None, "true", "false"])
def test_cli_caller_opt_out_ignores_environment_default(
    clean_tls_env, environment_flag
):
    from core_storage_api.db_tls import tls_connect_args

    clean_tls_env.setenv("ENVIRONMENT", "production")
    if environment_flag is not None:
        clean_tls_env.setenv("POSTGRES_REQUIRE_SSL", environment_flag)
    assert (
        tls_connect_args("postgresql+asyncpg://u:p@localhost/db", require=False) == {}
    )


@pytest.mark.parametrize(
    ("environment", "required"),
    [
        ("development", False),
        (" Development ", False),
        ("test", False),
        ("ci", False),
        ("local", False),
        ("staging", False),
        ("production", True),
        (" Production ", True),
        ("sandbox", True),
        (" SANDBOX ", True),
    ],
)
def test_cli_defaults_apply_only_to_deployed_environments(
    clean_tls_env, environment, required
):
    from core_storage_api.db_tls import tls_mode_from_env, tls_required_from_env

    clean_tls_env.setenv("ENVIRONMENT", environment)
    assert tls_required_from_env() is required
    assert tls_mode_from_env() == ("verify-full" if required else "")


@pytest.mark.parametrize(
    "environment_name", ["ENVIRONMENT", "environment", "EnViRoNmEnT"]
)
def test_settings_sources_resolve_environment_before_tls_default(
    clean_tls_env, environment_name
):
    clean_tls_env.setenv(environment_name, "production")
    settings = config.Settings(_env_file=None)
    assert settings.environment == "production"
    assert settings.postgres_require_ssl is True


def test_settings_file_can_disable_deployed_default(clean_tls_env, tmp_path):
    source = tmp_path / "synthetic-settings.txt"
    source.write_text("ENVIRONMENT=production\nPOSTGRES_REQUIRE_SSL=false\n")
    settings = config.Settings(_env_file=source)
    assert settings.environment == "production"
    assert settings.postgres_require_ssl is False
    source.write_text("ENVIRONMENT=sandbox\n")
    assert config.Settings(_env_file=source).postgres_require_ssl is True


def test_explicit_mode_is_independent_of_require_flag(clean_tls_env, monkeypatch):
    # Disabling the default floor must not silently disable an explicitly
    # requested verification mode. Clear the mode as well to opt out of both.
    settings = config.Settings(
        _env_file=None, postgres_require_ssl=False, postgres_ssl_mode="verify-full"
    )
    monkeypatch.setattr(config, "settings", settings)
    context = config.db_connect_args("postgresql+asyncpg://u:p@localhost/db")["ssl"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


def test_cli_caller_requirement_overrides_environment_opt_out(clean_tls_env):
    from core_storage_api.db_tls import tls_connect_args

    clean_tls_env.setenv("ENVIRONMENT", "production")
    clean_tls_env.setenv("POSTGRES_REQUIRE_SSL", "false")
    context = tls_connect_args("postgresql+asyncpg://u:p@localhost/db", require=True)[
        "ssl"
    ]
    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname
    assert context.verify_mode == ssl.CERT_REQUIRED
