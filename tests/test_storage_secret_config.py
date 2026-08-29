"""Storage shared-secret configuration safety in core-api."""

from __future__ import annotations

import pytest
from core_api.config import Settings


def test_storage_secret_is_excluded_from_repr_and_serialization() -> None:
    settings = Settings(core_storage_shared_secret="api-storage-secret")

    assert (
        settings.core_storage_shared_secret.get_secret_value() == "api-storage-secret"
    )
    assert "api-storage-secret" not in repr(settings)
    assert "core_storage_shared_secret" not in settings.model_dump()


def test_storage_secret_loads_from_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CORE_STORAGE_SHARED_SECRET", raising=False)
    monkeypatch.delenv("CORE_STORAGE_SHARED_SECRET_FILE", raising=False)
    secret_file = tmp_path / "storage-secret"
    secret_file.write_text("api-file-storage-secret\n")

    settings = Settings(
        _env_file=None,
        core_storage_shared_secret_file=str(secret_file),
    )

    assert (
        settings.core_storage_shared_secret.get_secret_value()
        == "api-file-storage-secret"
    )


def test_direct_storage_secret_wins_over_file(tmp_path) -> None:
    secret_file = tmp_path / "storage-secret"
    secret_file.write_text("ignored-file-secret\n")

    settings = Settings(
        _env_file=None,
        core_storage_shared_secret="direct-api-secret",
        core_storage_shared_secret_file=str(secret_file),
    )

    assert settings.core_storage_shared_secret.get_secret_value() == "direct-api-secret"
