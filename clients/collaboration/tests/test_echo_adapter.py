"""Unit test for the EchoAdapter — just verifies it logs the envelope.

The SDK lifecycle (run_adapter, bus subscribe) is exercised by the
integration tests; here we only confirm the adapter does what it
advertises: log the received envelope and otherwise no-op.
"""

from __future__ import annotations

import logging

from caura_bus_adapter.echo import EchoAdapter
from caura_bus_core import Envelope


async def test_consume_logs_envelope(caplog) -> None:
    caplog.set_level(logging.INFO, logger="caura-bus-adapter-echo")
    env = Envelope.new(from_="agent-a", to=["agent-b"], body="hello")
    await EchoAdapter().consume(env)
    assert any(env.id in rec.getMessage() for rec in caplog.records)
    assert any("hello" in rec.getMessage() for rec in caplog.records)


async def test_no_idle_gate_returns_immediately() -> None:
    await EchoAdapter().wait_until_idle()  # should not raise / not block
