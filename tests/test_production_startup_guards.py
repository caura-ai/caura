"""``_validate_production_settings`` — the guards that refuse an unsafe prod boot.

H-11 of the 2026-08-14 OSS/platform audit: production did not require
``GATEWAY_SHARED_SECRET``, and the X-Tenant-ID header-trust path's perimeter check
is a no-op when it is unset — so that path accepted caller-supplied identity
headers from anyone who could reach core-api directly.

The whole block was previously untested, because it lived inline in ``lifespan``
where a test could not reach it without driving the entire startup sequence. It
is now a function, so every guard here is covered — not just the new one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from core_api.app import _validate_production_settings


def _prod(**overrides):
    """A production settings object that passes every guard, before overrides."""
    base = {
        "environment": "production",
        "is_standalone": False,
        "settings_encryption_key": "a-real-fernet-key",
        "jwt_secret": "a-real-secret",
        "admin_api_key": "a-real-admin-key",
        "gateway_shared_secret": "a-real-gateway-secret",
        "memclaw_api_key": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_a_fully_configured_production_boot_is_allowed():
    _validate_production_settings(_prod())


@pytest.mark.parametrize("environment", ["development", "sandbox"])
def test_non_production_environments_are_not_gated(environment):
    """The guards are production-only; dev/sandbox keep booting bare."""
    _validate_production_settings(
        _prod(
            environment=environment,
            gateway_shared_secret=None,
            admin_api_key=None,
            settings_encryption_key=None,
            is_standalone=True,
        )
    )


def test_production_requires_a_perimeter():
    """H-11. With neither control set, the header-trust path is wide open.

    An unset gateway secret DISABLES that path's perimeter check rather than
    weakening it — the check is ``if gw_secret and not compare_digest(...)``.
    """
    with pytest.raises(RuntimeError, match="GATEWAY_SHARED_SECRET"):
        _validate_production_settings(_prod(gateway_shared_secret=None))


def test_empty_strings_count_as_unset():
    """`""` is the shape a missing env var actually takes in a container."""
    with pytest.raises(RuntimeError, match="GATEWAY_SHARED_SECRET"):
        _validate_production_settings(
            _prod(gateway_shared_secret="", memclaw_api_key="")
        )


def test_memclaw_api_key_alone_is_an_acceptable_perimeter():
    """The network-exposed OSS pattern, and why the guard is not gateway-specific.

    When ``CAURA_API_KEY`` is set, auth.py's Path 2 either authenticates the
    request against that key or 401s everything else — so Path 4's header-trust
    surface is UNREACHABLE and there is nothing left to protect. Such a
    deployment legitimately sets ENVIRONMENT=production for JSON logging and
    Sentry, and must not be forced to invent a gateway secret it has no gateway
    for.
    """
    _validate_production_settings(
        _prod(gateway_shared_secret=None, memclaw_api_key="a-real-memclaw-key")
    )


def test_gateway_secret_alone_is_an_acceptable_perimeter():
    _validate_production_settings(
        _prod(gateway_shared_secret="a-real-gateway-secret", memclaw_api_key=None)
    )


def test_production_refuses_standalone_mode():
    with pytest.raises(RuntimeError, match="IS_STANDALONE=true is not allowed"):
        _validate_production_settings(_prod(is_standalone=True))


def test_production_requires_the_settings_encryption_key():
    with pytest.raises(RuntimeError, match="SETTINGS_ENCRYPTION_KEY must be set"):
        _validate_production_settings(_prod(settings_encryption_key=None))


def test_production_requires_the_admin_api_key():
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY must be set"):
        _validate_production_settings(_prod(admin_api_key=None))


def test_production_refuses_the_default_jwt_secret():
    with pytest.raises(RuntimeError, match="JWT_SECRET must be changed"):
        _validate_production_settings(_prod(jwt_secret="change-me-in-production"))


def test_a_secretstr_wrapped_default_is_still_caught():
    """The unwrap branch: a SecretStr would otherwise never equal the bad value."""

    class _Secret:
        def __init__(self, v):
            self._v = v

        def get_secret_value(self):
            return self._v

    with pytest.raises(RuntimeError, match="JWT_SECRET must be changed"):
        _validate_production_settings(
            _prod(jwt_secret=_Secret("change-me-in-production"))
        )
