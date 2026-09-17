"""Lock the tools/list token count so it can't silently regress.

Asserts ``tools/list`` encodes to ≤ ``CEILING_TOKENS`` cl100k tokens.

Skipped if ``tiktoken`` isn't installed (the package is available in this
repo's venv; a dev running the suite without it just skips this gate).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


tiktoken = pytest.importorskip("tiktoken")

FIXTURES = Path(__file__).parent / "fixtures"

# Measured count after adding caura_keystones / caura_keystones_set
# (12 tools, CAURA-000): 4906 cl100k. Was 4796 with the 12 tools before
# Phase B's skills migration trimmed it to 10.
#
# 2026-05-14 (FRICTION-REPORT-V3 D5/D6): 5061 cl100k after expanding the
# keystones descriptions to (a) clarify ``agent_id`` is the TARGET agent
# for ``scope=agent`` and must NOT be passed for tenant/fleet scope, and
# (b) call out the ``rules`` (not ``keystones``) response-key shape. The
# external dev who wrote the v3 friction report lost ~10 minutes total
# to those two misreads; the +155 tokens per session are worth more than
# that across the install base.
#
# 2026-08-25 (C31 / wire-contract D2, ratified): 5296 cl100k after
# ``caura_recall`` gained the formerly REST-only knobs — ``valid_at``,
# ``min_similarity``, ``diagnostic`` — closing the MCP/REST parity gap
# MemoryImpact flagged as "C1+C2 together are a trap". Descriptions are
# already terse; the +96 is structural schema cost (three params × name/
# type/default/title). Ceiling 5200 → 5350 keeps ~50 tokens of headroom
# so the next accidental description bloat still trips this guard.
#
# 2026-08-26: 5321 cl100k (+25) after ``caura_keystones_set`` spelled out
# that the self-author tier needs an EXPLICIT agent_id, and that
# overwriting an existing doc_id also costs whatever its stored shape
# requires. A wet test read the old text as promising trust 1 for any
# "self" rule and filed the resulting 403 as a bug. Ceiling deliberately
# NOT raised — but note this spends half the margin above, leaving ~29.
# Trim before adding, or move the ceiling with a reason of your own.
#
# 2026-09-08: 5341 cl100k (+8) after ``caura_manage`` declared ``bulk_delete``
# and ``lineage`` — ops its handler already accepted and plugin/tools.json
# omitted — and disclosed that both are MCP-only, which is a real distinction
# for plugin callers because that dispatcher implements only the other four.
# Ceiling again NOT raised: the op ROLL-CALL came out of the description to pay
# for it. The ``op`` parameter enumerates all six in the same inputSchema, so
# the list was being bought twice; the surface split is the part no schema
# carries. Note also that the entry above recorded ~29 tokens of margin from
# 5321, and the fixture measured 5333 before this change — something in between
# spent 12 without writing itself down. Measure the fixture, do not trust the
# running total in this list.
#
# 2026-09-08 (carrying out the trim the entry above proposed): 5269 cl100k
# (-72) after removing five verbatim roll-calls — ``op`` from caura_doc and
# caura_keystones_set, ``focus`` from caura_insights, ``outcome_type`` from
# caura_evolve, and ``scope ∈ {...}; weight ∈ {...}`` from caura_keystones_set.
# Each is reproduced word for word by that parameter's own description in the
# same payload, so the list was being bought twice.
#
# CORRECTION to the prediction above, which said 48 (5341 -> 5293). 5293 is
# exactly what FOUR removals give: the note counted specs, and
# caura_keystones_set had two roll-calls, so it missed the 24-token
# scope+weight fragment. Both figures are honest measurements of different
# edits. If you budget from this ledger, count the edits, not the files.
# (Per-tool figures are still not quoted: dropping text moves tokenization
# boundaries, so the parts do not sum to the whole.)
#
# NOT trimmed, deliberately. The ``scope:`` clauses in caura_insights and
# caura_evolve look like roll-calls but carry trust thresholds and the
# "divergence requires fleet/all" / "fleet_id required" constraints, which no
# parameter description holds. And caura_keystones_set's TARGET-agent
# explanation, INVALID_ARGUMENTS consequence and trust-gating paragraph are
# what the 2026-05-14 and 2026-08-26 entries above bought, each after a real
# reader misread the shorter text. A parameter description repeats some of
# that wording — there, the duplication IS the fix, and removing it here
# would quietly reverse those two decisions.
#
# Ceiling 5350 -> 5320. The 2026-08-25 entry chose 5350 to keep ~50 tokens of
# headroom "so the next accidental description bloat still trips this guard";
# at 5269 that headroom is 81 and the guard is looser than it was designed to
# be. Lowering it banks the saving instead of spending it on slack, and the
# next addition still only has to state its reason, as that entry asked.
#
# 2026-09-09 (D16): 5287 cl100k (+18) after ``caura_recall.top_k`` disclosed
# that superseded hits pull their newest correction in BEYOND the cap, marked
# ``injected:true``. The old text promised a maximum ("Max results") that
# successor injection has violated since A34 — a caller reading it could not
# explain a 10-item answer to a top_k=5 call. Ceiling NOT raised; 33 remain.
CEILING_TOKENS = 5320


def _count(path: Path) -> int:
    enc = tiktoken.get_encoding("cl100k_base")
    data = json.loads(path.read_text())
    return len(enc.encode(json.dumps(data, separators=(",", ":"))))


def test_tokens_under_ceiling():
    tokens = _count(FIXTURES / "tools_list_baseline_v1.json")
    assert tokens <= CEILING_TOKENS, (
        f"tools/list is {tokens} cl100k tokens — over the {CEILING_TOKENS} "
        "ceiling. If the growth is intentional, raise CEILING_TOKENS in "
        "tests/test_mcp_token_budget.py and document the reason."
    )


@pytest.mark.asyncio
async def test_v1_baseline_matches_live_registry():
    """Guard against a stale baseline fixture — regenerate if this fails."""
    from core_api import mcp_server

    tools = await mcp_server.mcp.list_tools()
    live = []
    for t in tools:
        # ``by_alias=True`` is what the SDK serializes onto the wire. Without it
        # this asserted on the model's Python field names, which silently became
        # snake_case in mcp 2.x — the guard would fail on an internal rename
        # while a genuine wire change slipped through.
        d = (
            t.model_dump(mode="json", by_alias=True)
            if hasattr(t, "model_dump")
            else dict(t.__dict__)
        )
        live.append(d)
    live.sort(key=lambda x: x["name"])

    baseline = json.loads((FIXTURES / "tools_list_baseline_v1.json").read_text())
    baseline.sort(key=lambda x: x["name"])
    assert live == baseline, (
        "tools/list output has drifted from tools_list_baseline_v1.json. "
        "If intentional, regenerate the fixture via the snippet in "
        "tests/fixtures/README.md (or scripts/export_tool_specs.py + the "
        "live capture script)."
    )
