"""C27 — opt-in strict fleet scoping.

Wire contract D4 (RATIFIED) defines a NULL ``fleet_id`` as tenant-shared BY
DESIGN: a row written without a fleet is readable by every fleet in the tenant.
This is NOT a bug being fixed. It is an opt-in mode for tenants that want hard
fleet isolation, and the default must stay permissive — flipping it would
retroactively hide rows that tenants deliberately wrote as shared.

Two things are easy to get wrong here and both are pinned below.

**The predicate must live in ONE place.** A54 is the precedent: the identical
visibility clause was copied across several candidate queries, one copy was
fixed as filed, and the live leak simply reproduced through the next copy. Any
query that hand-rolls ``fleet_id.is_(None)`` is outside the switch and silently
permissive, so ``test_no_query_builds_the_fleet_predicate_by_hand`` fails on the
copy rather than waiting for the leak.

**``scope_org`` survives in both modes.** It is a visibility TIER a writer chose
explicitly, not an accident of a missing fleet. A tenant asking for fleet
isolation is not asking to revoke org-wide sharing, and folding the two together
would make one switch mean two things.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def _sql(strict: bool, include_org: bool = True, entity: bool = False) -> str:
    """Compile the real clause to SQL text.

    Deliberately compiled rather than asserted against fake column objects: the
    thing that matters is the predicate Postgres actually receives, and a stub
    that merely records method calls would keep passing if the helper stopped
    returning a usable clause at all.
    """
    from common.models.entity import Entity
    from common.models.memory import Memory
    from core_storage_api.services.postgres_service import _fleet_scope_clause

    model = Entity if entity else Memory
    clause = _fleet_scope_clause(
        model, ["f1"], strict=strict, include_org_visibility=include_org
    )
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


# ── the switch ────────────────────────────────────────────────────────────


def test_default_keeps_null_fleet_rows_visible():
    """Contract D4. The permissive branch is the DEFAULT, not a legacy path."""
    sql = _sql(strict=False)
    assert "IS NULL" in sql, (
        "default scoping dropped tenant-shared rows — this silently hides data "
        "written fleet-less on purpose"
    )


def test_strict_drops_only_the_null_fleet_disjunct():
    sql = _sql(strict=True)
    assert "IS NULL" not in sql
    assert "f1" in sql and "IN " in sql, "strict mode dropped the fleet match itself"


def test_scope_org_survives_strict_mode():
    """The switch means "stop inheriting null-fleet rows", not "revoke org-wide
    visibility". Conflating them would make one flag do two jobs."""
    assert "scope_org" in _sql(strict=True)
    assert "scope_org" in _sql(strict=False)


def test_entities_have_no_visibility_column_and_say_so():
    """``Entity`` carries no ``visibility``; asking for it would raise rather
    than quietly widen. The caller opts out explicitly."""
    sql = _sql(strict=True, include_org=False, entity=True)
    assert "scope_org" not in sql and "visibility" not in sql
    assert "IS NULL" not in sql and "f1" in sql


# ── the A54 lesson: one predicate, one place ──────────────────────────────


def test_no_query_builds_the_fleet_predicate_by_hand():
    """Fails on a COPY of the predicate, not on the leak it later causes.

    The legitimate uses are single-fleet equality lookups (``fleet_id == x OR
    fleet_id IS NULL`` for a write path), not the multi-fleet READ predicate
    this switch governs — so what is asserted is that no read builds an
    ``in_(fleet_ids)``/``is_(None)`` pair outside the helper.

    This one catches a predicate written out longhand. Its sibling below,
    ``test_every_fleet_scoped_read_routes_through_the_helper_with_its_own_arguments``,
    catches the other direction: a read that uses the helper but with the wrong
    arguments for its family. Neither subsumes the other.
    """
    from core_storage_api.services import postgres_service as ps

    src = inspect.getsource(ps)
    # every place fleet_ids is turned into a disjunction
    # The ONLY legitimate ``in_(fleet_ids)`` is the one inside the helper.
    hand_rolled = src.count("fleet_id.in_(fleet_ids)")
    assert hand_rolled == 1, (
        f"{hand_rolled} sites build the multi-fleet predicate inline (expected only "
        "the one inside _fleet_scope_clause); an inline copy sits outside the "
        "strict switch and is silently permissive — this is exactly how A54 leaked"
    )


# Every read that scopes by fleet, and the arguments each one must pass.
#
# TWO FAMILIES, and the difference between them is the whole point:
#
# * SCOPE reads take a caller's authorization scope — a plural ``fleet_ids``
#   resolved upstream — and apply D4 as written: null-fleet rows are
#   tenant-shared, ``scope_org`` survives, and ``strict`` comes from the tenant
#   switch.
# * FILTER reads take ONE caller-supplied ``fleet_id`` that core-api sometimes
#   overloads as a security PIN (``resolve_read_fleet_gate`` case (a) pins it
#   for a trust < 2 caller asking ``scope='fleet'``). Storage cannot tell a
#   filter from a pin by the value alone, so these stay strict and org-blind —
#   which is exactly the predicate they used before C27 centralised them.
#
# Relaxing a FILTER read toward the SCOPE defaults is a privilege escalation,
# not a tidy-up: ``visibility = 'scope_org'`` carries no fleet term, so a
# pinned trust-1 caller would see every other fleet's org-wide rows. It is also
# invisible to the contract gate — semantic change, no schema movement, so the
# broker baseline does not regenerate and oasdiff reports nothing (ax-0917-m-19).
# ``strict=strict_fleet_scoping`` — the tenant switch, an expression rather than
# a constant, so the AST reader reports this sentinel instead of a value.
FROM_TENANT_SWITCH = object()

FLEET_SCOPED_READS: dict[str, dict[str, object]] = {
    # SCOPE reads — D4 applies and ``strict`` is the tenant's choice.
    "memory_scored_search": {"strict": FROM_TENANT_SWITCH},
    "memory_load_by_ids": {"strict": FROM_TENANT_SWITCH},
    "memory_find_successors": {"strict": FROM_TENANT_SWITCH},
    # A scope read that is nonetheless org-blind, for a STRUCTURAL reason and
    # not a pin: it scopes ``Entity``, which has no ``visibility`` column, so
    # there is no org tier to preserve. Worth stating, because the argument list
    # otherwise looks like the pinned family below and invites the wrong
    # conclusion about why.
    "entity_fts_search": {
        "strict": FROM_TENANT_SWITCH,
        "include_org_visibility": False,
    },
    # FILTER reads — pinned, and the values are the assertion.
    "memory_list_by_filters": {"strict": True, "include_org_visibility": False},
    "memory_stats_breakdown": {"strict": True, "include_org_visibility": False},
    "memory_quality_metrics": {"strict": True, "include_org_visibility": False},
}

PINNED_FILTER_READS = {
    "memory_list_by_filters",
    "memory_stats_breakdown",
    "memory_quality_metrics",
}


def _helper_calls_by_function():
    """``{function name: [ {kwarg: literal-or-None}, ... ]}`` for every
    ``_fleet_scope_clause`` call in the storage service, read from the AST.

    Deliberately NOT a ``src.count(...)``. The previous version of this guard
    asserted the call sites numbered five, which pinned the incompleteness
    rather than catching it: it passed while two fleet-scoped reads hand-rolled
    the predicate, and it would have gone green again for the wrong reason the
    moment anyone raised the count. What matters is WHICH reads route through
    the helper and WITH WHAT — so that is what is read.
    """
    import ast

    from core_storage_api.services import postgres_service as ps

    tree = ast.parse(inspect.getsource(ps))
    found: dict[str, list[dict[str, object]]] = {}

    def visit(node, fn_name):
        for child in ast.iter_child_nodes(node):
            name = fn_name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Name)
                and child.func.id == "_fleet_scope_clause"
                and fn_name is not None
            ):
                kwargs: dict[str, object] = {}
                for kw in child.keywords:
                    try:
                        kwargs[kw.arg] = ast.literal_eval(kw.value)
                    except ValueError:
                        kwargs[kw.arg] = FROM_TENANT_SWITCH
                found.setdefault(fn_name, []).append(kwargs)
            visit(child, name)

    visit(tree, None)
    return found


def test_every_fleet_scoped_read_routes_through_the_helper_with_its_own_arguments():
    """Enumerates the reads instead of counting them, and pins their arguments.

    Catches three different regressions: a new fleet-scoped read that hand-rolls
    the predicate (absent from the AST), a declared read that stops using the
    helper, and — the one that matters most — a pinned filter read quietly
    relaxed toward the permissive defaults.
    """
    calls = _helper_calls_by_function()
    # The helper's own definition is not a call site.
    declared = set(FLEET_SCOPED_READS)

    assert set(calls) == declared, (
        f"fleet-scoped reads changed: unexpected {sorted(set(calls) - declared)}, "
        f"missing {sorted(declared - set(calls))}. Add the read here WITH the "
        "arguments it must pass — a read absent from this map is a read nobody "
        "has decided the scoping rule for."
    )

    for fn, expected_kwargs in FLEET_SCOPED_READS.items():
        for kwargs in calls[fn]:
            assert kwargs == expected_kwargs, (
                f"{fn} calls the helper with {kwargs}, expected {expected_kwargs}. "
                "If that is deliberate, change the entry here and say why — the "
                "arguments ARE the scoping rule, so a silent change to them is a "
                "silent change to who can read what."
            )

    for fn in PINNED_FILTER_READS:
        for kwargs in calls[fn]:
            assert (
                kwargs.get("strict") is True
                and kwargs.get("include_org_visibility") is False
            ), (
                f"{fn} is a PINNED read: core-api uses its fleet_id as a security "
                "confinement for trust < 2 callers, so it must stay "
                "strict=True, include_org_visibility=False. Relaxing it lets a "
                "pinned caller read other fleets' scope_org rows, and no contract "
                "gate will tell you (ax-0917-m-19)."
            )


@pytest.mark.parametrize(
    "fn",
    [
        "memory_scored_search",
        "memory_load_by_ids",
        "memory_find_successors",
        "entity_fts_search",
    ],
)
def test_storage_reads_accept_the_flag_and_default_permissive(fn):
    """Optional with a permissive default at the storage boundary, so a storage
    instance deployed AHEAD of core-api keeps serving callers that don't send
    it — the same independence A54's params were given."""
    from core_storage_api.services.postgres_service import PostgresService

    sig = inspect.signature(getattr(PostgresService, fn))
    assert "strict_fleet_scoping" in sig.parameters, f"{fn} cannot be scoped strictly"
    assert sig.parameters["strict_fleet_scoping"].default is False


# ── the tenant switch ─────────────────────────────────────────────────────


def test_setting_defaults_off():
    """Turning this on HIDES rows that are visible today, so it is a decision a
    tenant makes rather than one inherited from a deploy."""
    from core_api.services.organization_settings import ResolvedConfig

    assert ResolvedConfig(tenant_settings={}).strict_fleet_scoping is False


def test_setting_is_readable_when_set():
    from core_api.services.organization_settings import ResolvedConfig

    cfg = ResolvedConfig(tenant_settings={"search": {"strict_fleet_scoping": True}})
    assert cfg.strict_fleet_scoping is True


def test_setting_is_a_declared_writable_key():
    """Undeclared keys are rejected on write, so without this the switch could
    be read but never turned on."""
    from core_api.services.organization_settings import _LEAF_TYPES

    assert _LEAF_TYPES["search.strict_fleet_scoping"] is bool
