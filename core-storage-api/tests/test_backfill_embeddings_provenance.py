"""The embedding backfill must record which text each vector came from.

Writing the vector alone does not leave the row where it was found — it moves
it. Both embedding backfills scan ``embedding IS NULL``; a row written without
``embedded_content_hash`` lands in ``embedding IS NOT NULL AND
embedded_content_hash IS NULL``, and nothing anywhere scans that. So the repair
tool's own write was what removed rows from the reach of every repair path and
from the staleness detector, in one statement, with no error.

That is not theoretical: the same omission on core-api's bulk re-embed
fallbacks (caura#1281) put 241 production rows in that bucket before anyone
noticed, and this script would have quietly done the same to any row it
touched.

The sharp test here is ``test_stamps_the_hash_it_embedded_not_the_row_s_current_hash``.
Stamping provenance at all is the easy half; stamping the RIGHT hash is what
separates this fix from the obvious one. A SQL-side ``SET embedded_content_hash
= content_hash`` also makes the column non-NULL and also passes every other
test in this file — while recording a row as freshly embedded at the exact
moment it went stale.

Integration: these need a real PostgreSQL with pgvector, because the defect is
in emitted SQL and a mocked connection would assert against the mock.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from core_storage_api.scripts import backfill_embeddings as bf

VECTOR_DIM_FAKE = 1024


@pytest.fixture
async def engine(_ensure_schema):
    """The engine the script itself uses.

    ``run_backfill`` calls ``database.init.get_engine()``, whose pool settings
    (size, overflow, recycle, ``pool_pre_ping``) come from config. Building a
    second engine here would test a pool the script never runs on. It is a
    module-level singleton, so this deliberately does NOT dispose it.
    """
    from core_storage_api.database.init import get_engine

    return get_engine()


def _memories_spec() -> bf._TableSpec:
    """The real production spec for ``memories``, not a hand-built stand-in.

    Taken from ``_TARGETS`` so a spec that stops declaring its provenance
    columns fails these tests rather than passing against a local copy that
    still declares them.
    """
    return next(s for s in bf._TARGETS if s.table == "memories")


def _entities_spec() -> bf._TableSpec:
    return next(s for s in bf._TARGETS if s.table == "entities")


async def _seed_memory(engine, tenant_id: str, *, content: str, content_hash: str | None) -> uuid.UUID:
    mid = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO memories (id, tenant_id, agent_id, memory_type, content, content_hash, embedding) "
                "VALUES (:id, :t, 'agent-bf', 'fact', :c, :ch, NULL)"
            ),
            {"id": mid, "t": tenant_id, "c": content, "ch": content_hash},
        )
        await conn.commit()
    return mid


async def _read_memory(engine, mid: uuid.UUID) -> tuple[bool, str | None, str | None]:
    """Return (has_embedding, content_hash, embedded_content_hash)."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT embedding IS NOT NULL, content_hash, embedded_content_hash "
                    "FROM memories WHERE id = :id"
                ),
                {"id": mid},
            )
        ).one()
    return bool(row[0]), row[1], row[2]


def _patch_embedding(monkeypatch, *, on_call=None):
    """Stub the provider. ``_backfill_one_table`` imports it inside the function
    body, so patch the module attribute the import resolves against."""
    import common.embedding

    async def _fake(content, *_a, **_kw):
        if on_call is not None:
            await on_call(content)
        return [0.125] * VECTOR_DIM_FAKE

    monkeypatch.setattr(common.embedding, "get_embedding", _fake)


async def _run(engine, spec, tenant_id, *, mode: bf._ScanMode = bf._ScanMode.NULL_EMBEDDING):
    return await bf._backfill_one_table(
        engine,
        spec,
        tenant_id=tenant_id,
        batch_size=100,
        max_inflight=4,
        dry_run=False,
        mode=mode,
    )


@pytest.mark.integration
async def test_stamps_provenance_alongside_the_vector(engine, tenant_id, monkeypatch):
    """The whole point: after the sweep, the row says which text it embedded."""
    _patch_embedding(monkeypatch)
    ch = "a" * 64
    mid = await _seed_memory(engine, tenant_id, content="a body to embed", content_hash=ch)

    report = await _run(engine, _memories_spec(), tenant_id)

    assert report.embedded == 1
    has_emb, content_hash, provenance = await _read_memory(engine, mid)
    assert has_emb, "the sweep did not write a vector at all"
    assert provenance == ch, (
        f"vector written with embedded_content_hash={provenance!r}; a row embedded "
        "without provenance leaves every repair path and the staleness detector"
    )
    assert provenance == content_hash


@pytest.mark.integration
async def test_row_does_not_land_in_the_unswept_population(engine, monkeypatch):
    """Stated as the invariant that matters, and counted the way ops counts it.

    ``embedding IS NOT NULL AND embedded_content_hash IS NULL`` is the
    population no sweep selects on. Rather than re-writing that predicate here
    — the service's own docstring already warns that its three copies must be
    kept in step, and a fourth drifted immediately, omitting the
    ``LIVE_MEMORY_STATUSES`` filter — this asserts through
    ``memory_embedding_coverage_for_tenant``, whose ``unknown_provenance``
    bucket IS that predicate and is what the operator dashboard reports. So
    this fails if the backfill regresses, and equally if the metric stops
    measuring what it claims.

    Its own tenant, deliberately. The ``tenant_id`` fixture returns one
    session-constant string, and ``test_null_content_hash_stays_null`` below
    creates a row matching this very predicate under it — legitimately, since
    a row with no content hash has nothing to attest. Sharing the tenant would
    make this assertion depend on which test ran first.
    """
    from core_storage_api.services.postgres_service import PostgresService

    isolated_tenant = f"test-unswept-{uuid.uuid4().hex[:8]}"
    _patch_embedding(monkeypatch)
    await _seed_memory(engine, isolated_tenant, content="body", content_hash="b" * 64)

    await _run(engine, _memories_spec(), isolated_tenant)

    (
        _total,
        _missing,
        _stale,
        unknown,
        _missing_prov,
    ) = await PostgresService().memory_embedding_coverage_for_tenant(isolated_tenant)
    assert unknown == 0, f"{unknown} row(s) moved into the population nothing sweeps"


@pytest.mark.integration
async def test_stamps_the_hash_it_embedded_not_the_row_s_current_hash(engine, tenant_id, monkeypatch):
    """A content update mid-embed must not be recorded as freshly embedded.

    The window is real: fetch -> embed (a network call) -> write. If the row's
    content changes inside it, the vector describes the OLD text while
    ``content_hash`` now describes the NEW text. Stamping the value read with
    the content keeps the column's promise true and leaves the row visibly
    stale. A SQL-side ``SET embedded_content_hash = content_hash`` would
    instead stamp the NEW hash onto the OLD vector and mark the row fresh —
    the precise failure ``memory_update_embedding`` refuses the same re-read
    to avoid.

    This is the test that distinguishes the fix from the obvious one.
    """
    old_hash, new_hash = "c" * 64, "d" * 64
    mid = await _seed_memory(engine, tenant_id, content="original body", content_hash=old_hash)

    async def _concurrent_content_update(_content):
        async with engine.connect() as conn:
            await conn.execute(
                text("UPDATE memories SET content = :c, content_hash = :ch WHERE id = :id"),
                {"c": "edited body", "ch": new_hash, "id": mid},
            )
            await conn.commit()

    _patch_embedding(monkeypatch, on_call=_concurrent_content_update)

    await _run(engine, _memories_spec(), tenant_id)

    _has_emb, content_hash, provenance = await _read_memory(engine, mid)
    assert content_hash == new_hash, "fixture did not actually simulate the concurrent edit"
    assert provenance == old_hash, (
        f"stamped {provenance!r}; the vector was built from the text hashing to "
        f"{old_hash!r}, so recording anything else asserts a freshness that is not true"
    )
    assert provenance != content_hash, (
        "row reads as freshly embedded despite its content having changed since; "
        "the staleness detector can no longer see it"
    )


@pytest.mark.integration
async def test_null_content_hash_stays_null(engine, tenant_id, monkeypatch):
    """No hash to copy means unknown provenance, which is the honest record.

    A guessed or derived value would be worse than the NULL it replaced: NULL
    reads as "unknown", while a wrong hash reads as verified freshness. The
    embedding is still written — the row is repaired, just not attested.
    """
    _patch_embedding(monkeypatch)
    mid = await _seed_memory(engine, tenant_id, content="body without a hash", content_hash=None)

    report = await _run(engine, _memories_spec(), tenant_id)

    assert report.embedded == 1
    has_emb, content_hash, provenance = await _read_memory(engine, mid)
    assert has_emb, "the row should still get its vector"
    assert content_hash is None
    assert provenance is None, f"invented provenance {provenance!r} for a row with no content hash"


@pytest.mark.integration
async def test_entities_backfill_still_works(engine, tenant_id, monkeypatch):
    """``entities`` has neither column, so the stamp must not be emitted there.

    Migration 037 added ``embedded_content_hash`` to ``memories`` alone. A
    table-blind fix compiles fine and fails at runtime on every entity write
    with an undefined-column error — turning a provenance bug into an outage
    of the other half of the same script.
    """
    _patch_embedding(monkeypatch)
    eid = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO entities (id, tenant_id, entity_type, canonical_name, name_embedding) "
                "VALUES (:id, :t, 'person', 'Ada Lovelace', NULL)"
            ),
            {"id": eid, "t": tenant_id},
        )
        await conn.commit()

    report = await _run(engine, _entities_spec(), tenant_id)

    assert report.embedded == 1
    async with engine.connect() as conn:
        has_emb = (
            await conn.execute(
                text("SELECT name_embedding IS NOT NULL FROM entities WHERE id = :id"), {"id": eid}
            )
        ).scalar()
    assert has_emb, "entities backfill wrote no vector"


@pytest.mark.integration
async def test_hint_rewrite_replaces_stale_provenance(engine, tenant_id, monkeypatch):
    """``--rewrite-hint-prefixed`` overwrites an existing vector, so it owns the stamp.

    These rows already carry a vector and may already carry provenance for it.
    The rewrite replaces the vector, which makes any existing stamp describe
    something that no longer exists. Writing the current hash in the same
    statement keeps the two in step.
    """
    _patch_embedding(monkeypatch)
    stale_stamp, current_hash = "e" * 64, "f" * 64
    mid = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO memories "
                "(id, tenant_id, agent_id, memory_type, content, content_hash, "
                " embedded_content_hash, embedding, metadata) "
                "VALUES (:id, :t, 'agent-bf', 'fact', :c, :ch, :stale, "
                '        (:emb)::vector, \'{"retrieval_hint": "a hint"}\'::jsonb)'
            ),
            {
                "id": mid,
                "t": tenant_id,
                "c": "hint-prefixed body",
                "ch": current_hash,
                "stale": stale_stamp,
                "emb": str([0.5] * VECTOR_DIM_FAKE),
            },
        )
        await conn.commit()

    report = await _run(engine, _memories_spec(), tenant_id, mode=bf._ScanMode.REWRITE_HINT_PREFIXED)

    assert report.embedded == 1, "hint-rewrite mode did not select the seeded row"
    _has_emb, _content_hash, provenance = await _read_memory(engine, mid)
    assert provenance == current_hash, (
        f"stamp is {provenance!r}; after replacing the vector the row must attest the "
        "text the NEW vector was built from, not whatever the old one claimed"
    )
    assert provenance != stale_stamp


@pytest.mark.integration
async def test_missing_provenance_excludes_the_innocent_cases(engine):
    """The alertable bucket must hold only rows with no honest explanation.

    ``unknown_provenance`` cannot be alerted on: its level legitimately sits in
    the tens of thousands of pre-037 rows. ``missing_provenance`` is the same
    predicate with three carve-outs, and every one has to hold or the alert
    becomes a false positive on a healthy deployment — which is how an alert
    gets switched off and the next defect goes unseen.

    * a row created BEFORE migration 037 predates provenance entirely
    * a row with no ``content_hash`` has nothing to attest TO, so NULL is the
      honest value — core-worker's backfill legitimately publishes None
    * a row with no embedding has nothing to attest ABOUT

    Seeds one of each beside a genuine defect and asserts the buckets split.
    """
    from datetime import UTC, datetime

    from core_storage_api.services.postgres_service import PostgresService

    t = f"test-prov-buckets-{uuid.uuid4().hex[:8]}"
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO memories "
                "(id, tenant_id, agent_id, memory_type, content, content_hash, "
                " embedded_content_hash, embedding, created_at) VALUES "
                # 1. genuine defect: post-037, has a hash, has a vector, attests nothing
                "(:i1, :t, 'a', 'fact', 'defect', 'h1', NULL, (:emb)::vector, :recent), "
                # 2. pre-037: provenance did not exist yet
                "(:i2, :t, 'a', 'fact', 'legacy', 'h2', NULL, (:emb)::vector, :old), "
                # 3. no content_hash: nothing to attest TO
                "(:i3, :t, 'a', 'fact', 'nohash', NULL, NULL, (:emb)::vector, :recent), "
                # 4. no embedding: nothing to attest ABOUT
                "(:i4, :t, 'a', 'fact', 'novec', 'h4', NULL, NULL, :recent)"
            ),
            {
                "i1": uuid.uuid4(),
                "i2": uuid.uuid4(),
                "i3": uuid.uuid4(),
                "i4": uuid.uuid4(),
                "t": t,
                "emb": str([0.3] * VECTOR_DIM_FAKE),
                "recent": datetime(2026, 9, 1, tzinfo=UTC),
                "old": datetime(2026, 8, 1, tzinfo=UTC),
            },
        )
        await conn.commit()

    (
        _total,
        missing,
        _stale,
        unknown,
        missing_prov,
    ) = await PostgresService().memory_embedding_coverage_for_tenant(t)

    # All three embedded-but-unattested rows land in ``unknown`` — that bucket
    # is deliberately broad, which is exactly why it cannot be alerted on.
    assert unknown == 3, f"expected rows 1-3 in unknown_provenance, got {unknown}"
    # Only row 1 has no innocent reading.
    assert missing_prov == 1, (
        f"missing_provenance={missing_prov}; it must exclude pre-037 rows and rows "
        "with no content_hash, or the alert fires on a healthy deployment"
    )
    assert missing == 1, "row 4 has no vector at all"


def test_predicate_string_is_derived_from_the_constant():
    """The operator-facing query must describe the filter that produced the count.

    ``MISSING_PROVENANCE_PREDICATE_SQL`` is handed to whoever alerts on
    ``missing_provenance`` so a human can reproduce the number. It is a string,
    so nothing makes it agree with the ORM filter beside it — and the two live
    ten lines apart today but are consumed by different services.

    No database: this asserts the string is built FROM the constant rather than
    restating it, which is the only property that keeps them from drifting when
    the cutoff moves.
    """
    from core_storage_api.services import postgres_service as ps

    assert ps.PROVENANCE_REQUIRED_FROM.date().isoformat() in ps.MISSING_PROVENANCE_PREDICATE_SQL, (
        "the predicate string does not name the cutoff it is built from; an "
        "operator pasting it would count a different population than the alert did"
    )
    # The three carve-outs that make this bucket alertable at all must each be
    # visible in the query an operator is handed, or they will reproduce the
    # broad ``unknown_provenance`` count and conclude the alert was wrong.
    for term in (
        "embedding IS NOT NULL",
        "embedded_content_hash IS NULL",
        "content_hash IS NOT NULL",
        "created_at >=",
    ):
        assert term in ps.MISSING_PROVENANCE_PREDICATE_SQL, f"predicate omits {term!r}"


# ---------------------------------------------------------------------------
# --repair-provenance: the only sweep that can see these rows
# ---------------------------------------------------------------------------


async def _seed_embedded(
    engine,
    tenant_id: str,
    *,
    content: str,
    content_hash: str | None,
    created_at,
    stamp: str | None = None,
) -> uuid.UUID:
    """A row that already carries a vector — the population under repair."""
    mid = uuid.uuid4()
    async with engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO memories (id, tenant_id, agent_id, memory_type, content, "
                " content_hash, embedded_content_hash, embedding, created_at) "
                "VALUES (:id, :t, 'agent-bf', 'fact', :c, :ch, :stamp, (:emb)::vector, :ts)"
            ),
            {
                "id": mid,
                "t": tenant_id,
                "c": content,
                "ch": content_hash,
                "stamp": stamp,
                "emb": str([0.9] * VECTOR_DIM_FAKE),
                "ts": created_at,
            },
        )
        await conn.commit()
    return mid


@pytest.mark.integration
async def test_repair_provenance_reaches_rows_no_other_sweep_can(engine, monkeypatch):
    """The point of the mode: these rows are invisible to every other scan.

    Both embedding backfills and core-worker's event-driven task select
    ``embedding IS NULL``. These rows HAVE an embedding, which is exactly why
    241 of them sat unrepaired for a week while three separate repair paths ran
    over the same table.
    """
    from datetime import UTC, datetime

    _patch_embedding(monkeypatch)
    t = f"test-repair-{uuid.uuid4().hex[:8]}"
    ch = "a" * 64
    mid = await _seed_embedded(
        engine, t, content="body", content_hash=ch, created_at=datetime(2026, 9, 1, tzinfo=UTC)
    )

    # The default mode cannot see it — that is the gap being closed.
    before = await _run(engine, _memories_spec(), t)
    assert before.scanned == 0, "default mode should not match an already-embedded row"

    report = await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)

    assert report.embedded == 1
    _has_emb, content_hash, provenance = await _read_memory(engine, mid)
    assert provenance == ch
    assert provenance == content_hash


@pytest.mark.integration
async def test_repair_provenance_is_idempotent(engine, monkeypatch):
    """A second pass must find nothing.

    The repair flips ``embedded_content_hash`` from NULL to non-NULL, which
    takes the row out of its own selector. Worth pinning: the hint-rewrite mode
    next door is deliberately NOT idempotent, so "re-running is safe" is not a
    property this file can assume.
    """
    from datetime import UTC, datetime

    _patch_embedding(monkeypatch)
    t = f"test-repair-idem-{uuid.uuid4().hex[:8]}"
    await _seed_embedded(
        engine, t, content="body", content_hash="b" * 64, created_at=datetime(2026, 9, 1, tzinfo=UTC)
    )

    first = await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)
    second = await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)

    assert first.embedded == 1
    assert second.scanned == 0, "the repair did not take the row out of its own selector"


@pytest.mark.integration
async def test_repair_provenance_skips_rows_with_no_content_hash(engine, monkeypatch):
    """A row with nothing to attest to must not be scanned at all.

    Not a tidiness filter — an infinite-work guard. The repair stamps
    ``embedded_content_hash`` FROM ``content_hash``, so a row without one is
    stamped NULL again and still matches the selector. Including it would spend
    one provider call per row per run, forever, changing nothing.
    """
    from datetime import UTC, datetime

    _patch_embedding(monkeypatch)
    t = f"test-repair-nohash-{uuid.uuid4().hex[:8]}"
    await _seed_embedded(
        engine, t, content="body", content_hash=None, created_at=datetime(2026, 9, 1, tzinfo=UTC)
    )

    report = await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)

    assert report.scanned == 0, (
        "a row with no content_hash was scanned; the repair cannot make progress "
        "on it, so every run would re-embed it and change nothing"
    )


@pytest.mark.integration
async def test_repair_provenance_leaves_legacy_rows_alone_by_default(engine, monkeypatch):
    """Pre-037 rows cost money and are not known to be damaged.

    ``unknown_provenance`` holds tens of thousands of them. Sweeping them by
    default would turn a defect cleanup into an unbounded provider spend that
    nobody approved, so the default scan stops at the cutoff and
    ``--include-legacy`` opts in.
    """
    from datetime import UTC, datetime

    _patch_embedding(monkeypatch)
    t = f"test-repair-legacy-{uuid.uuid4().hex[:8]}"
    legacy = await _seed_embedded(
        engine, t, content="old", content_hash="c" * 64, created_at=datetime(2026, 8, 1, tzinfo=UTC)
    )

    default_run = await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)
    assert default_run.scanned == 0, "a pre-037 row was swept without --include-legacy"
    _e, _ch, provenance = await _read_memory(engine, legacy)
    assert provenance is None

    widened = await bf._backfill_one_table(
        engine,
        _memories_spec(),
        tenant_id=t,
        batch_size=100,
        max_inflight=4,
        dry_run=False,
        mode=bf._ScanMode.REPAIR_PROVENANCE,
        include_legacy=True,
    )
    assert widened.embedded == 1
    _e2, _ch2, provenance2 = await _read_memory(engine, legacy)
    assert provenance2 == "c" * 64


@pytest.mark.integration
async def test_repair_provenance_re_embeds_rather_than_stamping(engine, monkeypatch):
    """The vector must be recomputed, not just attested.

    Stamping ``embedded_content_hash = content_hash`` on the existing vector
    would assert that vector describes the current text — which is precisely
    what nothing knows here, since that is the state being repaired. It happened
    to hold for the 241 production rows (each verified first against a
    recomputed hash) and does not hold in general; a wrong hash reads downstream
    as verified freshness, worse than the NULL it replaced.

    The seeded vector is all 0.9 and the provider returns all 0.125, so a
    changed vector proves a real embed rather than a bare UPDATE of the hash.
    """
    from datetime import UTC, datetime

    _patch_embedding(monkeypatch)
    t = f"test-repair-reembed-{uuid.uuid4().hex[:8]}"
    mid = await _seed_embedded(
        engine, t, content="body", content_hash="d" * 64, created_at=datetime(2026, 9, 1, tzinfo=UTC)
    )

    await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)

    async with engine.connect() as conn:
        vec = (
            await conn.execute(text("SELECT embedding::text FROM memories WHERE id = :id"), {"id": mid})
        ).scalar()
    assert vec is not None
    assert vec.startswith("[0.125"), (
        "the stored vector was not recomputed; the repair stamped provenance onto "
        "whatever was already there, asserting a freshness it never verified"
    )


@pytest.mark.integration
async def test_repair_provenance_drives_the_alert_metric_to_zero(engine, monkeypatch):
    """End to end: the sweep empties the bucket the alert fires on.

    Asserted through ``memory_embedding_coverage_for_tenant`` rather than a
    hand-written predicate, so this fails if the sweep regresses OR if the
    metric stops measuring the population the sweep targets. Those two must
    agree, and nothing but this ties them together.
    """
    from datetime import UTC, datetime

    from core_storage_api.services.postgres_service import PostgresService

    _patch_embedding(monkeypatch)
    t = f"test-repair-metric-{uuid.uuid4().hex[:8]}"
    for i in range(3):
        await _seed_embedded(
            engine,
            t,
            content=f"body-{i}",
            content_hash=f"{i}" * 64,
            created_at=datetime(2026, 9, 1, tzinfo=UTC),
        )

    svc = PostgresService()
    *_, before = await svc.memory_embedding_coverage_for_tenant(t)
    assert before == 3, "fixture did not create the alertable population"

    await _run(engine, _memories_spec(), t, mode=bf._ScanMode.REPAIR_PROVENANCE)

    *_, after = await svc.memory_embedding_coverage_for_tenant(t)
    assert after == 0, f"sweep left {after} row(s) in the population the alert fires on"
