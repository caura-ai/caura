"""Shared "durable, decision-bearing" corpus rules for the report surfaces.

Both the live report (``routes/reports.py``) and the cached agent-activity
digest generator (``services/agent_digest.py``) must describe the SAME corpus,
so the exclusion rules live here as a single source of truth.

The ``/memories/list`` storage API cannot push these excludes server-side, so
callers fetch raw rows and drop the noise client-side via :func:`is_cohesive`.
"""

from __future__ import annotations

import re

# Reporting window lengths in days, shared by the live report and the digest
# generator so "day"/"week" mean the same thing on both surfaces.
PERIOD_DAYS: dict[str, int] = {"day": 1, "week": 7}

# Episodic activity-log type(s) + the unattributed firehose agent excluded so the
# report reflects durable, decision-bearing per-agent work.
NON_DURABLE_TYPES = ("episode",)
RESERVED_FIREHOSE_AGENTS = ("main",)

# "Cohesive" filter: heartbeat / health-check / status-poll noise leaks in as
# NON-episode rows (e.g. action/outcome "heartbeat" or "Checked HEARTBEAT.md"
# writes), so the type+firehose exclusion above is not enough — a per-agent
# leaderboard built on it still counts monitoring pings as "what the agent did".
# This case-insensitive title regex drops that noise across ALL types so the
# report reflects real work; genuine rules/decisions/insights never match it, so
# the value/quality surfaces are unaffected. Pure-monitor agents fall off the
# leaderboard naturally once their pings are excluded.
NON_COHESIVE_TITLE_REGEX = (
    r"(heartbeat|health[- ]?check|healthz|healthy|watchdog|gpu.?health|no.?change|"
    r"auth error|zero auth|0 auth|encrypted|unreadable|no readable|no actionable|"
    r"no usable|no_reply|polled|quickcheck|app-fleet|discovery script|"
    r"gateway (active|reachable)|cache refresh)"
)
_NON_COHESIVE_TITLE_RE = re.compile(NON_COHESIVE_TITLE_REGEX, re.IGNORECASE)


def is_cohesive(m: dict) -> bool:
    """True if a memory row belongs in the durable report corpus: not episodic,
    not from a firehose agent, and not heartbeat/health/status-poll noise
    (title-only match, mirroring the ``exclude_title_regex`` predicate)."""
    return (
        m.get("memory_type") not in NON_DURABLE_TYPES
        and m.get("agent_id") not in RESERVED_FIREHOSE_AGENTS
        and not _NON_COHESIVE_TITLE_RE.search(m.get("title") or "")
    )
