"""A refused review answers with storage's reason, not storage's address.

core-api turned storage's refusal of a review into ``HTTPException(409,
detail=str(exc))``. ``str()`` of the client's ``httpx.HTTPStatusError`` is
"Client error '409 Conflict' for url '<storage>/.../resolve'": it dropped why
storage refused (the record was already reviewed, or another record of the pair
holds the verdict) and sent storage's internal address to the browser. The
dashboard, seeing the same text for every refusal, could only say "Already
reviewed by someone else.", and the pilot dry run's duplicate pair (caura #2471)
looked like someone else's review.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from core_api.auth import AuthContext
from core_api.routes import conflicts
from core_api.schemas import ConflictResolveRequest

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]

_STORAGE_URL = (
    "http://storage.internal/api/v1/storage/memories/memory-conflicts/c1/resolve"
)


def _refusal(status: int, response: httpx.Response) -> httpx.HTTPStatusError:
    """What the storage client raises when storage refuses a review."""
    request = httpx.Request("PATCH", _STORAGE_URL)
    response.request = request
    return httpx.HTTPStatusError(
        f"Client error '{status} Conflict' for url '{_STORAGE_URL}'",
        request=request,
        response=response,
    )


async def _refused(monkeypatch, error: Exception) -> HTTPException:
    sc = AsyncMock()
    sc.resolve_memory_conflict = AsyncMock(side_effect=error)
    monkeypatch.setattr(conflicts, "get_storage_client", lambda: sc)
    monkeypatch.setattr(conflicts, "_reviewer", AsyncMock(return_value="u-7"))
    monkeypatch.setattr(conflicts, "log_action", AsyncMock())
    body = ConflictResolveRequest(tenant_id="t1", review_status="resolved")
    with pytest.raises(HTTPException) as refused:
        await conflicts.resolve_conflict(
            "c1", body, auth=AuthContext(tenant_id="t1", is_person=True, user_id="u-7")
        )
    return refused.value


@pytest.mark.parametrize(
    "reason",
    [
        "Conflict already reviewed (review_status=resolved)",
        "Another verdict still holds this pair",
        "Conflict chain has changed",
    ],
)
async def test_a_refused_review_answers_with_storages_reason(monkeypatch, reason):
    refused = await _refused(
        monkeypatch, _refusal(409, httpx.Response(409, json={"detail": reason}))
    )

    assert refused.status_code == 409
    assert refused.detail == reason


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(409, text="<html>upstream busy</html>"),
        httpx.Response(409, json={"detail": ""}),
        httpx.Response(409, json=["not", "an", "object"]),
    ],
    ids=["not json", "empty detail", "not an object"],
)
async def test_a_refusal_without_a_reason_still_names_no_storage_address(
    monkeypatch, response
):
    refused = await _refused(monkeypatch, _refusal(409, response))

    assert refused.status_code == 409
    assert refused.detail == "Conflict could not be reviewed"
    assert "storage.internal" not in refused.detail
