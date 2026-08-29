from __future__ import annotations

import pytest

from core_storage_api.config import LOCAL_DATABASE_URL, Settings

ALLOYDB_NAMES = (
    "ALLOYDB_HOST",
    "ALLOYDB_PORT",
    "ALLOYDB_USER",
    "ALLOYDB_PASSWORD",
    "ALLOYDB_DATABASE",
)


def _clear_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ALLOYDB_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_alloydb_fields_build_the_runtime_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("ALLOYDB_HOST", "10.0.0.8")
    monkeypatch.setenv("ALLOYDB_PORT", "6432")
    monkeypatch.setenv("ALLOYDB_USER", "sandbox@user")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "p/a:ss word")
    monkeypatch.setenv("ALLOYDB_DATABASE", "sandbox/name")

    settings = Settings(_env_file=None)

    assert settings.database_url.get_secret_value() == (
        "postgresql+asyncpg://sandbox%40user:p%2Fa%3Ass%20word@10.0.0.8:6432/sandbox%2Fname"
    )


def test_derived_database_url_is_hidden_from_settings_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("ALLOYDB_HOST", "10.0.0.8")
    monkeypatch.setenv("ALLOYDB_USER", "sandbox-user")
    monkeypatch.setenv("ALLOYDB_PASSWORD", "sensitive-password")
    monkeypatch.setenv("ALLOYDB_DATABASE", "sandbox-db")
    monkeypatch.setenv(
        "READ_DATABASE_URL",
        "postgresql+asyncpg://reader:read-sensitive-password@replica/sandbox-db",
    )

    settings = Settings(_env_file=None)

    assert "sensitive-password" not in repr(settings)
    assert "sensitive-password" not in str(settings)
    assert "sensitive-password" not in repr(dict(settings))
    assert "read-sensitive-password" not in repr(settings)
    assert "read-sensitive-password" not in str(settings)
    assert "read-sensitive-password" not in repr(dict(settings))
    assert "database_url" not in settings.model_dump()
    assert "read_database_url" not in settings.model_dump()


def test_explicit_database_url_wins_over_partial_alloydb_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://explicit/db")
    monkeypatch.setenv("ALLOYDB_HOST", "ignored")

    assert Settings(_env_file=None).database_url.get_secret_value() == "postgresql+asyncpg://explicit/db"


def test_alloydb_fields_load_from_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_database_env(monkeypatch)
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "ALLOYDB_HOST=10.0.0.9\n"
        "ALLOYDB_USER=dotenv-user\n"
        "ALLOYDB_PASSWORD=dotenv-password\n"
        "ALLOYDB_DATABASE=dotenv-db\n"
    )

    assert Settings(_env_file=dotenv).database_url.get_secret_value() == (
        "postgresql+asyncpg://dotenv-user:dotenv-password@10.0.0.9:5432/dotenv-db"
    )


def test_alloydb_fields_load_from_constructor(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)

    settings = Settings(
        _env_file=None,
        alloydb_host="10.0.0.10",
        alloydb_user="constructor-user",
        alloydb_password="constructor-password",
        alloydb_database="constructor-db",
    )

    assert settings.database_url.get_secret_value() == (
        "postgresql+asyncpg://constructor-user:constructor-password@10.0.0.10:5432/constructor-db"
    )


def test_partial_alloydb_configuration_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("ALLOYDB_HOST", "10.0.0.8")

    with pytest.raises(ValueError, match="incomplete AlloyDB configuration"):
        Settings(_env_file=None)


def test_port_only_alloydb_configuration_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)
    monkeypatch.setenv("ALLOYDB_PORT", "6432")

    with pytest.raises(ValueError, match="incomplete AlloyDB configuration"):
        Settings(_env_file=None)


def test_unconfigured_database_keeps_the_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_database_env(monkeypatch)

    assert Settings(_env_file=None).database_url.get_secret_value() == LOCAL_DATABASE_URL


def test_storage_secret_loads_from_file_and_stays_out_of_serialization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CORE_STORAGE_SHARED_SECRET", raising=False)
    monkeypatch.delenv("CORE_STORAGE_SHARED_SECRET_FILE", raising=False)
    secret_file = tmp_path / "storage-secret"
    secret_file.write_text("file-storage-secret\n")

    settings = Settings(
        _env_file=None,
        core_storage_shared_secret_file=str(secret_file),
    )

    assert settings.core_storage_shared_secret.get_secret_value() == "file-storage-secret"
    assert "file-storage-secret" not in repr(settings)
    assert "core_storage_shared_secret" not in settings.model_dump()
