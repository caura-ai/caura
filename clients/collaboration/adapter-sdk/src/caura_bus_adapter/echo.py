"""Echo adapter — the &lt;30-LOC reference implementation.

Demonstrates the adapter-sdk surface. Logs every received envelope to
stdout and otherwise does nothing. Used by ``docker-compose.yml`` as
the two demo adapters that prove the bus is delivering end-to-end.
"""

from __future__ import annotations

import logging

from caura_bus_core import Envelope, load_config, require_api_key

from .sdk import (
    NoIdleGate,
    adapter_main,
    configure_logging,
    make_arg_parser,
    run_adapter,
)

log = logging.getLogger("caura-bus-adapter-echo")


class EchoAdapter(NoIdleGate):
    supports_interrupt = True
    capabilities = ["receive", "echo"]

    async def consume(self, env: Envelope) -> None:
        log.info(
            "RECV msg_id=%s kind=%s from=%s thread=%s body=%r",
            env.id,
            env.kind,
            env.from_,
            env.thread_id,
            env.body,
        )


def main() -> None:
    args = make_arg_parser(
        "caura-bus-adapter-echo",
        "Log every envelope addressed to this agent. Demo adapter.",
    ).parse_args()
    configure_logging(args.log_level)
    require_api_key()
    adapter_main(run_adapter(load_config(args.config), EchoAdapter()))


if __name__ == "__main__":
    main()
