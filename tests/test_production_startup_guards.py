"""Startup guards that refuse an unusable or unsafe boot.

H-11 of the 2026-08-14 OSS/platform audit: production did not require
``GATEWAY_SHARED_SECRET``, and the X-Tenant-ID header-trust path's perimeter check
is a no-op when it is unset — so that path accepted caller-supplied identity
headers from anyone who could reach core-api directly.

The whole block was previously untested, because it lived inline in ``lifespan``
where a test could not reach it without driving the entire startup sequence. It
is now a function, so every guard here is covered — not just the new one.

M-78 and L-68 of the 2026-10-01 OSS audit: staging and every sandbox run as
``ENVIRONMENT=sandbox``, which skipped every guard, perimeter included. A sandbox
now keeps the perimeter and standalone guards; the production-only secrets stay
production-only by decision. Production also refuses ``TESTING=1``, which mounts
the test-only routes.

M-15 of the same audit: ``DEPLOYMENT_MODE=deferred`` on the default in-process
event bus published every write's embed and enrich requests to no subscriber, so
every memory stayed unembedded and unenriched with no error or log. Every
environment now refuses that boot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from common.events.inprocess import InProcessEventBus
from core_api.app import _DANGEROUS_DEFAULTS, _validate_startup_settings
from tests._legacy_contracts import LEGACY_API_KEY_FIELD


def _prod(*, compat_api_key=None, **overrides):
    """A production settings object that passes every guard, before overrides."""
    base = {
        "environment": "production",
        "is_standalone": False,
        "settings_encryption_key": "a-real-fernet-key",
        "jwt_secret": "a-real-secret",
        "admin_api_key": "a-real-admin-key",
        "gateway_shared_secret": "a-real-gateway-secret",
        LEGACY_API_KEY_FIELD: compat_api_key,
        "core_storage_shared_secret": SecretStr("a-real-storage-secret"),
        "deployment_mode": "inline",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _no_testing_flag(monkeypatch):
    """The suite runs with ``TESTING=1`` (tests/conftest.py), which production
    refuses, so each guard here starts from a deployment without it."""
    monkeypatch.delenv("TESTING", raising=False)


def test_a_fully_configured_production_boot_is_allowed():
    _validate_startup_settings(_prod())


def test_development_skips_every_hosted_guard():
    """With storage auth configured, a local development boot needs nothing else."""
    _validate_startup_settings(
        _prod(
            environment="development",
            gateway_shared_secret=None,
            admin_api_key=None,
            settings_encryption_key=None,
            jwt_secret="",
            is_standalone=True,
        )
    )


def _sandbox(**overrides):
    """A hosted non-production deployment that passes the sandbox guards."""
    return _prod(environment="sandbox", **overrides)


def test_sandbox_skips_only_the_production_secrets():
    """M-78, scoped by decision: a sandbox keeps the perimeter guards, but not the
    production-only secrets, which its deploy is not known to set."""
    _validate_startup_settings(
        _sandbox(admin_api_key=None, settings_encryption_key=None, jwt_secret="")
    )


def test_sandbox_requires_a_perimeter():
    """M-78. A sandbox is reachable like production, so with neither control set
    its header-trust path takes identity headers from anyone."""
    with pytest.raises(RuntimeError, match="GATEWAY_SHARED_SECRET"):
        _validate_startup_settings(_sandbox(gateway_shared_secret=None))


def test_sandbox_accepts_the_api_key_as_its_perimeter():
    _validate_startup_settings(
        _sandbox(gateway_shared_secret=None, compat_api_key="a-real-api-key")
    )


def test_sandbox_refuses_standalone_mode():
    with pytest.raises(RuntimeError, match="IS_STANDALONE=true is not allowed"):
        _validate_startup_settings(_sandbox(is_standalone=True))


def test_production_refuses_testing_mode(monkeypatch):
    """L-68. ``TESTING=1`` mounts the test-only ``/testing`` routes, and the same
    variable was their only guard, so one inherited from a CI image reached
    production unnoticed."""
    monkeypatch.setenv("TESTING", "1")
    with pytest.raises(RuntimeError, match="TESTING=1"):
        _validate_startup_settings(_prod())


def test_production_requires_a_perimeter():
    """H-11. With neither control set, the header-trust path is wide open.

    An unset gateway secret DISABLES that path's perimeter check rather than
    weakening it — the check is ``if gw_secret and not compare_digest(...)``.
    """
    with pytest.raises(RuntimeError, match="GATEWAY_SHARED_SECRET"):
        _validate_startup_settings(_prod(gateway_shared_secret=None))


def test_empty_strings_count_as_unset():
    """`""` is the shape a missing env var actually takes in a container."""
    with pytest.raises(RuntimeError, match="GATEWAY_SHARED_SECRET"):
        _validate_startup_settings(_prod(gateway_shared_secret="", compat_api_key=""))


def test_api_key_alone_is_an_acceptable_perimeter():
    """The network-exposed OSS pattern, and why the guard is not gateway-specific.

    When ``CAURA_API_KEY`` is set, auth.py's Path 2 either authenticates the
    request against that key or 401s everything else — so Path 4's header-trust
    surface is UNREACHABLE and there is nothing left to protect. Such a
    deployment legitimately sets ENVIRONMENT=production for JSON logging and
    Sentry, and must not be forced to invent a gateway secret it has no gateway
    for.
    """
    _validate_startup_settings(
        _prod(gateway_shared_secret=None, compat_api_key="a-real-api-key")
    )


def test_gateway_secret_alone_is_an_acceptable_perimeter():
    _validate_startup_settings(_prod(gateway_shared_secret="a-real-gateway-secret"))


def test_production_refuses_standalone_mode():
    with pytest.raises(RuntimeError, match="IS_STANDALONE=true is not allowed"):
        _validate_startup_settings(_prod(is_standalone=True))


def test_production_requires_the_settings_encryption_key():
    with pytest.raises(RuntimeError, match="SETTINGS_ENCRYPTION_KEY must be set"):
        _validate_startup_settings(_prod(settings_encryption_key=None))


def test_production_requires_the_admin_api_key():
    with pytest.raises(RuntimeError, match="ADMIN_API_KEY must be set"):
        _validate_startup_settings(_prod(admin_api_key=None))


@pytest.mark.parametrize("environment", ["development", "sandbox", "production"])
def test_every_environment_requires_the_storage_shared_secret(environment):
    with pytest.raises(RuntimeError, match="CORE_STORAGE_SHARED_SECRET"):
        _validate_startup_settings(
            _prod(
                environment=environment,
                core_storage_shared_secret=SecretStr(""),
            )
        )


@pytest.mark.parametrize("secret_value", [" ", "\t"])
def test_blank_storage_shared_secrets_are_also_rejected(secret_value):
    with pytest.raises(RuntimeError, match="CORE_STORAGE_SHARED_SECRET"):
        _validate_startup_settings(
            _prod(
                environment="development",
                core_storage_shared_secret=SecretStr(secret_value),
            )
        )


def test_production_refuses_the_default_jwt_secret():
    with pytest.raises(RuntimeError, match="JWT_SECRET must be changed"):
        _validate_startup_settings(_prod(jwt_secret="change-me-in-production"))


_BLANKS = [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="spaces"),
    pytest.param("\t\n", id="tab-newline"),
]


@pytest.mark.parametrize("var", sorted(_DANGEROUS_DEFAULTS))
@pytest.mark.parametrize("blank", _BLANKS)
def test_production_refuses_a_blank_dangerous_default(var, blank):
    """A blank secret is not "changed from the default" — it is worse.

    ``SETTINGS_ENCRYPTION_KEY`` is checked for presence ten lines up; this loop
    compared only against one literal, so ``JWT_SECRET=""`` was not equal to the
    placeholder and production booted signing API tokens with an empty secret.

    Parametrized over ``_DANGEROUS_DEFAULTS`` rather than naming the field, so
    the next secret added there is covered the day it is added instead of
    inheriting the exact gap this closes.
    """
    with pytest.raises(RuntimeError, match=f"{var.upper()} must be set"):
        _validate_startup_settings(_prod(**{var: blank}))


@pytest.mark.parametrize("var", sorted(_DANGEROUS_DEFAULTS))
@pytest.mark.parametrize(
    "blank", [pytest.param("", id="empty"), pytest.param("   ", id="spaces")]
)
def test_production_refuses_a_wrapped_blank_dangerous_default(var, blank):
    """The unwrap must precede the presence check, not follow it.

    ``SecretStr("")`` is an OBJECT, so it is truthy: a presence test run on the
    wrapper passes a blank secret straight through. Distinct from the wrapped
    *default* test below, which covers the equality path rather than this one.
    """
    with pytest.raises(RuntimeError, match=f"{var.upper()} must be set"):
        _validate_startup_settings(_prod(**{var: SecretStr(blank)}))


@pytest.mark.parametrize("var, bad_val", sorted(_DANGEROUS_DEFAULTS.items()))
@pytest.mark.parametrize(
    "pad",
    [
        pytest.param("{}\n", id="trailing-newline"),
        pytest.param(" {}", id="leading-space"),
        pytest.param("  {}  ", id="both"),
    ],
)
def test_production_refuses_the_default_even_with_surrounding_whitespace(
    var, bad_val, pad
):
    """The placeholder must not become bootable by picking up whitespace.

    Presence stripped and equality did not, so ``change-me-in-production\n``
    passed both checks and production signed API tokens with the published
    default. A trailing newline is not contrived — it is what a file-mounted
    secret yields (``cat key >> .env``, a k8s ``stringData`` block scalar),
    which is the delivery path a left-in-place placeholder arrives by.
    """
    with pytest.raises(RuntimeError, match=f"{var.upper()} must be changed"):
        _validate_startup_settings(_prod(**{var: pad.format(bad_val)}))


@pytest.mark.parametrize(
    "field, expected",
    [
        pytest.param(
            "settings_encryption_key", "SETTINGS_ENCRYPTION_KEY", id="fernet-key"
        ),
        pytest.param("admin_api_key", "ADMIN_API_KEY", id="admin-key"),
    ],
)
def test_production_refuses_a_whitespace_only_sibling_secret(field, expected):
    """The same defect class, in the guards either side of the loop.

    Each was a bare truthiness test, so ``"   "`` passed — including
    SETTINGS_ENCRYPTION_KEY, the guard this fix's own reasoning cites as the
    one that checked presence properly. It checked presence, not blankness.
    """
    with pytest.raises(RuntimeError, match=expected):
        _validate_startup_settings(_prod(**{field: "   "}))


def test_production_refuses_a_whitespace_only_perimeter():
    """A whitespace gateway secret is an outage dressed as a perimeter.

    HTTP strips optional whitespace from header values, so the gateway's own
    injected secret arrives empty and every header-trust request 401s. Refusing
    to boot is the louder failure this block already prefers.
    """
    with pytest.raises(RuntimeError, match=r"GATEWAY_SHARED_SECRET|CAURA_API_KEY"):
        _validate_startup_settings(
            _prod(gateway_shared_secret="   ", compat_api_key="\n")
        )


def test_a_secretstr_wrapped_default_is_still_caught():
    """The unwrap branch: a SecretStr would otherwise never equal the bad value."""

    class _Secret:
        def __init__(self, v):
            self._v = v

        def get_secret_value(self):
            return self._v

    with pytest.raises(RuntimeError, match="JWT_SECRET must be changed"):
        _validate_startup_settings(_prod(jwt_secret=_Secret("change-me-in-production")))


# ── M-15: deferred mode needs a bus core-worker shares ──────────────────────


@pytest.mark.parametrize("environment", ["development", "sandbox", "production"])
def test_deferred_mode_on_the_in_process_bus_is_refused(monkeypatch, environment):
    """Deferred writes publish embed and enrich requests that only core-worker
    consumes. On the in-process bus nothing in core-api subscribes to them, so
    every memory stayed unembedded and unenriched with no error or log."""
    monkeypatch.setattr("core_api.app.get_event_bus", lambda: InProcessEventBus())
    with pytest.raises(RuntimeError, match="DEPLOYMENT_MODE=deferred"):
        _validate_startup_settings(
            _prod(environment=environment, deployment_mode="deferred")
        )


def test_deferred_mode_on_a_shared_bus_is_allowed(monkeypatch):
    """Control: the SaaS shape, core-worker subscribed on Pub/Sub."""

    class _SharedBus:
        pass

    monkeypatch.setattr("core_api.app.get_event_bus", _SharedBus)
    _validate_startup_settings(_prod(deployment_mode="deferred"))


def test_inline_mode_does_not_build_the_event_bus(monkeypatch):
    """Inline boots never consult the bus here, so validation builds nothing
    ahead of the lifespan."""

    def _no_bus():
        raise AssertionError("an inline boot must not build the event bus")

    monkeypatch.setattr("core_api.app.get_event_bus", _no_bus)
    _validate_startup_settings(_prod())
