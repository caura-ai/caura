"""The hold the broker's write gate asks for (g2.5).

caura-daemon's write gate refuses an agent's write to a memory file, such as
CLAUDE.md, when the fleet policy denies it or requires approval (g2.3, g2.4).
It then sends the refused write here to be held for a person's review: an item
of a bulk write with ``status: "quarantined"``, whose content is what the agent
tried to put in the file, and whose ``metadata.write_gate`` is the gate's
account of it: the ``tool``, the ``paths`` it wrote, the ``action`` the policy
decided and the ``rule_ids`` that decided it.

Only the broker's install credential may ask for that status; anyone else's
item is refused, as any status outside the vocabulary is. The account is kept
in ``_system.hold``, with the reason ``write_gate``, where no caller can write,
and anyone else's is stripped with the platform keys: a write can't vouch for
its own provenance. A cloud that predates this refuses the status too, so a
held write never lands live on it.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WRITE_GATE_KEY = "write_gate"
WRITE_GATE_HOLD_REASON = "write_gate"

# The policy actions the gate holds a write on.
_ACTIONS = frozenset({"deny", "require_approval"})
# Bounds past which an account can only be a broker bug's.
_MAX_TOOL = 64
_MAX_PATHS = 16
_MAX_PATH = 4096
_MAX_RULE_IDS = 16
_MAX_RULE_ID = 200


def write_gate_hold_from(metadata: dict | None) -> dict:
    """The hold for a write the broker's gate refused: its reason and the gate's account.

    A malformed part of the account is dropped, not the hold: the status, not
    the account, is what holds the write. The warning is the trace. Keys other
    than the four are ignored, so a broker that adds one doesn't lose the
    account on an older cloud.
    """
    hold: dict = {"reason": WRITE_GATE_HOLD_REASON}
    raw = (metadata or {}).get(WRITE_GATE_KEY)
    if raw is None:
        return hold
    if not isinstance(raw, dict):
        logger.warning("dropping a write-gate account that isn't an object")
        return hold
    tool = raw.get("tool")
    if isinstance(tool, str) and 0 < len(tool) <= _MAX_TOOL:
        hold["tool"] = tool
    elif tool is not None:
        logger.warning("dropping a malformed write-gate tool")
    for key, max_items, max_len in (
        ("paths", _MAX_PATHS, _MAX_PATH),
        ("rule_ids", _MAX_RULE_IDS, _MAX_RULE_ID),
    ):
        values = raw.get(key)
        if _strings(values, max_items, max_len):
            hold[key] = values
        elif values is not None:
            logger.warning("dropping malformed write-gate %s", key)
    action = raw.get("action")
    if action in _ACTIONS:
        hold["action"] = action
    elif action is not None:
        logger.warning("dropping a malformed write-gate action")
    return hold


def _strings(values: object, max_items: int, max_len: int) -> bool:
    """Whether ``values`` is a list of 1 to ``max_items`` strings of 1 to ``max_len`` characters."""
    return (
        isinstance(values, list)
        and 0 < len(values) <= max_items
        and all(isinstance(v, str) and 0 < len(v) <= max_len for v in values)
    )
