"""Deterministic demo responder: requests get one idempotent, correlated reply."""

from caura_bus_core import Bus, Envelope, SendMessage, load_config

from .sdk import NoIdleGate, adapter_main, checkpoint, configure_logging, run_adapter


class Responder(NoIdleGate):
    supports_interrupt = True
    capabilities = ["respond", "collaborate"]

    def __init__(self, bus):
        self.bus = bus

    async def consume(self, env: Envelope):
        if env.kind == "request":
            # A structured task can ask this fixture to reach a consequential
            # step. Caura evaluates the checkpoint; no action executes while pending.
            import asyncio
            import json

            try:
                task = json.loads(env.body)
            except ValueError:
                task = {}
            if isinstance(task, dict) and task.get("action_type"):
                await checkpoint(
                    checkpoint_key="before-action",
                    summary=task.get("summary", "Agent task"),
                    proposed_action=task.get("proposed_action", "Complete the requested action"),
                    action_type=task["action_type"],
                    confidence=task.get("confidence", 1),
                    missing_information=task.get("missing_information", []),
                    conflicting_results=task.get("conflicting_results", False),
                )
            if isinstance(task, dict) and task.get("work_seconds"):
                await asyncio.sleep(min(float(task["work_seconds"]), 60))
            decision = next((part for part in env.parts if part.get("type") == "caura_human_decision"), None)
            body = f"Caura reply: {env.body}"
            if decision and decision.get("action") == "redirect":
                body = f"Caura redirected task: {decision['instructions']}"
            await self.bus.send(
                SendMessage(
                    to=[env.from_],
                    body=body,
                    kind="response",
                    reply_to=env.id,
                ),
                idempotency_key=f"reply:{env.id}",
            )


async def run():
    config = load_config()
    async with Bus(config) as bus:
        await run_adapter(config, Responder(bus))


if __name__ == "__main__":
    configure_logging("INFO")
    adapter_main(run())
