"""Enablement policy for the anonymous heartbeat.

The truth table lives in ``docs/telemetry.md`` ("When it runs"). Every row
below is one condition; the heartbeat runs only when every row says on, so
any single off wins. The result and its reason are logged at boot and
returned by ``GET /api/v1/telemetry``.

This module reads configuration only. It never touches the network, the
storage client or the event loop — "off means zero work" starts here. That
includes the collector URL: a plain ``http://`` override to anything but
localhost is an off row, decided here at boot, not a refusal buried in the
sender at send time where nobody reads the DEBUG log.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from core_api.config import Settings

# Reasons, as returned by ``GET /telemetry`` and printed in the boot line.
REASON_CAURA_TELEMETRY_OFF: Final = "caura_telemetry_off"
REASON_DO_NOT_TRACK: Final = "do_not_track"
REASON_CI: Final = "ci"
REASON_ENTERPRISE_GATEWAY: Final = "enterprise_gateway"
REASON_MANAGED_PLATFORM: Final = "managed_platform"
REASON_PYTEST: Final = "pytest"
REASON_INVALID_ENDPOINT_URL: Final = "invalid_endpoint_url"

Reason = Literal[
    "caura_telemetry_off",
    "do_not_track",
    "ci",
    "enterprise_gateway",
    "managed_platform",
    "pytest",
    "invalid_endpoint_url",
]

# ``CAURA_TELEMETRY`` values that switch the heartbeat off. Anything else
# (including the default ``on``) leaves it on; the switch is a kill switch,
# not a tri-state.
_OFF_VALUES = frozenset({"off", "0", "false"})

# The one-line hint printed at boot and returned by ``GET /telemetry``.
DISABLE_HINT = "CAURA_TELEMETRY=off or DO_NOT_TRACK=1"

# Plain http is allowed to these hosts only (tests, a collector on the same
# box); everything else must be https so a mistyped override cannot send in
# clear text.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
ENDPOINT_URL_RULE = "CAURA_TELEMETRY_URL must be https:// (plain http is allowed for localhost only)"


def check_endpoint_url(url: str) -> None:
    """Raise ``ValueError`` unless ``url`` is https, or http to localhost."""
    parts = urlsplit(url or "")
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and (parts.hostname or "").lower() in _LOCAL_HOSTS:
        return
    raise ValueError(ENDPOINT_URL_RULE)


def endpoint_url_is_valid(url: str) -> bool:
    try:
        check_endpoint_url(url)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Enabled:
    """The heartbeat runs."""

    @property
    def enabled(self) -> bool:
        return True

    @property
    def reason(self) -> None:
        return None


@dataclass(frozen=True)
class Disabled:
    """The heartbeat does not run; ``reason`` is one of the ``REASON_*`` strings."""

    reason: Reason

    @property
    def enabled(self) -> bool:
        return False


Decision = Enabled | Disabled


def _is_off(value: str | None) -> bool:
    return (value or "").strip().lower() in _OFF_VALUES


def _do_not_track(value: str | None) -> bool:
    # Console Do Not Track convention (consoledonottrack.com): set and not
    # "0" means off. Empty counts as unset so ``DO_NOT_TRACK=`` in a shell
    # profile does not silently flip the switch.
    stripped = (value or "").strip()
    return bool(stripped) and stripped != "0"


def evaluate(settings: Settings, env: Mapping[str, str] | None = None) -> Decision:
    """Return ``Enabled()`` or ``Disabled(reason)`` for this process.

    ``env`` defaults to ``os.environ``; tests pass a dict. Rows are checked in
    the order of the truth table in ``docs/telemetry.md`` and the first off
    row supplies the reason.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    if _is_off(settings.caura_telemetry):
        return Disabled(REASON_CAURA_TELEMETRY_OFF)
    if _do_not_track(environ.get("DO_NOT_TRACK")):
        return Disabled(REASON_DO_NOT_TRACK)
    if environ.get("CI", "").strip():
        return Disabled(REASON_CI)
    if settings.gateway_shared_secret:
        return Disabled(REASON_ENTERPRISE_GATEWAY)
    if settings.platform_llm_provider or settings.platform_embedding_provider:
        return Disabled(REASON_MANAGED_PLATFORM)
    if environ.get("PYTEST_CURRENT_TEST"):
        return Disabled(REASON_PYTEST)
    if not endpoint_url_is_valid(settings.caura_telemetry_url):
        return Disabled(REASON_INVALID_ENDPOINT_URL)
    return Enabled()
