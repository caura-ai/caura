"""``POST /ingest/commit`` charges one write unit per fact, not per request.

It charged a flat 1 for a commit that writes ``len(body.facts)`` memories. Two
consequences, and the second is the one that matters: the writes counter feeds
``_is_over_plan_limits``, so the cheapest route past a write cap was to ingest
in bulk rather than write one at a time.

Every other multi-item write path already meters the count —
``caura_write``'s bulk branch calls ``bulk_check_and_increment(tenant_id,
len(bulk_items))``. This is the parity the finding names.
"""

from __future__ import annotations

import pytest

from core_api.routes import memories as memories_route

pytestmark = [pytest.mark.asyncio]


class _Auth:
    """The narrowest AuthContext stand-in the endpoint actually reads."""

    tenant_id = "t-meter"
    is_install_credential = False
    install_uuid = None

    def enforce_read_only(self) -> None: ...
    def enforce_usage_limits(self) -> None: ...
    def enforce_tenant(self, tenant_id: str) -> None: ...


def _body(n: int):
    from core_api.schemas import IngestCommitRequest

    return IngestCommitRequest(
        tenant_id="t-meter",
        facts=[{"content": f"fact {i}"} for i in range(n)],
    )


@pytest.mark.parametrize("n_facts", [1, 3, 50])
async def test_a_commit_is_charged_once_per_fact(monkeypatch, n_facts: int) -> None:
    charged: list[tuple[str, int]] = []

    async def _bulk(tenant_id: str, count: int):
        charged.append((tenant_id, count))

    async def _flat(tenant_id: str, operation: str, count: int = 1):
        raise AssertionError(
            f"per-request metering is back: check_and_increment({tenant_id!r}, {operation!r})"
        )

    async def _commit(body):
        return {"created": n_facts}

    monkeypatch.setattr(memories_route, "bulk_check_and_increment", _bulk)
    monkeypatch.setattr(memories_route, "check_and_increment", _flat)
    monkeypatch.setattr(memories_route, "ingest_commit", _commit)

    await memories_route.ingest_commit_endpoint(
        request=None, body=_body(n_facts), response=None, auth=_Auth()
    )

    assert charged == [("t-meter", n_facts)]


async def test_the_charge_lands_before_the_write(monkeypatch) -> None:
    """Ordering, pinned for the same reason ``test_billing_happens_before_the_write``
    pins it on the MCP surface: a batch that fails partway still costs what it
    attempted, and two orderings for one operation is the drift that keeps
    recurring in this area."""
    order: list[str] = []

    async def _bulk(tenant_id: str, count: int):
        order.append("meter")

    async def _commit(body):
        order.append("write")
        return {}

    monkeypatch.setattr(memories_route, "bulk_check_and_increment", _bulk)
    monkeypatch.setattr(memories_route, "ingest_commit", _commit)

    await memories_route.ingest_commit_endpoint(
        request=None, body=_body(2), response=None, auth=_Auth()
    )

    assert order == ["meter", "write"]


async def test_an_admin_commit_is_not_metered(monkeypatch) -> None:
    """``if auth.tenant_id:  # skip for admin`` — preserved, not incidental."""
    charged: list[int] = []

    async def _bulk(tenant_id: str, count: int):
        charged.append(count)

    async def _commit(body):
        return {}

    monkeypatch.setattr(memories_route, "bulk_check_and_increment", _bulk)
    monkeypatch.setattr(memories_route, "ingest_commit", _commit)

    admin = _Auth()
    admin.tenant_id = None

    await memories_route.ingest_commit_endpoint(
        request=None, body=_body(4), response=None, auth=admin
    )

    assert charged == []
