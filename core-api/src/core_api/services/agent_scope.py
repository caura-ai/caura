"""CAURA-723 — tell a caller when its agent filter is why the result is empty.

The defect: a tenant-scoped caller that passes a wrong ``filter_agent_id`` gets
``HTTP 200 · items [] · warnings null`` — byte-identical to a correct id that
simply has nothing relevant to say. The filter is a SQL ``WHERE
memories.agent_id = ?``, so a typo matches no rows and the query returns
nothing, exactly as a genuinely empty query would.

Wet-measured, one memory written by ``agent-real``:

    filter_agent_id = "agent-real"  -> 200  items=1
    filter_agent_id = "agnet-real"  -> 200  items=0  warnings=None   <-- silent

It fails in the safe-looking direction, which is why it survives: an empty list
reads as "no data yet", the most ordinary thing a memory product can say, so
nobody investigates. It cost a 589-query benchmark run against an empty store.

WHY THIS IS TENANT-SCOPED ONLY. An agent-scoped credential carries a verified
``X-Agent-ID``, and ``enforce_self_agent`` **403s** it for naming any agent but
itself — measured, both for a typo and for a peer id. So the silent path needs a
credential with no agent identity, one asserting the id in the request body.
That is the multi-user backend shape (one key, many end users), the dashboard's
hand-typed ``filter_agent_id`` box, and every benchmark harness we own. Callers
pass ``None`` for an authenticated identity so the probe stays off the path
where the condition is unreachable.

WHEN THIS RUNS, AND WHAT IT COSTS. Only after the search, and the storage probe
only when the search came back EMPTY. A successful agent-filtered search pays
nothing at all — no extra round trip, no extra query.

That ordering is deliberate and was the second attempt. Probing BEFORE the
search buys one thing: skipping the embedding call on a misconfigured request.
But the saving lands on requests that are already broken, which are rare, while
the cost lands on every healthy one, which is the norm — the wrong way round on
expected cost. (It also, in the first draft, put the short-circuit ahead of
``enforce_fleet_read_many``, which ``test_c27_strict_fleet_scoping`` and
``test_h05_multi_fleet_read_gate`` caught.)

WHICH FACT DECIDES WHAT, AND WHY IT IS NOT THE OBVIOUS ONE. The natural
implementation — look the id up in ``agents``, and if absent report "no such
agent" — is wrong, and wrong in the direction that misleads. ``DELETE
/agents/{id}`` says so itself: *"Delete an agent. Memories written by this agent
are NOT deleted."* The rows stay live and searchable with no agent row, and rows
predating agent tracking were never registered at all. So a missing agent row
means "deregistered or never registered", never "nothing to find".

  * whether the search RETURNED ROWS, plus ``has_memories`` when it did not,
    is what establishes there was nothing to find.
  * ``agent_preexisted`` only chooses the wording.

WHICH FIELD IS EXPLAINED. ``filter_agent_id`` whenever it is set, because that
is the one that becomes ``WHERE memories.agent_id = ?`` and can therefore empty
a result on its own. NOT the resolved identity: ``_resolve_read_identity``
prefers ``caller_agent_id`` over ``filter_agent_id``, so keying on it meant a
caller sending a valid ``caller_agent_id`` beside a typo'd ``filter_agent_id``
got probed on the wrong id — the valid one, which has memories — and the typo
went unreported. Measured on PR #1434:

    filter=TYPO only           -> warnings ['filter_agent_unknown']
    caller=REAL + filter=TYPO  -> warnings []            <-- the gap

``caller_agent_id`` alone is still worth reporting but is a DIFFERENT
phenomenon: it restricts no rows, it only decides which ``scope_agent`` rows
are visible. So it gets its own wording and never claims to have filtered
anything.

HOW FAR ``agent_registered`` CAN BE TRUSTED. Not very, and the wording says so.
``get_or_create_agent`` runs on the READ path and creates a row for whatever id
was asserted, so a search mints agent rows from free-text input. The first
request carrying a typo reports ``filter_agent_unknown`` correctly; that same
request registers the typo, and every later one reports ``filter_agent_empty``
instead. Row existence is therefore evidence of "some request named this id
once", not of a real agent.

Both codes are kept because the first-occurrence signal is accurate and worth
having, but ``filter_agent_empty`` must not read as "registered, so probably
fine" — it names the ambiguity in the message. Pinned by
``test_a_repeated_typo_still_warns_and_never_reads_as_benign``.

The root fix is to stop registering on reads, which is security-adjacent (the
route needs the row for trust-level fleet forcing and
``enforce_fleet_read_many``) and is tracked separately as CAURA-724.

WHERE ``agent_preexisted`` COMES FROM. The route's own
``get_or_create_agent`` call, via its ``registration_ctx`` out-dict. That call
already does the ``agents`` lookup, so this is free — and it has to come from
there rather than from the probe, because by the time the search has run the
row exists whether or not it did beforehand. Asking afterwards would report
every typo as a registered agent. ``None`` means "unknown" (an admin credential
skips ``get_or_create_agent`` entirely), and the probe is then asked for it.
"""

from __future__ import annotations

import logging

from core_api.clients.storage_client import get_storage_client

logger = logging.getLogger(__name__)

# Stable slugs. ``SearchWarning.code`` is documented as a stable slug, so these
# are part of the wire contract once shipped.
FILTER_AGENT_UNKNOWN = "filter_agent_unknown"
FILTER_AGENT_EMPTY = "filter_agent_empty"
FILTER_AGENT_DEREGISTERED = "filter_agent_deregistered"


def _details(agent_id: str, field: str, *, registered: bool) -> dict:
    # ``field`` names WHICH request field the id came from. Hardcoding
    # ``filter_agent_id`` was wrong whenever the id was a ``caller_agent_id``,
    # and a client acting on ``details`` would have corrected the wrong knob.
    return {"field": field, field: agent_id, "agent_registered": registered}


def _deregistered(agent_id: str, field: str, *, had_results: bool) -> dict:
    tail = (
        "Results below are complete."
        if had_results
        else "Its memories are not reachable in the requested scope."
    )
    return {
        "code": FILTER_AGENT_DEREGISTERED,
        "message": (
            f"Agent '{agent_id}' ({field}) has no registration in this tenant but "
            f"memories exist under that id. It was deregistered, or predates agent "
            f"tracking. {tail}"
        ),
        "details": _details(agent_id, field, registered=False),
    }


async def explain_agent_scope(
    *,
    tenant_id: str,
    filter_agent_id: str | None,
    caller_agent_id: str | None,
    had_results: bool,
    agent_preexisted: bool | None,
    preexistence_of: str | None,
    fleet_ids: list[str] | None,
    readable_tenant_ids: list[str] | None,
) -> list[dict]:
    """Warnings explaining an asserted agent scope. ``[]`` when there is nothing to say.

    Both ids are the ones a TENANT-SCOPED caller asserted; pass ``None`` for
    both under an authenticated agent identity, where a wrong id is already a
    403 and there is nothing to add.

    ``filter_agent_id`` wins when both are set: it is the one that restricts
    rows, so it is the one that can empty a result.

    ``agent_preexisted`` is whether an ``agents`` row existed BEFORE this
    request, or ``None`` if unknown. ``preexistence_of`` names WHICH id it
    describes — the route learns it for the resolved identity, which is not
    necessarily the id being explained here, and applying one agent's
    registration state to another is how the first cut of this fix reported a
    typo'd filter as "registered but empty".

    Never raises. A probe failure means we could not explain the result, which
    is exactly today's behaviour — degrading to it is right, and turning a
    working search into a 500 over a diagnostic hint would not be.
    """
    # The row-restricting field first — see WHICH FIELD IS EXPLAINED above.
    agent_id = filter_agent_id or caller_agent_id
    if not agent_id:
        return []
    is_row_filter = bool(filter_agent_id)
    field = "filter_agent_id" if is_row_filter else "caller_agent_id"
    # Only trust the free signal when it is about THIS id. Otherwise fall back
    # to asking storage, which is one query on a path that already returned
    # nothing.
    known_preexisted = agent_preexisted if preexistence_of == agent_id else None

    # Rows came back, so the filter worked. The one thing still worth saying is
    # that the agent behind them is gone — a caller filtering by a dead agent
    # and getting results should know. Costs nothing: no probe on this path.
    if had_results:
        if known_preexisted is False:
            return [_deregistered(agent_id, field, had_results=True)]
        return []

    payload: dict = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        # Only when the route could not tell us — otherwise this repeats the
        # lookup ``get_or_create_agent`` already did, and repeats it too late
        # to be true.
        "include_agent_registered": known_preexisted is None,
    }
    if fleet_ids:
        payload["fleet_ids"] = list(fleet_ids)
    if readable_tenant_ids:
        payload["readable_tenant_ids"] = list(readable_tenant_ids)

    try:
        probe = await get_storage_client().agent_scope_probe(payload)
    except Exception:
        logger.warning(
            "agent scope probe failed; empty result left unexplained (tenant=%s agent=%s)",
            tenant_id,
            agent_id,
            exc_info=True,
        )
        return []

    if not probe:
        return []

    has_memories = bool(probe.get("has_memories"))
    registered = known_preexisted
    if registered is None:
        registered = bool(probe.get("agent_registered"))

    if has_memories:
        # The agent has reachable memories; this query just did not match any.
        # An ordinary empty result — say nothing, unless the agent is gone.
        if not registered:
            return [_deregistered(agent_id, field, had_results=False)]
        return []

    # ``caller_agent_id`` restricts no rows — it only decides which
    # ``scope_agent`` rows are visible — so its wording must not claim to have
    # filtered anything or to have caused this empty result.
    consequence = (
        f"so `{field}` matched nothing"
        if is_row_filter
        else f"so `{field}` grants visibility of no scope_agent rows"
    )
    if registered:
        # NOT a benign "new agent, nothing yet" report. Registration is weak
        # evidence: the read path registers whatever id it is handed, so the
        # second occurrence of a typo lands here rather than in
        # FILTER_AGENT_UNKNOWN below. The message has to carry that, or a
        # caller reads "registered" as "the id is right" and stops looking —
        # which is the state the whole warning exists to prevent. See the
        # module docstring, and CAURA-724 for the root fix.
        return [
            {
                "code": FILTER_AGENT_EMPTY,
                "message": (
                    f"Agent '{agent_id}' is registered in this tenant but has never "
                    f"stored a memory in the requested scope, {consequence}. Registration "
                    "alone does not mean the id is correct: an id first seen on a search "
                    "is registered by that search, so a repeated typo reports here rather "
                    "than as unknown. Check the id before treating this as an empty agent."
                ),
                "details": _details(agent_id, field, registered=True),
            }
        ]
    return [
        {
            "code": FILTER_AGENT_UNKNOWN,
            "message": (
                f"No agent '{agent_id}' and no memories under that id exist in this "
                f"tenant, {consequence}. Check the id for a typo, or whether it belongs "
                "to a different tenant."
            ),
            "details": _details(agent_id, field, registered=False),
        }
    ]
