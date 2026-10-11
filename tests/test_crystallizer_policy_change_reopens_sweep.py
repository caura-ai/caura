"""Loosening a crystallizer sweep setting reopens the rows it settled (M-38).

The dedup sweep stamps a row ``last_dedup_checked_at`` once its cluster reaches
an outcome, and a stamped row is never a sweep candidate again. Three of those
outcomes are policy, not fact: a cluster below ``min_cluster_size``, rows with no
neighbour at ``dedup_threshold``, and pairs swept while ``auto_crystallize`` was
off. The stamp never expired, so loosening any of the three changed nothing for
rows already settled under the old value.

A settings write that touches one of them now clears the tenant's stamps, so the
next run sweeps those rows under the new policy. The reset runs in the
background, one bounded storage call at a time, so the save does not wait on a
large tenant. It never fails the settings write, and a reset that fails is
logged at ERROR and recorded as a failed background task: re-saving the same
value is a no-op, so nothing else would retry it.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import pytest

from core_api.services import organization_settings as os_svc
from core_api.services import task_tracker

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_DONE = {"reset": 0, "done": True}


@pytest.fixture(autouse=True)
def _reset_cache():
    os_svc._settings_cache.clear()
    yield
    os_svc._settings_cache.clear()


@pytest.fixture
def background(monkeypatch) -> list:
    """The tasks ``update_settings`` hands to ``track_task``, for a test to await."""
    started: list = []

    def track(coro):
        task = asyncio.ensure_future(coro)
        started.append(task)
        return task

    monkeypatch.setattr(os_svc, "track_task", track, raising=False)
    return started


async def _settle(background: list) -> None:
    await asyncio.gather(*background)


def _storage(monkeypatch, *, changed: bool = True, resets=None) -> AsyncMock:
    sc = AsyncMock()

    async def update_org_settings(tenant_id, new_settings, *, changed_by=None):
        return {"settings": new_settings, "changed": changed}

    sc.update_org_settings = AsyncMock(side_effect=update_org_settings)
    sc.reset_dedup_checked = AsyncMock(side_effect=resets, return_value=_DONE)
    monkeypatch.setattr(os_svc, "get_storage_client", lambda: sc)
    monkeypatch.setattr(task_tracker, "get_storage_client", lambda: sc)
    monkeypatch.setattr(os_svc, "get_event_bus", lambda: AsyncMock())
    return sc


@pytest.mark.parametrize(
    "crystallizer",
    [
        {"min_cluster_size": 2},
        {"dedup_threshold": 0.9},
        {"auto_crystallize": True},
    ],
)
async def test_a_sweep_setting_change_reopens_the_settled_rows(
    monkeypatch, background, crystallizer
):
    sc = _storage(monkeypatch)
    await os_svc.update_settings("t1", {"crystallizer": crystallizer})
    await _settle(background)
    sc.reset_dedup_checked.assert_awaited_once_with("t1")


async def test_the_settings_write_does_not_wait_for_the_reset(monkeypatch, background):
    sc = _storage(monkeypatch)
    release = asyncio.Event()

    async def slow_reset(tenant_id):
        await release.wait()
        return _DONE

    sc.reset_dedup_checked.side_effect = slow_reset
    out = await asyncio.wait_for(
        os_svc.update_settings("t1", {"crystallizer": {"min_cluster_size": 2}}), 1
    )
    assert out["crystallizer"]["min_cluster_size"] == 2

    release.set()
    await _settle(background)
    sc.reset_dedup_checked.assert_awaited_once_with("t1")


async def test_the_reset_drains_a_large_tenant_one_call_at_a_time(
    monkeypatch, background
):
    more = {"reset": 5000, "done": False}
    sc = _storage(monkeypatch, resets=[more, more, {"reset": 7, "done": True}])
    await os_svc.update_settings("t1", {"crystallizer": {"dedup_threshold": 0.9}})
    await _settle(background)
    assert sc.reset_dedup_checked.await_count == 3


async def test_a_failed_reset_is_recorded_as_a_failed_background_task(
    monkeypatch, background, caplog
):
    sc = _storage(monkeypatch, resets=RuntimeError("storage unavailable"))
    with caplog.at_level(logging.ERROR):
        await os_svc.update_settings("t1", {"crystallizer": {"min_cluster_size": 2}})
        await _settle(background)

    assert any(r.levelno == logging.ERROR for r in caplog.records)
    sc.add_task_failure.assert_awaited_once()
    row = sc.add_task_failure.await_args.args[0]
    assert (row["task_name"], row["tenant_id"], row["status"]) == (
        "crystallizer_reopen_sweep",
        "t1",
        "failed",
    )


async def test_an_unrelated_settings_change_does_not_reopen_the_sweep(
    monkeypatch, background
):
    sc = _storage(monkeypatch)
    await os_svc.update_settings("t1", {"dedup": {"semantic_dedup_enabled": True}})
    await _settle(background)
    sc.reset_dedup_checked.assert_not_awaited()


async def test_a_no_op_settings_write_does_not_reopen_the_sweep(
    monkeypatch, background
):
    sc = _storage(monkeypatch, changed=False)
    await os_svc.update_settings("t1", {"crystallizer": {"min_cluster_size": 2}})
    await _settle(background)
    sc.reset_dedup_checked.assert_not_awaited()


async def test_a_failed_reset_does_not_fail_the_settings_write(monkeypatch, background):
    _storage(monkeypatch, resets=RuntimeError("storage unavailable"))
    out = await os_svc.update_settings("t1", {"crystallizer": {"min_cluster_size": 2}})
    await _settle(background)
    assert out["crystallizer"]["min_cluster_size"] == 2
