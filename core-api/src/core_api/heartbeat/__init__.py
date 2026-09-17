"""Anonymous daily heartbeat from self-hosted Caura servers.

One ping a day to the collector named by ``settings.caura_telemetry_url``,
carrying bucketed counts and provider kinds — never names, addresses, content
or exact numbers. ``docs/telemetry.md`` is the operator-facing contract and
``docs/telemetry-schema-v1.json`` the payload schema; both change together.

Module map:

* :mod:`.policy`   — the enablement decision (``Enabled`` | ``Disabled(reason)``).
* :mod:`.payload`  — the payload builder plus the shared bucketing function.
* :mod:`.clients`  — the in-process User-Agent family counter.
* :mod:`.identity` — the persisted ``deployment_id`` / ``deployment_token``.
* :mod:`.sender`   — the loop, the HTTP call and the boot log lines.

Named ``heartbeat`` rather than ``telemetry`` so it does not collide with
``caura_rail.Telemetry`` in docs or with the ``request_observation``
middleware.
"""

from core_api.heartbeat.policy import Decision, Disabled, Enabled, evaluate
from core_api.heartbeat.sender import HeartbeatSender, get_sender, install

__all__ = [
    "Decision",
    "Disabled",
    "Enabled",
    "HeartbeatSender",
    "evaluate",
    "get_sender",
    "install",
]
