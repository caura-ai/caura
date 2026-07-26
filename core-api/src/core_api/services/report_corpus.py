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


def passes_noise_filter(m: dict) -> bool:
    """True if a row is real signal — not a firehose agent and not
    heartbeat/health/status-poll noise (title-only match). Includes episodes:
    this is the base filter the digest's episode-fallback uses when an agent has
    no durable rows (its activity IS episodic)."""
    return m.get("agent_id") not in RESERVED_FIREHOSE_AGENTS and not _NON_COHESIVE_TITLE_RE.search(
        m.get("title") or ""
    )


def is_cohesive(m: dict) -> bool:
    """True if a memory row belongs in the durable report corpus: passes the
    noise filter AND is not an episodic activity-log type."""
    return passes_noise_filter(m) and m.get("memory_type") not in NON_DURABLE_TYPES


_SUBAGENT_RE = re.compile(r"^agent:(?P<parent>[^:]+):subagent:")


def resolve_parent(agent_id: str, known_ids: frozenset[str] | set[str]) -> str:
    """Resolve a subagent's top-level parent agent_id, for family rollup.

    The structured ``agents.owner_ref`` pointer is unpopulated in production, so
    the parent is derived from id conventions and resolved transitively to the
    root:
      1. ``agent:<parent>:subagent:<uuid>``            -> <parent>
      2. ``<parent>-<task>`` where <parent> is itself a known agent id
         (longest such prefix, boundary '-')           -> <parent>
      3. otherwise the agent is already top-level       -> itself

    ``known_ids`` is the tenant's agent-id set (already fetched by list_agents),
    so this needs no extra query. Prefer ``owner_ref`` upstream once populated.
    """
    seen: set[str] = set()
    cur = agent_id
    while cur not in seen:
        seen.add(cur)
        m = _SUBAGENT_RE.match(cur)
        if m:
            nxt = m.group("parent")
        else:
            # longest known-agent prefix on a '-' boundary (avoids matching an
            # unrelated agent that merely shares a leading substring)
            best = ""
            for pid in known_ids:
                if pid != cur and cur.startswith(pid + "-") and len(pid) > len(best):
                    best = pid
            nxt = best
        if not nxt or nxt == cur:
            return cur
        cur = nxt
    return cur
