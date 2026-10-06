"""Only storage endpoint conflicts become relation input errors (audit M-42)."""

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from core_api.schemas import RelationUpsert
from core_api.services import entity_service

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize("status", [403, 409, 422, 503])
async def test_relation_storage_status_translation(monkeypatch, status):
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "http://storage.invalid/entities/relations"),
        json={"detail": "private upstream diagnostic"},
    )
    error = httpx.HTTPStatusError(
        "upstream failure", request=response.request, response=response
    )
    storage = AsyncMock()
    storage.create_relation.side_effect = error
    monkeypatch.setattr(entity_service, "get_storage_client", lambda: storage)
    body = RelationUpsert(
        tenant_id="tenant-test",
        from_entity_id=uuid4(),
        to_entity_id=uuid4(),
        relation_type="knows",
    )
    if status == 409:
        with pytest.raises(HTTPException) as caught:
            await entity_service.upsert_relation(body)
        assert caught.value.status_code == 422
        assert caught.value.detail == (
            "from_entity_id or to_entity_id does not exist in this tenant"
        )
    else:
        with pytest.raises(httpx.HTTPStatusError) as caught_upstream:
            await entity_service.upsert_relation(body)
        assert caught_upstream.value is error
    storage.create_relation.assert_awaited_once()
