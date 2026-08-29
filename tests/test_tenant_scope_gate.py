"""The CI gate that keeps the storage tenancy invariant checkable.

The script lives at ``scripts/tenant_scope_gate.py``. It enumerates every
storage route and every public ``PostgresService`` method, and fails when one
that takes no binding tenant scope is not accounted for in
``core-storage-api/tenant_scope_allowlist.json``.

What these tests are for: a gate is only worth having if it FAILS on the thing
it claims to catch, and most of the ways this one could rot are silent. It could
enumerate fewer routes than the app serves, classify a guarded route as unscoped
(or, far worse, an unscoped one as guarded), or let the allowlist grow while
still reporting green. Each of those gets a case below that injects the fault
and asserts the gate notices.

``test_trunk_is_green`` is the other half: the allowlist ships seeded, so the
gate must pass on the tree as committed. A red gate on trunk is a gate somebody
deletes on a Friday.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import typing
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "tenant_scope_gate.py"
ALLOWLIST = REPO_ROOT / "core-storage-api" / "tenant_scope_allowlist.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import tenant_scope_gate as gate


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _classify(source: str) -> tuple[str, str]:
    """Classify a single handler written as source, the way the gate does."""
    node = ast.parse(source).body[0]
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    return gate._classify_handler(node)


# ---------------------------------------------------------------------------
# The gate holds on the tree as committed
# ---------------------------------------------------------------------------


def test_trunk_is_green() -> None:
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_allowlist_entry_claims_a_known_category() -> None:
    """An unclassified entry is an unreviewed one, and the gate says so.

    Asserted here as well as in the gate so the failure names the entry when a
    regeneration adds a row and nobody fills the category in.
    """
    entries = json.loads(ALLOWLIST.read_text())["exceptions"]
    unclassified = [e["id"] for e in entries if e.get("category") not in gate.CATEGORIES]
    assert not unclassified, f"entries with no valid category: {unclassified}"


def test_the_file_documents_every_category_its_entries_claim() -> None:
    """The reader of the JSON alone must be able to look up any category it uses.

    This is the property the gate lost for a round: a category was added to the
    script and one entry hand-edited to claim it, so the file's own
    ``_categories`` explained six of the seven categories in use.
    """
    doc = json.loads(ALLOWLIST.read_text())
    used = {e["category"] for e in doc["exceptions"]}
    assert used - set(doc["_categories"]) == set(), "entries claim categories the file never explains"


def test_category_doc_drift_is_caught(tmp_path: Path) -> None:
    """Each way ``_categories`` can diverge from the script is an error.

    Dropping a key is what actually happened; the other two are the same defect
    seen from the other side, and are cheap to hold once the check is equality.
    """
    documented = dict(gate.CATEGORIES)
    path = tmp_path / "allowlist.json"

    def write(categories: dict[str, str]) -> None:
        path.write_text(json.dumps({"_categories": categories, "exceptions": []}))

    write(documented)
    assert gate.check_category_doc(path) == [], "the generated block must be accepted"

    dropped = dict(documented)
    del dropped["grant-in-lieu-of-tenant"]
    write(dropped)
    assert "missing grant-in-lieu-of-tenant" in "".join(gate.check_category_doc(path))

    write({**documented, "invented-offline": "added to the JSON but not the script"})
    assert "unknown invented-offline" in "".join(gate.check_category_doc(path))

    write({**documented, "id-addressed-read": "a description edited in the JSON, not the script"})
    assert "reworded id-addressed-read" in "".join(gate.check_category_doc(path))


def test_a_plural_tenant_list_is_not_a_binding_scope() -> None:
    """``tenant_ids`` names which tenants to span; it does not confine to one.

    The gate cannot see whether such a list was derived from a verified
    caller-tenant relationship or taken verbatim from the body, and the live
    instance is the unsafe one: ``POST /tenant-usage/query`` forwards
    ``body.tenant_ids`` straight through to the query. Crediting it is a false
    REQUIRED, the direction the gate must never be wrong in.

    Asserted through the real enumeration against the real service, not a
    synthetic class — classifying a stand-in here would mean reimplementing the
    rule under test and asserting the copy agrees with itself.
    """
    assert "tenant_ids" not in gate.BINDING_SCOPE

    entries = {e.key: e for e in gate.enumerate_methods()}
    assert entries["tenant_usage_query"].verdict == "NONE"

    listed = {e["id"]: e for e in json.loads(ALLOWLIST.read_text())["exceptions"]}
    assert listed["method:tenant_usage_query"]["category"] == "grant-in-lieu-of-tenant"
    assert listed["route:POST /api/v1/storage/tenant-usage/query"]["category"] == "grant-in-lieu-of-tenant"


def test_the_id_addressed_backlog_is_split_by_blast_radius() -> None:
    """Reads and writes by bare UUID are not the same finding and are not filed as one.

    The flat category let "another tenant's row can be DELETED by UUID" sit in
    the same count as "another tenant's row can be READ by UUID". The split is
    the forcing function: the mutating half is what gets fixed first.
    """
    listed = json.loads(ALLOWLIST.read_text())["exceptions"]
    used = {e["category"] for e in listed}
    assert "id-addressed" not in used, "the flat category was retired; reclassify the entry"
    assert {"id-addressed-write", "id-addressed-read"} <= set(gate.CATEGORIES)
    assert gate.DESTRUCTIVE_CATEGORIES <= gate.BACKLOG_CATEGORIES

    writes = [e for e in listed if e["category"] == "id-addressed-write"]
    assert writes, "an empty write backlog means the split silently collapsed"


def test_a_multi_line_error_survives_as_one_annotation() -> None:
    """``::error::`` is line-based, and the remedy is always on line two onward.

    Every multi-line message here puts the diagnosis first and what to do about
    it below. Emitted raw, GitHub keeps the first line as the annotation and
    spills the rest into the log, dropping exactly the actionable half.
    """
    encoded = gate._as_annotation("the allowlist grew:\n      + method:x\n    Scope it.")
    assert "\n" not in encoded
    assert encoded.count("%0A") == 2
    assert "method:x" in encoded


def test_annotation_encoding_does_not_eat_its_own_escapes() -> None:
    """A literal ``%`` must not turn a following ``%0A`` into rendered text."""
    encoded = gate._as_annotation("100% of routes\nsecond line")
    assert encoded == "100%25 of routes%0Asecond line"


def test_every_gate_error_is_emitted_as_a_single_line(capsys: pytest.CaptureFixture) -> None:
    """End-to-end: nothing reaches a workflow command with a raw newline in it."""
    entry = gate.Entry("method", "brand_new_unscoped", "NONE", "no binding tenant parameter")
    errors = gate.check([entry], [], {})
    assert errors and any("\n" in e for e in errors), "expected a multi-line error to test"

    for err in errors:
        line = f"::error::{gate._as_annotation(err)}"
        assert line.count("\n") == 0


def test_duplicate_allowlist_ids_are_rejected(tmp_path: Path) -> None:
    """Two rows for one path is a silent, order-dependent choice between them.

    The dict comprehension kept whichever came last. That is also a way past the
    relabel guard: append a second copy of a mutating entry carrying a milder
    category, and the lookup returns the milder one while the row still reads as
    unchanged.
    """
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "exceptions": [
                    {"id": "method:x", "verdict": "NONE", "category": "id-addressed-write"},
                    {"id": "method:x", "verdict": "NONE", "category": "no-tenant-data"},
                ]
            }
        )
    )
    with pytest.raises(gate.AllowlistError) as excinfo:
        gate.load_allowlist(path)
    assert "method:x" in str(excinfo.value)


def test_every_owned_entry_carries_a_tracked_issue() -> None:
    """The owned backlog records who is accountable, and the file holds it.

    A category says what the debt is. The issue is where paying it gets argued
    and closed, so a note that loses its reference turns a tracked item back
    into a line in a JSON file nobody is accountable for.
    """
    listed = json.loads(ALLOWLIST.read_text())["exceptions"]
    owned = [e for e in listed if e["category"] in gate.TRACKED_CATEGORIES]
    assert owned, "expected a non-empty owned backlog"
    untracked = [e["id"] for e in owned if not gate.ISSUE_REF.search(e.get("note") or "")]
    assert not untracked, f"owned entries with no tracked issue: {untracked}"


def test_owning_a_category_is_not_the_same_question_as_mutating() -> None:
    """The two sets differ on purpose, and the difference is load-bearing.

    ``grant-in-lieu-of-tenant`` destroys nothing, so it does not belong under a
    name meaning "mutates" — but the caller names its own scope against a
    service that authenticates nothing, so it needs an owner just as much.
    Answering "needs an owner" with "mutates" is what left it unowned.
    """
    assert gate.DESTRUCTIVE_CATEGORIES < gate.TRACKED_CATEGORIES
    assert "grant-in-lieu-of-tenant" in gate.TRACKED_CATEGORIES
    assert "grant-in-lieu-of-tenant" not in gate.DESTRUCTIVE_CATEGORIES
    # The bulk read backlog is deliberately unowned: 22 issues nobody reads is
    # not accountability.
    assert "id-addressed-read" not in gate.TRACKED_CATEGORIES
    # Keeps the parametrised list below honest without making it read the
    # constant at collection time, which would make this module uncollectible
    # against any gate that predates it rather than just failing these tests.
    assert set(_TRACKED) == gate.TRACKED_CATEGORIES


_TRACKED = ("id-addressed-write", "grant-in-lieu-of-tenant")


@pytest.mark.parametrize("category", _TRACKED)
def test_an_owned_entry_without_an_issue_is_rejected(category: str) -> None:
    """And the gate enforces it rather than trusting the file to stay right."""
    entry = gate.Entry("method", "some_unscoped_path", "NONE", "no binding tenant parameter")
    listed = {
        "method:some_unscoped_path": {
            "id": "method:some_unscoped_path",
            "verdict": "NONE",
            "category": category,
            "note": "no reference here",
        }
    }
    errors = gate.check([entry], [], listed)
    assert any("no tracked issue" in e for e in errors), f"{category} was not required to name one"

    listed["method:some_unscoped_path"]["note"] = "Tracked in #1234."
    assert gate.check([entry], [], listed) == []


def test_the_committed_allowlist_has_no_duplicates() -> None:
    """The file as committed, not a fixture."""
    ids = [e["id"] for e in json.loads(ALLOWLIST.read_text())["exceptions"]]
    assert len(ids) == len(set(ids))


def test_allowlist_holds_only_paths_that_still_exist() -> None:
    """No stale rows: every entry corresponds to something the gate enumerated."""
    live = {e.ident for e in gate.exceptions(gate.enumerate_methods() + gate.enumerate_routes())}
    listed = {e["id"] for e in json.loads(ALLOWLIST.read_text())["exceptions"]}
    assert listed - live == set(), f"allowlisted but no longer unscoped: {sorted(listed - live)}"


# ---------------------------------------------------------------------------
# It fails on what it claims to catch
# ---------------------------------------------------------------------------


def test_a_new_unscoped_method_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The GHSA-wgvw shape: an id-addressed read with no tenant parameter."""
    extra = gate.Entry("method", "memory_get_everything_by_id", "NONE", "no binding tenant parameter")
    errors = gate.check([extra], [], _seeded_allowlist())
    assert any("memory_get_everything_by_id" in e for e in errors)


def test_an_unscoped_method_added_to_the_real_class_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end over the real class: enumerate, classify, then fail.

    The cases around this one feed synthetic entries straight to ``check``,
    which proves the accounting but not that enumeration would have found the
    method in the first place. Attaching one to ``PostgresService`` exercises
    the whole chain — and the chain is where a silent hole would be, since a
    method the enumerator never yields is a method the gate never objects to.
    """
    from core_storage_api.services.postgres_service import PostgresService

    async def memory_get_everything_by_id(self, *, memory_id: str) -> None:  # type: ignore[no-untyped-def]
        """An id-addressed read with no tenant predicate."""

    monkeypatch.setattr(
        PostgresService, "memory_get_everything_by_id", memory_get_everything_by_id, raising=False
    )

    entries = _live_entries()
    injected = [e for e in entries if e.key == "memory_get_everything_by_id"]
    assert injected, "enumeration missed a method added to PostgresService"
    assert injected[0].verdict == "NONE"

    errors = gate.check(entries, [], _seeded_allowlist())
    assert any("memory_get_everything_by_id" in e for e in errors)


def test_a_scoped_method_added_to_the_real_class_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction: a properly scoped addition needs no allowlist line.

    Without this, a gate that simply failed on every new method would pass every
    test above while making the allowlist grow forever.
    """
    from core_storage_api.services.postgres_service import PostgresService

    async def memory_get_scoped(self, *, memory_id: str, tenant_id: str) -> None:  # type: ignore[no-untyped-def]
        """An id-addressed read that binds the tenant."""

    monkeypatch.setattr(PostgresService, "memory_get_scoped", memory_get_scoped, raising=False)

    entries = _live_entries()
    injected = [e for e in entries if e.key == "memory_get_scoped"]
    assert injected and injected[0].verdict == "REQUIRED"
    assert gate.check(entries, [], _seeded_allowlist()) == []


def test_a_new_unscoped_route_fails() -> None:
    extra = gate.Entry("route", "GET /api/v1/storage/leak/{row_id}", "NONE", "no binding tenant read")
    errors = gate.check([extra], [], _seeded_allowlist())
    assert any("leak/{row_id}" in e for e in errors)


def test_an_allowlisted_entry_with_no_category_fails() -> None:
    entry = gate.Entry("method", "some_method", "NONE", "no binding tenant parameter")
    errors = gate.check([entry], [], {"method:some_method": {"id": "method:some_method", "category": ""}})
    assert any("no category" in e for e in errors)


def test_an_allowlisted_entry_with_an_invented_category_fails() -> None:
    """The closed set is the thing that keeps the list readable at ~120 rows."""
    entry = gate.Entry("method", "some_method", "NONE", "no binding tenant parameter")
    errors = gate.check(
        [entry], [], {"method:some_method": {"id": "method:some_method", "category": "its-fine-honest"}}
    )
    assert any("unknown category" in e for e in errors)


def test_a_stale_allowlist_entry_fails() -> None:
    """An entry for something now scoped must be deleted, not left to rot.

    Without this the list only ever grows in practice: nobody removes a row
    when they fix the path it describes, and the count stops meaning anything.
    """
    errors = gate.check([], [], {"method:long_since_fixed": {"category": "id-addressed-read"}})
    assert any("no longer needs to be" in e for e in errors)


def test_an_allowlisted_entry_that_lost_scope_fails() -> None:
    """Staying on the list is not permission to get worse.

    OPTIONAL still scopes a caller that passes the tenant; NONE cannot be
    scoped at all. The identifier is the same either way, so comparing id sets
    — which is all the first version of this gate did — reports green while a
    path already granted an exception quietly stops being scopeable.
    """
    entry = gate.Entry("method", "memory_admin_list", "NONE", "no binding tenant parameter")
    errors = gate.check(
        [entry],
        [],
        {"method:memory_admin_list": {"verdict": "OPTIONAL", "category": "admin-unscoped"}},
    )
    assert any("recorded as OPTIONAL and is now NONE" in e for e in errors)


def test_an_unchanged_verdict_is_quiet() -> None:
    entry = gate.Entry("method", "memory_admin_list", "OPTIONAL", "tenant_id is defaulted")
    errors = gate.check(
        [entry],
        [],
        {"method:memory_admin_list": {"verdict": "OPTIONAL", "category": "admin-unscoped"}},
    )
    assert errors == []


def test_a_widening_grant_without_a_binding_scope_fails() -> None:
    """``readable_tenant_ids`` is only safe to omit when there is a fallback.

    All eleven call sites today pair it with ``tenant_id``, so the ``else``
    branch narrows to one tenant. A twelfth without that pairing would make
    omitting the grant an unscoped read, which is the direction that matters.
    """
    grant = gate.Entry("grant", "memory_search_everywhere", "NONE", "takes readable_tenant_ids with no binding tenant")
    errors = gate.check([], [grant], {})
    assert any("widening grant" in e for e in errors)


# ---------------------------------------------------------------------------
# The classifier reads the idioms the routers actually use
# ---------------------------------------------------------------------------


def test_require_helper_is_required() -> None:
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body = await request.json()\n"
        "    tenant_id = _require(body, 'tenant_id')\n"
    )
    assert verdict == "REQUIRED"


def test_every_fail_closed_guard_exists_and_raises() -> None:
    """The trusted-guard list must describe functions that are really there.

    A name in ``FAIL_CLOSED_GUARDS`` is a claim that calling it proves the route
    rejects a missing tenant. Renaming a helper, or softening one so it returns
    instead of raising, would leave the claim standing over code that no longer
    backs it — and the gate would keep reading REQUIRED off it.
    """
    import ast as _ast

    source = (
        REPO_ROOT / "core-storage-api" / "src" / "core_storage_api" / "routers" / "_validation.py"
    ).read_text()
    defined = {
        n.name: n
        for n in _ast.parse(source).body
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
    }
    for name in gate.FAIL_CLOSED_GUARDS:
        assert name in defined, f"{name} is trusted as a guard but is not defined in _validation.py"
        assert any(isinstance(s, _ast.Raise) for s in _ast.walk(defined[name])), (
            f"{name} is trusted as a fail-closed guard but never raises"
        )


def test_a_lookalike_helper_is_not_trusted_as_a_guard() -> None:
    """``_require_if_present`` starts with ``_require`` and promises the opposite.

    The prefix match this replaced would have taken any such name as proof of
    scoping — silently, and in the direction that removes the route from the
    allowlist rather than adding it.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    _require_if_present(body, 'tenant_id')\n"
        "    return await svc.by_ids(body['ids'])\n"
    )
    assert verdict == "NONE"


def test_subscript_is_required() -> None:
    verdict, _ = _classify("async def h(request):\n    body = await request.json()\n    t = body['tenant_id']\n")
    assert verdict == "REQUIRED"


def test_bare_get_is_optional() -> None:
    """``bulk-get``'s shape, and the reason this verdict exists at all."""
    verdict, detail = _classify(
        "async def h(request):\n"
        "    body = await request.json()\n"
        "    tenant_filter = body.get('tenant_id')\n"
        "    return await svc.by_ids(body['ids'])\n"
    )
    assert verdict == "OPTIONAL"
    assert "never rejects" in detail


def test_inline_guard_after_get_is_required() -> None:
    """``purge_tenant_data``'s shape: ``.get`` then an explicit reject.

    Reading only ``_require`` classified two dozen correctly-guarded routes as
    exceptions. That is not a safe direction to be wrong in either — an
    allowlist padded with false positives is one reviewers skim.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if not isinstance(tenant_id, str) or not tenant_id:\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
    )
    assert verdict == "REQUIRED"


def test_a_raise_nested_under_a_second_condition_is_not_a_guard() -> None:
    """A missing tenant must reach the raise for the raise to be a guard.

    ``if tenant_id: if something_else: raise`` mentions the tenant and contains
    a raise, so searching the whole subtree credited it — but a request with no
    tenant takes neither branch. Crediting it removes the route from the
    allowlist, which is the invisible direction.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if tenant_id:\n"
        "        if something_else:\n"
        "            raise HTTPException(status_code=400, detail='unrelated')\n"
    )
    assert verdict == "OPTIONAL"


def test_a_guard_that_only_fires_when_the_tenant_is_present_is_not_a_guard() -> None:
    """``if tenant_id and flag: raise`` raises when the tenant IS there.

    The question is not whether the tenant appears in the condition but whether
    a request WITHOUT one reaches the raise. Here it does not — the `and`
    short-circuits — so crediting it marks an unscoped route REQUIRED.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if tenant_id and other_flag:\n"
        "        raise HTTPException(status_code=400, detail='unrelated')\n"
    )
    assert verdict == "OPTIONAL"


def test_an_and_guard_with_a_non_scope_branch_is_not_a_guard() -> None:
    """``if not tenant_id and unrelated: raise`` — absent plus a false flag passes."""
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if not tenant_id and unrelated_flag:\n"
        "        raise HTTPException(status_code=400, detail='unrelated')\n"
    )
    assert verdict == "OPTIONAL"


def test_an_and_guard_over_two_binding_keys_is_a_guard() -> None:
    """``if not tenant_id and not org_id: raise`` — neither present reaches the raise.

    The one shape the ``and`` branch does credit: every operand is itself a
    negative check on a binding key, so the test is false exactly when at least
    one is present.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    org_id = body.get('org_id')\n"
        "    if not tenant_id and not org_id:\n"
        "        raise HTTPException(status_code=400, detail='need one')\n"
    )
    assert verdict == "REQUIRED"


def test_the_widening_grant_does_not_stand_in_for_a_binding_scope() -> None:
    """``if not tenant_id and not readable_tenant_ids: raise`` is NOT a guard.

    Structurally identical to the test above, and deliberately classified the
    other way: ``readable_tenant_ids`` is the widening grant, supplied verbatim
    by an unauthenticated caller, so satisfying the guard with it alone proves
    nothing about entitlement. Crediting this shape would let the weaker of the
    two satisfy the gate — a false REQUIRED, the invisible direction — so it
    falls through to OPTIONAL and is carried explicitly under
    ``grant-in-lieu-of-tenant``. Pinned because the pair reads as symmetric and
    is not.
    """
    verdict, evidence = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    readable_tenant_ids = body.get('readable_tenant_ids')\n"
        "    if not tenant_id and not readable_tenant_ids:\n"
        "        raise HTTPException(status_code=400, detail='need one')\n"
    )
    assert verdict == "OPTIONAL", evidence


def test_a_compound_or_guard_is_still_a_guard() -> None:
    """The dominant real idiom, at eight sites — rejecting every BoolOp breaks it.

    ``or`` is true if any branch is, so one negative check on the tenant carries
    the whole test. Compoundness is not what separates a guard from a non-guard;
    polarity is.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if not isinstance(tenant_id, str) or not tenant_id:\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
    )
    assert verdict == "REQUIRED"


def test_a_pair_check_against_the_body_tenant_is_a_guard() -> None:
    """``node.tenant_id != body.get("tenant_id")`` — GHSA-xw4x's own fix.

    An absent tenant reads as None, which differs from the row's real tenant,
    so the raise IS reached. Handling only ``is None`` / ``== None`` would have
    scored the endpoint that closed that advisory as unscoped.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    node = await svc.get_node(body['node_id'])\n"
        "    if node is None or node.tenant_id != body.get('tenant_id'):\n"
        "        raise HTTPException(status_code=404, detail='Node not found')\n"
    )
    assert verdict == "REQUIRED"


def test_a_local_that_merely_shares_the_name_is_not_the_request() -> None:
    """A ``tenant_id`` assigned from a config default is not what the caller sent."""
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = DEFAULT_TENANT\n"
        "    if not tenant_id:\n"
        "        raise HTTPException(status_code=400, detail='misconfigured')\n"
        "    return await svc.by_ids(body['ids'])\n"
    )
    assert verdict == "NONE"


def test_a_scope_popped_from_the_body_is_still_read_from_the_request() -> None:
    """``update_memory`` pops the tenant so it cannot reach the column update.

    Removing the key afterwards does not make it less of a read of the request.
    """
    verdict, _ = _classify(
        "async def h(memory_id, request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.pop('tenant_id', None)\n"
        "    if not tenant_id:\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
    )
    assert verdict == "REQUIRED"


def test_a_guard_is_read_against_the_binding_in_effect_where_it_stands() -> None:
    """A name bound to the tenant LATER cannot credit an EARLIER guard.

    Python has no block scope, so a flat name-to-key map made
    ``value = compute()`` / ``if not value: raise`` / ``value =
    body.get("tenant_id")`` read as a tenant guard — the guard was checking
    something else entirely. Resolving each guard against the last binding
    before its line is what separates them.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    value = compute_something()\n"
        "    if not value:\n"
        "        raise HTTPException(status_code=400, detail='unrelated')\n"
        "    value = body.get('tenant_id')\n"
        "    return await svc.by_ids(body['ids'], value)\n"
    )
    assert verdict == "OPTIONAL"


def test_a_guard_after_the_binding_still_counts() -> None:
    """The ordinary order — bind, then guard — must keep working."""
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    value = body.get('tenant_id')\n"
        "    if not value:\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
    )
    assert verdict == "REQUIRED"


def test_a_negated_or_is_not_a_guard() -> None:
    """``if not (tenant_id or flag): raise`` fires only when BOTH are falsy.

    So a request with no tenant and a truthy flag reaches past the raise. The
    negation has to be pushed inward — De Morgan turns this into the ``and``
    case, which already refuses a branch that is not a negative scope check —
    rather than looked through to the names underneath it.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if not (tenant_id or other_flag):\n"
        "        raise HTTPException(status_code=400, detail='unrelated')\n"
    )
    assert verdict == "OPTIONAL"


def test_a_negated_and_is_a_guard() -> None:
    """``if not (tenant_id and x): raise`` fires when EITHER is falsy.

    The other half of De Morgan: a missing tenant does reach the raise, so this
    one is fail-closed and must keep counting.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if not (tenant_id and something):\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
    )
    assert verdict == "REQUIRED"


def test_a_tenant_key_read_off_a_config_is_not_the_request() -> None:
    """``if not config["tenant_id"]: raise`` proves something about a config.

    Matching the bare literal anywhere in a guard's test credited it as proof
    the ROUTE was scoped. The main classification loop anchors its reads to the
    request; a guard's test has to be held to the same rule.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    config = await load_config()\n"
        "    if not config['tenant_id']:\n"
        "        raise HTTPException(status_code=400, detail='misconfigured')\n"
        "    return await svc.by_ids(body['ids'])\n"
    )
    assert verdict == "NONE"


def test_a_tenant_key_read_off_the_body_in_a_guard_still_counts() -> None:
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    if not body['tenant_id']:\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
    )
    assert verdict == "REQUIRED"


def test_a_guard_that_does_not_raise_is_still_optional() -> None:
    """Logging the absence is not rejecting it."""
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body = await request.json()\n"
        "    tenant_id = body.get('tenant_id')\n"
        "    if not tenant_id:\n"
        "        logger.warning('no tenant')\n"
    )
    assert verdict == "OPTIONAL"


def test_writing_a_tenant_key_is_not_reading_one() -> None:
    """``response["tenant_id"] = ...`` says nothing about what the caller sent.

    The dangerous direction. A wrong "not scoped" costs one allowlist line
    somebody deletes; a wrong "scoped" removes the route from the list
    entirely, so a real gap is never shown to anyone. Matching every subscript
    in the function — regardless of receiver or Load/Store context — scored
    this handler REQUIRED.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    response = {}\n"
        "    response['tenant_id'] = 'audit'\n"
        "    return await svc.by_ids(body['ids'])\n"
    )
    assert verdict == "NONE"


def test_a_tenant_key_on_an_unrelated_dict_is_not_the_request() -> None:
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    config = await load_config()\n"
        "    t = config.get('tenant_id')\n"
        "    return await svc.by_ids(body['ids'])\n"
    )
    assert verdict == "NONE"


def test_an_annotated_body_binding_is_still_the_request() -> None:
    """``body: dict = await request.json()`` is an AnnAssign, not an Assign.

    99 of the 107 body bindings in the routers are written this way. Anchoring
    reads to the request without handling the annotated form made every one of
    those handlers look unscoped — 46 routes at the time — which is how a
    correct-sounding tightening turns into a broken gate.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    return await svc.search(tenant_id=body['tenant_id'])\n"
    )
    assert verdict == "REQUIRED"


def test_a_tenant_read_through_a_nested_body_object_counts() -> None:
    """``event = body.get("event")`` then ``_require(event, "tenant_id")``.

    A real idiom in ``recall_log_write``'s route. The tenant is still one the
    caller had to send, so anchoring has to follow the derivation.
    """
    verdict, _ = _classify(
        "async def h(request):\n"
        "    body: dict = await request.json()\n"
        "    event = body.get('event')\n"
        "    if not isinstance(event, dict):\n"
        "        raise HTTPException(status_code=422, detail='required')\n"
        "    _require(event, 'tenant_id')\n"
    )
    assert verdict == "REQUIRED"


def test_no_tenant_read_at_all_is_none() -> None:
    verdict, _ = _classify("async def h(request):\n    body = await request.json()\n    return body['ids']\n")
    assert verdict == "NONE"


def test_a_defaulted_query_parameter_is_optional() -> None:
    """``GET /memories/{memory_id}``'s shape — the scope is a defaulted arg."""
    verdict, _ = _classify("async def h(memory_id, tenant_id = None):\n    return memory_id\n")
    assert verdict == "OPTIONAL"


def test_an_undefaulted_parameter_is_required() -> None:
    verdict, _ = _classify("async def h(tenant_id, fleet_id = None):\n    return tenant_id\n")
    assert verdict == "REQUIRED"


# ---------------------------------------------------------------------------
# The enumeration cannot silently shrink
# ---------------------------------------------------------------------------


def test_route_walk_matches_the_openapi_schema() -> None:
    """The self-check that makes walking FastAPI's private tree acceptable.

    If a version bump changes how included routers are stored, the walk returns
    fewer operations than the app serves and every one of them stops being
    checked — green, and covering less. This asserts the guard is live rather
    than that today's walk happens to work.
    """
    from core_storage_api.app import app

    class Truncated:
        """An app whose walk finds nothing but whose schema is unchanged."""

        routes: typing.ClassVar[list[object]] = []

        @staticmethod
        def openapi() -> dict:
            return app.openapi()

    with pytest.raises(RuntimeError, match="disagrees with the OpenAPI schema"):
        gate._resolve_operations(Truncated())


def test_every_live_route_is_enumerated() -> None:
    """The counts the gate reports are the counts the app actually serves."""
    from core_storage_api.app import app

    documented = sum(
        1
        for _path, verbs in app.openapi()["paths"].items()
        for verb in verbs
        if verb.lower() in ("get", "post", "put", "patch", "delete")
    )
    assert len(gate.enumerate_routes()) == documented


def test_every_public_service_method_is_enumerated() -> None:
    """Oracle read from the source, not from the same predicate the gate uses.

    Computing the expected set with ``inspect.getmembers(..., predicate)``
    reproduces whatever that predicate does, mistakes included, so it cannot
    catch one that skips an entire kind of method — which is exactly what
    ``isfunction`` did to classmethods. Parsing the class body is an
    independent answer to the same question.
    """
    import ast as _ast

    from core_storage_api.services import postgres_service as module

    tree = _ast.parse(Path(module.__file__).read_text())
    class_body = next(
        n.body for n in tree.body if isinstance(n, _ast.ClassDef) and n.name == "PostgresService"
    )
    expected = {
        n.name
        for n in class_body
        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and not n.name.startswith("_")
    }
    assert {e.key for e in gate.enumerate_methods()} == expected


def test_a_classmethod_is_enumerated(monkeypatch: pytest.MonkeyPatch) -> None:
    """``inspect.isfunction`` is False for a classmethod reached via the class.

    Which made it invisible: not reported, not allowlisted, not ratcheted. A
    method the enumerator never yields is one the gate never objects to, so
    this is the failure mode that looks exactly like success.
    """
    from core_storage_api.services.postgres_service import PostgresService

    @classmethod
    def cm_unscoped_read(cls, memory_id: str) -> None:  # type: ignore[no-untyped-def]
        """An id-addressed read with no tenant predicate."""

    monkeypatch.setattr(PostgresService, "cm_unscoped_read", cm_unscoped_read, raising=False)

    injected = [e for e in gate.enumerate_methods() if e.key == "cm_unscoped_read"]
    assert injected, "a @classmethod on PostgresService was not enumerated"
    assert injected[0].verdict == "NONE"


def test_a_staticmethod_is_enumerated(monkeypatch: pytest.MonkeyPatch) -> None:
    from core_storage_api.services.postgres_service import PostgresService

    @staticmethod
    def sm_unscoped_read(memory_id: str) -> None:  # type: ignore[no-untyped-def]
        """An id-addressed read with no tenant predicate."""

    monkeypatch.setattr(PostgresService, "sm_unscoped_read", sm_unscoped_read, raising=False)

    injected = [e for e in gate.enumerate_methods() if e.key == "sm_unscoped_read"]
    assert injected and injected[0].verdict == "NONE"


def test_a_mandatory_binding_param_does_not_outvote_a_defaulted_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``(tenant_id: str, org_id: str | None = None)`` stays OPTIONAL, not REQUIRED.

    Deliberately unlike ``_classify_handler``, where one required key settles a
    route. A route's "required" is a guard that RAISES — proof no request gets
    through without the key. A method's "mandatory" is only the absence of a
    default: proof a caller passes something, not that it is used as a
    predicate. ``settings`` and ``lifecycle_audit`` key on ``org_id``, so this
    signature can filter on the defaulted parameter and carry the mandatory one
    for logging, and the caller who forgets it gets the unscoped query — the
    bulk-get defect at the SQL layer.

    No method has this shape today, so this pins a decision rather than a fix:
    it passes on the parent commit. It is here because the rationale was already
    written in ``enumerate_methods``' docstring and a reviewer still proposed
    relaxing it, which prose evidently does not prevent and a red test does.
    """
    from core_storage_api.services.postgres_service import PostgresService

    async def mixed_scope(self, tenant_id: str, org_id: str | None = None) -> None:  # type: ignore[no-untyped-def]
        """Mandatory tenant_id beside a defaulted org_id."""

    monkeypatch.setattr(PostgresService, "mixed_scope", mixed_scope, raising=False)

    injected = [e for e in gate.enumerate_methods() if e.key == "mixed_scope"]
    assert injected, "the injected method was not enumerated"
    assert injected[0].verdict == "OPTIONAL"
    assert "org_id" in injected[0].detail


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def base_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo whose committed allowlist already holds one exception.

    A non-empty baseline is the realistic case: the gate's job is to hold a
    large existing list flat, not to demand zero.
    """
    repo = tmp_path / "scratch"
    (repo / "core-storage-api").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "core-storage-api" / "tenant_scope_allowlist.json").write_text(
        json.dumps(
            {"exceptions": [{"id": "method:already_here", "verdict": "NONE", "category": "id-addressed-read"}]}
        )
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    monkeypatch.setattr(gate, "REPO_ROOT", repo)
    return repo


def _at(ident: str, verdict: str = "NONE") -> dict[str, gate.Entry]:
    kind, key = ident.split(":", 1)
    return {ident: gate.Entry(kind, key, verdict, "")}


def test_ratchet_allows_the_list_to_stay_flat(base_repo: Path) -> None:
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    assert gate.ratchet("HEAD", path, _at("method:already_here")) == []


def test_ratchet_allows_the_list_to_shrink(base_repo: Path) -> None:
    """Shrinking is the point of the exercise, not something to warn about."""
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    assert gate.ratchet("HEAD", path, {}) == []


def test_ratchet_fails_when_the_list_grows(base_repo: Path) -> None:
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    errors = gate.ratchet("HEAD", path, _at("method:already_here") | _at("method:newly_unscoped"))
    assert len(errors) == 1
    assert "method:newly_unscoped" in errors[0]
    assert "method:already_here" not in errors[0]


def test_ratchet_fails_when_an_entry_weakens(base_repo: Path) -> None:
    """The hole ``check`` alone leaves open.

    ``check`` compares the tree against the committed allowlist, so it catches a
    regression only while the file still remembers the old verdict. An author
    who weakens a path and then re-runs ``--write`` moves the record along with
    the code: the id set is unchanged and the file agrees with the tree, so both
    of those go quiet. The base tree is the only copy that still remembers.
    """
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    path.write_text(
        json.dumps(
            {"exceptions": [{"id": "method:already_here", "verdict": "OPTIONAL", "category": "id-addressed-read"}]}
        )
    )
    _git(base_repo, "commit", "-qam", "record as OPTIONAL")

    errors = gate.ratchet("HEAD", path, _at("method:already_here", verdict="NONE"))
    assert len(errors) == 1
    assert "OPTIONAL -> NONE" in errors[0]


def test_ratchet_fails_when_a_mutating_path_is_relabelled_as_a_read(base_repo: Path) -> None:
    """The hole the read/write split would otherwise open.

    Splitting the backlog by blast radius makes "how many unscoped paths mutate
    rows" a number people watch, and any number people watch can be made to fall
    the cheap way. Relabelling leaves the row, the verdict and the id untouched,
    so every other check here stays quiet.
    """
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    path.write_text(
        json.dumps(
            {"exceptions": [{"id": "method:already_here", "verdict": "NONE", "category": "id-addressed-write"}]}
        )
    )
    _git(base_repo, "commit", "-qam", "record as mutating")

    path.write_text(
        json.dumps(
            {"exceptions": [{"id": "method:already_here", "verdict": "NONE", "category": "id-addressed-read"}]}
        )
    )
    errors = gate.ratchet("HEAD", path, _at("method:already_here"))
    assert len(errors) == 1
    assert "id-addressed-write -> id-addressed-read" in errors[0]


def test_ratchet_allows_a_mutating_path_to_leave_the_list_entirely(base_repo: Path) -> None:
    """The legitimate exit: it got a tenant scope, so it is no longer an exception."""
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    path.write_text(
        json.dumps(
            {"exceptions": [{"id": "method:already_here", "verdict": "NONE", "category": "id-addressed-write"}]}
        )
    )
    _git(base_repo, "commit", "-qam", "record as mutating")

    path.write_text(json.dumps({"exceptions": []}))
    assert gate.ratchet("HEAD", path, {}) == []


def test_a_duplicate_in_the_ratchet_base_is_an_error_not_a_pass(base_repo: Path) -> None:
    """A duplicate in the BASE decides what every comparison is made against.

    The working copy is protected by this check running on each PR, but that
    induction has no base case for what is already on main.
    """
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    path.write_text(
        json.dumps(
            {
                "exceptions": [
                    {"id": "method:already_here", "verdict": "NONE", "category": "id-addressed-write"},
                    {"id": "method:already_here", "verdict": "NONE", "category": "no-tenant-data"},
                ]
            }
        )
    )
    _git(base_repo, "commit", "-qam", "a bad merge doubled a row")
    path.write_text(
        json.dumps(
            {"exceptions": [{"id": "method:already_here", "verdict": "NONE", "category": "id-addressed-write"}]}
        )
    )

    with pytest.raises(gate.AllowlistError):
        gate.ratchet("HEAD", path, _at("method:already_here"))


def test_ratchet_allows_an_entry_to_strengthen(base_repo: Path) -> None:
    """Getting better is not a regression."""
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    assert gate.ratchet("HEAD", path, _at("method:already_here", verdict="OPTIONAL")) == []


def test_ratchet_fails_loudly_on_an_unresolvable_base(base_repo: Path) -> None:
    """"Nobody looked" must not be reported as "nothing grew".

    ``git show <bad-ref>:<path>`` fails exactly the way ``git show
    <good-ref>:<missing-path>`` does. Treating both as "this commit introduces
    the file" meant a failed ``git fetch`` in CI, or a mistyped ``--base``,
    skipped the only mechanism holding the allowlist flat — on a green build,
    with nothing printed.
    """
    path = base_repo / "core-storage-api" / "tenant_scope_allowlist.json"
    errors = gate.ratchet("no-such-ref-at-all", path, _at("method:newly_unscoped"))
    assert len(errors) == 1
    assert "does not resolve" in errors[0]


def test_path_existence_at_a_ref_is_decided_by_exit_code(base_repo: Path) -> None:
    """Not by reading git's prose.

    The "did the allowlist exist at base" question was answered by looking for
    "does not exist" in ``git show``'s stderr — English, and git's to reword.
    Under a translated locale that reads as a real failure on the introducing
    commit, and a genuine error whose text happens to match reads as "nothing
    to compare", which is the silently-green case the ref check exists to stop.
    """
    assert gate._path_in_ref("HEAD", "core-storage-api/tenant_scope_allowlist.json")
    assert not gate._path_in_ref("HEAD", "core-storage-api/never_existed.json")


def test_ratchet_is_silent_before_the_allowlist_exists(base_repo: Path) -> None:
    """The commit that introduces the file has no baseline to compare against."""
    missing = base_repo / "core-storage-api" / "not_yet.json"
    assert gate.ratchet("HEAD", missing, _at("method:anything")) == []


def _seeded_allowlist() -> dict[str, dict[str, str]]:
    return gate.load_allowlist(ALLOWLIST)


def _live_entries() -> list[gate.Entry]:
    """Both halves of the enumeration.

    ``check`` reports an allowlist row matching nothing as stale, so handing it
    only the methods would call all 64 route rows stale and drown the assertion
    the caller actually made.
    """
    return gate.enumerate_methods() + gate.enumerate_routes()
