"""The rules receipt a broker write carries (g2.8).

caura-daemon records a ``session.rules_delivered`` receipt each time it hands a
session its rules (g1.6), and stamps that session's latest one on each memory it
writes there, beside ``session_id``: ``metadata.rules_receipt`` holds
``event_id``, the id the cloud keeps the receipt's audit event under, and
``rule_set_hash``, the hash of the rules it delivered (null when it delivered
none). A memory then names the delivery its session was under.

Only an install credential's copy is kept, in ``_system.rules_receipt``, where
no caller can write. Anyone else's is stripped with the platform keys: an agent
naming its own receipt would be vouching for itself.
"""

from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger(__name__)

RULES_RECEIPT_KEY = "rules_receipt"

# A rule-set hash as both the broker and ``common.governance.ruleset_hash`` write
# it: a sha256 hex digest, lowercase.
_RULE_SET_HASH = re.compile(r"[0-9a-f]{64}")


def rules_receipt_from(metadata: dict | None) -> dict | None:
    """The receipt in a broker write's ``metadata``, normalised; None if it has none.

    A malformed one is dropped, not the write: it can only come from a broker
    bug, and the memory is worth keeping without it. The warning is the trace.
    Keys other than the two are ignored, so a broker that adds one doesn't lose
    the receipt on an older cloud.
    """
    raw = (metadata or {}).get(RULES_RECEIPT_KEY)
    if raw is None:
        return None
    try:
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        if not isinstance(raw.get("event_id"), str):
            raise ValueError("event_id is not a string")
        event_id = str(uuid.UUID(raw["event_id"]))
        if "rule_set_hash" not in raw:
            raise ValueError("rule_set_hash is missing")
        rule_set_hash = raw["rule_set_hash"]
        if rule_set_hash is not None and not (
            isinstance(rule_set_hash, str) and _RULE_SET_HASH.fullmatch(rule_set_hash)
        ):
            raise ValueError("rule_set_hash is neither null nor a sha256 hex digest")
    except ValueError as exc:
        logger.warning("dropping a malformed rules receipt: %s", exc)
        return None
    return {"event_id": event_id, "rule_set_hash": rule_set_hash}
