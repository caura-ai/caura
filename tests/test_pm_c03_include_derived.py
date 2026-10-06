"""pm-0918-c-03 — ``/search`` can exclude atomic-fact fan-out children.

A70's fan-out writes a short single-claim child per extracted fact. They stay
retrievable alongside the parent they came from, so a caller asking for 50
memories can get 36 memories and 14 fragments — measured at ~28% of returned
rows on the store that prompted this.

That store's own measurements are under review (``pm-0918-c-01``), so the
within-run 79.8% vs 82.2% delta recorded alongside it is deliberately NOT
repeated here as a value for this feature: it was measured client-side before
the server-side refill below existed, which makes it a floor rather than an
estimate. The behaviour these tests pin does not depend on it.

Filtering them CLIENT-side does not work, and the reason is the whole design:
dropping rows after the response distorts ``top_k``. Ask for 50, drop 14, get
36, and now over-fetch and guess. So the filter runs server-side and BEFORE the
trim, where the existing ``SEARCH_OVERFETCH_FACTOR`` absorbs it.

What shipped is option (a) of three: a request flag defaulting to TODAY'S
behaviour, plus a per-tenant default so a crowded store gets option (c) —
exclude by default — without imposing it on every tenant. The global default is
deliberately unchanged; moving it is a breaking change to an endpoint in the
frozen broker subset that no CI gate catches. See
``docs/atomic-fact-fanout/pm-c03-include-derived-blast-radius.md``.
"""

import pytest

from core_api.constants import INCLUDE_DERIVED_DEFAULT
from core_api.search_trim import is_derived_fanout_row, resolve_include_derived
from tests.conftest import get_test_auth
from tests.conftest import uid as _uid

# ── the predicate: what is, and is not, a derived row ────────────────────


@pytest.mark.unit
def test_a_fanout_child_is_derived():
    assert is_derived_fanout_row(
        {"parent_memory_id": "p1", "source": "atomic_fact_fanout"}
    )


@pytest.mark.unit
def test_an_auto_chunk_child_is_NOT_derived():
    """The single most important assertion in this file.

    Auto-chunk children carry ``parent_memory_id`` too, so the obvious
    one-clause predicate catches them — and they are not redundant. The parent
    holds the whole document in ``content`` but carries ONE embedding over all
    of it; these children are the only vectors that can match a specific
    passage. Excluding them would not remove duplicates, it would make long
    documents unfindable, and it would surface days later as a retrieval
    complaint with nothing pointing at this filter.
    """
    assert not is_derived_fanout_row({"parent_memory_id": "p1", "source": "auto_chunk"})


@pytest.mark.unit
def test_an_ordinary_row_is_not_derived():
    assert not is_derived_fanout_row({"summary": "s", "tags": ["a"]})
    assert not is_derived_fanout_row({})
    assert not is_derived_fanout_row(None)


@pytest.mark.unit
def test_a_row_with_no_metadata_object_is_not_derived():
    """``metadata`` is nullable and has held scalars historically. Neither writer
    can produce one — both build a dict literal — so this is an ordinary row,
    not a parse failure worth flagging."""
    assert not is_derived_fanout_row("null")
    assert not is_derived_fanout_row(42)


@pytest.mark.unit
def test_a_caller_cannot_forge_the_marker_with_source_alone():
    """``source`` is caller-writable BY DESIGN — it is deliberately absent from
    ``PLATFORM_ONLY_KEYS`` because ingest writes it in caller-adjacent item
    metadata. ``parent_memory_id`` is reserved and stripped from caller input.
    Requiring both is what stops a caller hiding its own rows from the default
    search by stamping a source on them."""
    assert not is_derived_fanout_row({"source": "atomic_fact_fanout"})


@pytest.mark.unit
def test_the_marker_is_read_from_the_system_namespace_too():
    """Neither writer routes through ``set_system_value`` today, so nothing
    lands in ``_system``. Checked anyway because ``extract_system_metadata``
    merges the two on read: a row written the other way would be a fan-out child
    to every consumer while being invisible to a top-level-only probe, and that
    divergence would be silent."""
    assert is_derived_fanout_row(
        {"_system": {"parent_memory_id": "p1", "source": "atomic_fact_fanout"}}
    )


# ── precedence: request > tenant > global ────────────────────────────────


class _Cfg:
    def __init__(self, value):
        self.search_include_derived = value


@pytest.mark.unit
def test_the_global_default_is_pinned():
    """Pinned so a refactor cannot flip it unnoticed — which matters more now
    that a per-tenant override exists to mistake it for. Changing this is a
    BREAKING change to ``POST /search``, which is in the frozen broker subset
    and whose oasdiff gate sees NOTHING when a default moves. If this assertion
    is what is failing, that is the gate: the change needs a ``BREAKING
    CHANGE:`` trailer and the ``kind/breaking`` label, not an edit here."""
    assert INCLUDE_DERIVED_DEFAULT is True
    assert resolve_include_derived(None, None) is True


@pytest.mark.unit
def test_an_unset_tenant_falls_through_to_the_global_default():
    assert resolve_include_derived(None, _Cfg(None)) is True


@pytest.mark.unit
def test_a_tenant_can_turn_derived_rows_off_store_wide():
    assert resolve_include_derived(None, _Cfg(False)) is False


@pytest.mark.unit
def test_the_request_flag_beats_the_tenant_setting_in_both_directions():
    """Both directions, because only one of them is obvious. A caller must be
    able to turn them OFF for one query in a tenant that leaves them on, AND
    back ON in a tenant that has switched them off — the second is why the
    request field is a tri-state instead of ``bool = True``."""
    assert resolve_include_derived(False, _Cfg(True)) is False
    assert resolve_include_derived(True, _Cfg(False)) is True


@pytest.mark.unit
def test_a_config_object_predating_the_property_resolves_to_the_default():
    """Test doubles and older config objects have no such attribute. Same
    ``getattr`` tolerance ``strict_fleet_scoping`` is read with."""

    class _Old:
        pass

    assert resolve_include_derived(None, _Old()) is True


# ── the settings knob, through the HTTP route that writes it ─────────────


async def test_the_tenant_default_survives_a_real_settings_put(client):
    """The c-04 regression test, and it is deliberately NOT a unit test.

    On pm-0918-c-04 a per-tenant switch was registered only as a
    ``ResolvedConfig`` property and never added to ``DEFAULT_SETTINGS``. The
    resolver happily served the default while ``_check_keys`` rejected every
    write, so ``PUT /settings`` answered 422 and the knob was UNSETTABLE. It
    read as shipped. Every test that file had built ``ResolvedConfig`` directly,
    which bypasses the validation entirely — which is exactly why this one goes
    through the route.

    Verified by REINTRODUCING the defect rather than by asserting against it:
    removing the ``DEFAULT_SETTINGS`` entry while leaving the resolver property
    in place makes this fail with c-04's exact error, ``Unknown settings key(s):
    ['search.include_derived']``.

    ORDER IS LOAD-BEARING, and the near-miss is why it is written down. The
    first draft of this test asserted the unset read shape BEFORE the PUT, and
    under the reintroduced defect it failed on a ``KeyError`` from the GET —
    a true report, of a different thing, one step removed from the defect being
    guarded. A test for an unsettable knob has to fail ON THE WRITE. The unset
    read shape is worth pinning too and has its own test below.
    """
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")

    # The WRITE first, and on purpose: this is the call the c-04 defect answered
    # 422 to, so it is what has to fail loudly if the knob ever stops being
    # registered. Asserting the read shape first would fail here on a KeyError
    # instead — a true report of a different thing, one step removed from the
    # defect being guarded.
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"search": {"include_derived": False}},
        headers=headers,
    )
    assert resp.status_code == 200, f"PUT failed — the knob is unsettable: {resp.text}"

    reloaded = await client.get(
        f"/api/v1/settings?tenant_id={tenant_id}", headers=headers
    )
    assert reloaded.status_code == 200, reloaded.text
    assert reloaded.json()["search"]["include_derived"] is False, (
        "the write was accepted but did not persist"
    )

    # And the value the search path actually reads agrees with what came back.
    from core_api.services.organization_settings import resolve_config

    config = await resolve_config(tenant_id)
    assert config.search_include_derived is False
    assert resolve_include_derived(None, config) is False


async def test_an_unset_tenant_reports_the_key_as_null(client):
    """Unset has to be visible as ``null`` in the display view, not missing from
    it: a dashboard cannot render a control for a key it never receives, and the
    resolver has to read it as "not set" rather than as "off"."""
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")
    resp = await client.get(f"/api/v1/settings?tenant_id={tenant_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    search = resp.json()["search"]
    assert "include_derived" in search, (
        "not registered in DEFAULT_SETTINGS — see the c-04 note"
    )
    assert search["include_derived"] is None


async def test_a_string_false_is_refused_rather_than_silently_truthy(client):
    """``"false"`` is a truthy string. Without the ``_LEAF_TYPES`` entry it would
    resolve to ON while the dashboard rendered the tenant's "off" back to them —
    the worst shape of this bug, because it looks correct from both ends."""
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"search": {"include_derived": "false"}},
        headers=headers,
    )
    assert resp.status_code == 422, (
        f"expected a type refusal, got {resp.status_code}: {resp.text}"
    )


async def test_a_misspelled_key_is_refused(client):
    tenant_id, headers = get_test_auth(tenant_id=f"test-tenant-{_uid()}")
    resp = await client.put(
        f"/api/v1/settings?tenant_id={tenant_id}",
        json={"search": {"include_derived_rows": False}},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


# ── the filter, in the step that applies it ──────────────────────────────


def _row(metadata, score=1.0):
    class _Memory:
        def __init__(self, md):
            self.id = "m"
            self.metadata_ = md
            self.title = None
            self.memory_type = "fact"
            self.status = "active"

    class _Row:
        def __init__(self, md, sc):
            self.Memory = _Memory(md)
            self.score = sc
            self.vec_sim = sc
            self.fts_score = 0.0
            self.fts_match = False
            self.has_embedding = True

    return _Row(metadata, score)


def _ctx(rows, *, include_derived, top_k):
    from core_api.pipeline.context import PipelineContext

    return PipelineContext(
        data={
            "raw_rows": rows,
            "search_params": {"min_similarity": 0.0, "fts_weight": 0.0},
            "final_top_k": top_k,
            "include_derived": include_derived,
        }
    )


_FANOUT = {"parent_memory_id": "p1", "source": "atomic_fact_fanout"}
_CHUNK = {"parent_memory_id": "p1", "source": "auto_chunk"}


@pytest.mark.unit
async def test_the_step_returns_derived_rows_by_default():
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    ctx = _ctx([_row(_FANOUT), _row({})], include_derived=True, top_k=10)
    await PostFilterResults().execute(ctx)
    assert len(ctx.data["filtered_rows"]) == 2


@pytest.mark.unit
async def test_the_step_drops_fanout_children_but_keeps_auto_chunk_children():
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    ctx = _ctx([_row(_FANOUT), _row(_CHUNK), _row({})], include_derived=False, top_k=10)
    await PostFilterResults().execute(ctx)
    kept = [r.Memory.metadata_ for r in ctx.data["filtered_rows"]]
    assert _FANOUT not in kept
    assert _CHUNK in kept, "auto-chunk children are not redundant — see the predicate"
    assert len(kept) == 2


@pytest.mark.unit
async def test_a_caller_asking_for_top_k_still_gets_top_k():
    """The reason this is server-side at all.

    Ten candidates for a ``top_k`` of 5, three of them derived — the shape the
    2x overfetch produces. Filtering consumes overfetch headroom, so the caller
    still gets 5. Client-side the same caller gets 5 minus however many of its 5
    were derived, and has to over-fetch and guess to compensate.
    """
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    rows = [_row(_FANOUT if i % 3 == 0 else {}, score=1.0 - i / 100) for i in range(10)]
    assert sum(1 for r in rows if r.Memory.metadata_ is _FANOUT) == 4

    ctx = _ctx(rows, include_derived=False, top_k=5)
    await PostFilterResults().execute(ctx)
    assert len(ctx.data["filtered_rows"]) == 5
    assert all(r.Memory.metadata_ is not _FANOUT for r in ctx.data["filtered_rows"])


@pytest.mark.unit
async def test_a_context_without_the_key_behaves_as_before():
    """An older caller, or a test double, builds a ctx with no
    ``include_derived``. It must read as "include", not as falsy "exclude"."""
    from core_api.pipeline.context import PipelineContext
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    ctx = PipelineContext(
        data={
            "raw_rows": [_row(_FANOUT), _row({})],
            "search_params": {"min_similarity": 0.0, "fts_weight": 0.0},
            "final_top_k": 10,
        }
    )
    await PostFilterResults().execute(ctx)
    assert len(ctx.data["filtered_rows"]) == 2


@pytest.mark.unit
async def test_the_diagnostic_names_the_derived_exclusion_separately():
    """A derived row cut here would otherwise report as ``trimmed_by_top_k``,
    which is the one reading that sends someone raising top_k to get it back —
    the one thing that cannot work."""
    from core_api.pipeline.steps.search.post_filter_results import PostFilterResults

    ctx = _ctx([_row(_FANOUT), _row({})], include_derived=False, top_k=10)
    ctx.data["diagnostic"] = True
    await PostFilterResults().execute(ctx)

    reasons = {c["excluded"] for c in ctx.data["diagnostic_results"]}
    assert "derived_excluded" in reasons
    assert ctx.data["diagnostic_counts"]["excluded_derived"] == 1
    # And it is not double-counted as top_k pressure that did not happen.
    assert ctx.data["diagnostic_counts"]["excluded_by_top_k_trim"] == 0
