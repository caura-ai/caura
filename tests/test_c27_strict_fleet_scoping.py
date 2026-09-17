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


def test_every_fleet_scoped_read_goes_through_the_helper():
    """Fails on a COPY of the predicate, not on the leak it later causes.

    Counts hand-rolled ``fleet_id.is_(None)`` disjuncts in the storage service.
    The legitimate uses are single-fleet equality lookups (``fleet_id == x OR
    fleet_id IS NULL`` for a write path), not the multi-fleet READ predicate
    this switch governs — so what is asserted is that no read builds an
    ``in_(fleet_ids)``/``is_(None)`` pair outside the helper.
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
    # definition + the four fleet-scoped reads
    assert src.count("_fleet_scope_clause(") == 5


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
