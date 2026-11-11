"""A memory map fit says where its time went.

A fit pages up to 50,000 rows with their embeddings, decodes them, fits the
layout on a sample, places every row and saves the model and the positions.
Request latency cannot tell those parts apart, and a large tenant's fit runs
in the background, outside any request, so each finished fit logs one line:
the points, the sample and the pages, and the milliseconds spent in storage,
decoding, the layout and saving. A read of a saved map logs nothing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging

import numpy as np
import pytest

from core_api.services import memory_field

pytestmark = pytest.mark.unit

TENANT = "map-timing"
SCOPE = {"tenant_id": TENANT, "include_scope_agent": True}


def _row(
    i: int,
    visibility: str = "scope_team",
    fleet_id: str | None = None,
    created_at: str = "2026-11-10T00:00:00+00:00",
) -> dict:
    vec = np.zeros(8, dtype="<f4")
    vec[i % 8] = 1.0
    vec[(i + 3) % 8] = 0.1 * (i % 5)
    return {
        "id": f"m-{i}",
        "title": None,
        "preview": f"memory {i}",
        "created_at": created_at,
        "entity_ids": [],
        "row_version": str(1000 + i),
        "visibility": visibility,
        "fleet_id": fleet_id,
        "embedding_b64": base64.b64encode(vec.tobytes()).decode(),
    }


class _Storage:
    """core-storage's field page, model and positions, in memory."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.model: dict | None = None
        self.claim: str | None = None
        self.positions: dict[tuple[str, str], tuple[float, float, int]] = {}
        self.claims = 0
        self.signatures = 0
        self.fit_page_delay = 0.0

    def _visible(self, body: dict) -> list[dict]:
        # As storage scopes the field: agent-private rows only for a scope
        # that includes them; the fit's tenant-wide scope does not.
        rows = self.rows
        if body.get("fleet_id"):
            rows = [r for r in rows if r["fleet_id"] == body["fleet_id"]]
        if body.get("include_scope_agent"):
            return rows
        return [r for r in rows if r["visibility"] != "scope_agent"]

    async def memory_field_signature(self, scope: dict) -> dict:
        self.signatures += 1
        visible = self._visible(scope)
        return {
            "count": len(visible),
            "embedded": len(visible),
            "newest": "t",
            "revision": 1,
        }

    async def memory_field_page(self, body: dict) -> dict:
        if body.get("embeddings") == "all" and body.get("model_version"):
            await asyncio.sleep(self.fit_page_delay)  # a fit's page
        rows = self._visible(body)
        start = 0
        if "cursor_id" in body:
            start = (
                next(i for i, r in enumerate(rows) if r["id"] == body["cursor_id"]) + 1
            )
        page = [dict(r) for r in rows[start : start + body["limit"]]]
        version = body.get("model_version")
        for r in page:
            placed = self.positions.get((r["id"], version)) if version else None
            if version:
                r["x"], r["y"], r["cluster"] = placed or (None, None, None)
            embeddings = body.get("embeddings", "all")
            if embeddings == "none" or (embeddings == "unplaced" and placed):
                r["embedding_b64"] = None
        more = start + len(page) < len(rows)
        cursor = (
            {"ts": page[-1]["created_at"], "id": page[-1]["id"]}
            if page and more
            else None
        )
        return {"rows": page, "entities": [], "next_cursor": cursor}

    async def projection_model_get(self, tenant_id: str) -> dict:
        model = self.model or {}
        return {
            "tenant_id": tenant_id,
            "model_version": model.get("model_version"),
            "kind": model.get("kind"),
            "dim": model.get("dim"),
            "n_fit": model.get("n_fit"),
            "params_b64": model.get("params_b64"),
            "fitted_at": model.get("fitted_at"),
            "fitting": self.claim is not None,
        }

    async def projection_claim(
        self, tenant_id: str, claim_token: str, lease_seconds: int
    ) -> dict:
        self.claims += 1
        claimed = self.claim in (None, claim_token)
        if claimed:
            self.claim = claim_token
        return {
            "claimed": claimed,
            "model_version": (self.model or {}).get("model_version"),
            "fitted_at": None,
        }

    async def projection_release(self, tenant_id: str, claim_token: str) -> bool:
        if self.claim != claim_token:
            return False
        self.claim = None
        return True

    async def projection_model_save(
        self, tenant_id: str, claim_token: str, *, params: bytes, **model
    ) -> bool:
        if self.claim != claim_token:
            return False
        self.model = {
            **model,
            "params_b64": base64.b64encode(params).decode(),
            "fitted_at": "2026-11-10T00:00:00+00:00",
        }
        self.claim = None
        return True

    async def projection_positions_save(
        self,
        tenant_id: str,
        model_version: str,
        items: list[dict],
        claim_token: str | None = None,
    ) -> int | None:
        current = (self.model or {}).get("model_version")
        if model_version != current and (
            claim_token is None or claim_token != self.claim
        ):
            return None
        for item in items:
            self.positions[(item["memory_id"], model_version)] = (
                item["x"],
                item["y"],
                item["cluster"],
            )
        return len(items)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    @contextlib.asynccontextmanager
    async def no_slot(*_args, **_kwargs):
        yield

    monkeypatch.setattr(memory_field, "per_tenant_slot", no_slot)
    memory_field._reset_state()
    yield
    memory_field._reset_state()


def _fit_lines(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "path", None) == "memory-field-fit"]


async def test_a_fit_logs_its_points_pages_and_where_the_time_went(monkeypatch, caplog):
    monkeypatch.setattr(memory_field, "FIELD_PAGE_SIZE", 2)
    storage = _Storage([_row(i) for i in range(9)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)

    with caplog.at_level(logging.INFO, logger=memory_field.logger.name):
        payload = await memory_field._get_memory_field(SCOPE, limit=20000)

    assert len(payload["points"]) == 9
    (record,) = _fit_lines(caplog)
    assert record.levelno == logging.INFO
    assert TENANT in record.getMessage()
    assert (
        record.tenant_id,
        record.points,
        record.embedded,
        record.sample,
        record.pages,
    ) == (TENANT, 9, 9, 9, 5)
    for part in ("storage_ms", "decode_ms", "layout_ms", "save_ms", "total_ms"):
        assert isinstance(getattr(record, part), int) and getattr(record, part) >= 0
    assert record.total_ms >= record.layout_ms
    assert storage.model["model_version"] == record.model_version


async def test_a_read_of_a_saved_map_logs_no_fit(monkeypatch, caplog):
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    await memory_field._get_memory_field(SCOPE, limit=20000)
    memory_field._reset_state()  # another process: no cached payload, the saved model in storage
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=memory_field.logger.name):
        payload = await memory_field._get_memory_field(SCOPE, limit=20000)

    assert len(payload["points"]) == 6
    assert not _fit_lines(caplog)


async def test_a_large_tenant_fits_in_the_background(monkeypatch, caplog):
    monkeypatch.setattr(memory_field, "FIT_INLINE_MAX", 4)
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)

    with caplog.at_level(logging.INFO, logger=memory_field.logger.name):
        with pytest.raises(memory_field.FieldPreparingError):
            await memory_field._get_memory_field(SCOPE, limit=20000)
        # Another request while the fit runs: still preparing, no second fit.
        with pytest.raises(memory_field.FieldPreparingError):
            await memory_field._get_memory_field(SCOPE, limit=20000)
        async with asyncio.timeout(10):
            await asyncio.gather(*memory_field._fits.values())

    assert len(_fit_lines(caplog)) == 1
    payload = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert len(payload["points"]) == 6


async def test_concurrent_first_requests_in_one_process_share_one_fit(
    monkeypatch, caplog
):
    """The claim is a storage round trip. Requests that arrive together must
    not each make one: the losers would answer that the map is being prepared
    while their own process fits it."""
    storage = _Storage([_row(i) for i in range(6)])
    claim = storage.projection_claim
    claims = 0

    async def slow_claim(*args, **kwargs):
        nonlocal claims
        claims += 1
        await asyncio.sleep(0.05)
        return await claim(*args, **kwargs)

    monkeypatch.setattr(storage, "projection_claim", slow_claim)
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    with caplog.at_level(logging.INFO, logger=memory_field.logger.name):
        payloads = await asyncio.gather(
            *(memory_field._get_memory_field(SCOPE, limit=20000) for _ in range(3))
        )

    assert [len(p["points"]) for p in payloads] == [6, 6, 6]
    assert claims == 1
    assert len(_fit_lines(caplog)) == 1


async def test_a_failed_fit_holds_the_tenant_off_fitting_again(monkeypatch):
    """A fit pages up to 50,000 embeddings. One that failed is not tried again
    by every retry: for ``FIT_RETRY_SECONDS`` requests answer busy instead."""
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)

    def broken(vectors):
        raise RuntimeError("fit failed")

    monkeypatch.setattr(memory_field, "fit_model", broken)
    for _ in range(3):
        memory_field.field_cache.clear()
        with pytest.raises(memory_field.FieldBusyError):
            await memory_field._get_memory_field(SCOPE, limit=20000)
    assert storage.claims == 1
    assert storage.claim is None  # given back

    monkeypatch.setattr(memory_field, "FIT_RETRY_SECONDS", 0.0)
    memory_field._fit_outcomes.clear()
    with pytest.raises(memory_field.FieldBusyError):
        await memory_field._get_memory_field(SCOPE, limit=20000)
    assert storage.claims == 2  # once the hold is over, the next request tries again


async def test_too_few_shared_embeddings_are_laid_out_without_fitting_again(
    monkeypatch,
):
    """The caller sees seven embedded memories, four of them private; the
    tenant-wide fit finds three, too few to fit. The caller's map is laid out
    on request, and the next request does not page for a fit again."""
    rows = [_row(i) for i in range(3)] + [_row(i, "scope_agent") for i in range(3, 7)]
    storage = _Storage(rows)
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)

    payload = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert len(payload["points"]) == 7
    assert storage.model is None
    memory_field.field_cache.clear()  # a write since: the payload is rebuilt
    payload = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert len(payload["points"]) == 7
    assert storage.claims == 1


async def test_a_fit_that_loses_its_claim_stops_and_is_not_taken_for_too_few(
    monkeypatch,
):
    """Storage refuses a fit's positions once its claim has gone: another
    process is fitting the tenant. The fit stops at the first refused batch,
    saves no model, and the map is being prepared, not laid out on request."""
    monkeypatch.setattr(memory_field, "POSITIONS_BATCH", 2)
    storage = _Storage([_row(i) for i in range(6)])
    saves = 0

    async def refused(*_args, **_kwargs):
        nonlocal saves
        saves += 1
        return None

    monkeypatch.setattr(storage, "projection_positions_save", refused)
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    with pytest.raises(memory_field.FieldPreparingError):
        await memory_field._get_memory_field(SCOPE, limit=20000)
    assert saves == 1
    assert storage.model is None
    assert memory_field._fit_outcome(TENANT) is None


async def test_a_tenant_with_too_few_shared_memories_is_laid_out_within_bounds(
    monkeypatch,
):
    """Laid out on request, the caller's map is its newest ``ON_REQUEST_MAX``
    memories, one layout per slot, not all 50,000."""
    monkeypatch.setattr(memory_field, "ON_REQUEST_MAX", 5)
    rows = [_row(i) for i in range(3)] + [_row(i, "scope_agent") for i in range(3, 15)]
    storage = _Storage(rows)
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    real_build = memory_field.build_field
    held = []

    def build(*args, **kwargs):
        held.append(memory_field._fit_slots.locked())
        return real_build(*args, **kwargs)

    monkeypatch.setattr(memory_field, "build_field", build)
    monkeypatch.setattr(memory_field, "_fit_slots", asyncio.Semaphore(1))
    payload = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert len(payload["points"]) == 5
    assert payload["total"] == 15
    assert held == [True]


async def test_a_narrow_view_of_a_large_tenant_does_not_fit_inline(monkeypatch):
    """The caller sees four memories, one fleet's; the fit pages all nine
    shared ones. Inline or background is the fit's size, not the caller's."""
    monkeypatch.setattr(memory_field, "FIT_INLINE_MAX", 5)
    rows = [_row(i, fleet_id="f1") for i in range(4)] + [
        _row(i, fleet_id="f2") for i in range(4, 9)
    ]
    storage = _Storage(rows)
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    fleet_scope = {"tenant_id": TENANT, "fleet_id": "f1"}

    with pytest.raises(memory_field.FieldPreparingError):
        await memory_field._get_memory_field(fleet_scope, limit=20000)
    async with asyncio.timeout(10):
        await asyncio.gather(*memory_field._fits.values())
    payload = await memory_field._get_memory_field(fleet_scope, limit=20000)
    assert len(payload["points"]) == 4
    assert storage.model["n_fit"] == 9


async def test_an_inline_fit_that_runs_long_answers_preparing_and_finishes(monkeypatch):
    """An inline fit is a small tenant's, but a request waits for it only so
    long; then the map is being prepared, and the fit finishes regardless."""
    monkeypatch.setattr(memory_field, "FIT_INLINE_WAIT_SECONDS", 0.05)
    storage = _Storage([_row(i) for i in range(6)])
    storage.fit_page_delay = 0.3
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)

    with pytest.raises(memory_field.FieldPreparingError):
        await memory_field._get_memory_field(SCOPE, limit=20000)
    async with asyncio.timeout(10):
        await asyncio.gather(*memory_field._fits.values())
    assert storage.model is not None
    assert memory_field._fit_outcome(TENANT) is None
    payload = await memory_field._get_memory_field(SCOPE, limit=20000)
    assert len(payload["points"]) == 6


async def test_a_tenant_credentials_first_request_scans_the_signature_once(monkeypatch):
    """A tenant credential's view is the tenant-wide shared scope the fit
    pages (storage defaults ``caller_tenant_id`` to the tenant), so deciding
    how to fit costs it no second signature scan."""
    storage = _Storage([_row(i) for i in range(6)])
    monkeypatch.setattr(memory_field, "get_storage_client", lambda: storage)
    tenant_credential = {
        "tenant_id": TENANT,
        "caller_tenant_id": TENANT,
        "include_scope_agent": False,
    }
    payload = await memory_field._get_memory_field(tenant_credential, limit=20000)
    assert len(payload["points"]) == 6
    assert storage.signatures == 1
