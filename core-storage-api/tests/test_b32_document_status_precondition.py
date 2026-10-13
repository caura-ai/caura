"""A document write can name the statuses it may replace (L-24).

The skills inbox and Forge both read a skill, decide, and write the whole
document back. ``POST /documents`` replaced whatever was stored by then, so a
Forge re-mine that had read ``candidate`` could write ``candidate`` over an
operator's quarantine, and an inbox action could revert a concurrent one.

``expected_status`` lists the statuses the stored document may be in for the
write to land; ``None`` among them means it may also not exist yet. Otherwise
storage answers 409 ``document_status_changed`` and writes nothing. The check is
the write's own ``WHERE``, so nothing can land between them. A write without
``expected_status`` is unchanged.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from tests.test_integration import PREFIX

pytestmark = pytest.mark.asyncio

COLLECTION = "skills"


def _doc(tenant_id: str, doc_id: str, status: str, **extra) -> dict:
    return {
        "tenant_id": tenant_id,
        "collection": COLLECTION,
        "doc_id": doc_id,
        "data": {"slug": doc_id, "status": status, **extra},
    }


async def _stored(client: AsyncClient, tenant_id: str, doc_id: str) -> dict | None:
    resp = await client.get(f"{PREFIX}/documents/{COLLECTION}/{doc_id}", params={"tenant_id": tenant_id})
    if resp.status_code == 404:
        return None
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def _seed(client: AsyncClient, tenant_id: str, doc_id: str, status: str) -> None:
    resp = await client.post(f"{PREFIX}/documents", json=_doc(tenant_id, doc_id, status, note="seeded"))
    assert resp.status_code == 200, resp.text


@pytest.fixture
def ids() -> tuple[str, str]:
    return f"b32-{uuid.uuid4().hex[:8]}", f"forge/b32-{uuid.uuid4().hex[:8]}"


async def test_a_write_over_a_status_that_moved_is_refused(client: AsyncClient, ids) -> None:
    """The finding: Forge read ``candidate``, an operator quarantined the skill,
    and Forge's write arrives. It must not land."""
    tenant_id, doc_id = ids
    await _seed(client, tenant_id, doc_id, "quarantined")

    body = {**_doc(tenant_id, doc_id, "candidate"), "expected_status": [None, "candidate"]}
    resp = await client.post(f"{PREFIX}/documents", json=body)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "document_status_changed"
    stored = await _stored(client, tenant_id, doc_id)
    assert stored == {"slug": doc_id, "status": "quarantined", "note": "seeded"}


async def test_a_write_that_needs_the_document_does_not_create_it(client: AsyncClient, ids) -> None:
    """An inbox action expects the skill it loaded. If it is gone, the write
    must not bring it back."""
    tenant_id, doc_id = ids

    body = {**_doc(tenant_id, doc_id, "quarantined"), "expected_status": ["staged"]}
    resp = await client.post(f"{PREFIX}/documents", json=body)

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["error"] == "document_status_changed"
    assert await _stored(client, tenant_id, doc_id) is None


async def test_a_write_over_an_expected_status_lands(client: AsyncClient, ids) -> None:
    tenant_id, doc_id = ids
    await _seed(client, tenant_id, doc_id, "candidate")

    body = {**_doc(tenant_id, doc_id, "candidate", note="re-mined"), "expected_status": [None, "candidate"]}
    resp = await client.post(f"{PREFIX}/documents", json=body)

    assert resp.status_code == 200, resp.text
    assert (await _stored(client, tenant_id, doc_id))["note"] == "re-mined"


async def test_a_write_that_may_insert_creates_the_document(client: AsyncClient, ids) -> None:
    tenant_id, doc_id = ids

    body = {**_doc(tenant_id, doc_id, "candidate"), "expected_status": [None, "candidate"]}
    resp = await client.post(f"{PREFIX}/documents", json=body)

    assert resp.status_code == 200, resp.text
    assert (await _stored(client, tenant_id, doc_id))["status"] == "candidate"


async def test_a_write_without_a_precondition_still_replaces_any_status(client: AsyncClient, ids) -> None:
    """The control: every other caller of ``POST /documents`` is unchanged."""
    tenant_id, doc_id = ids
    await _seed(client, tenant_id, doc_id, "quarantined")

    resp = await client.post(f"{PREFIX}/documents", json=_doc(tenant_id, doc_id, "candidate"))

    assert resp.status_code == 200, resp.text
    assert (await _stored(client, tenant_id, doc_id))["status"] == "candidate"
