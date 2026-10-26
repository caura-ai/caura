"""The body budget is enforced before routing, including unknown-length bodies."""

import httpx
import pytest
from fastapi import FastAPI, Request

from core_api.middleware.ingest_body_size import IngestBodySizeMiddleware

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "path,cap", [("/memories", 4000000), ("/ingest/file", 3000000)]
)
@pytest.mark.parametrize("chunked", [False, True])
async def test_body_cap_boundary_before_handler(path, cap, chunked):
    app = FastAPI()
    invoked = []

    @app.post(path)
    async def endpoint(request: Request):
        invoked.append(True)
        return {"size": len(await request.body())}

    app.add_middleware(IngestBodySizeMiddleware)

    async def chunks(size):
        for start in range(0, size, 10000):
            yield b"x" * min(10000, size - start)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        for size, status in ((cap, 200), (cap + 1, 413)):
            invoked.clear()
            content = chunks(size) if chunked else b"x" * size
            response = await c.post(path, content=content)
            assert response.status_code == status
            if status == 413:
                subject = "File" if path.startswith("/ingest/") else "Request body"
                assert response.json()["detail"].startswith(
                    f"{subject} must be {cap // 1000000} MB or under"
                )
            assert bool(invoked) == (status == 200)


async def test_disconnect_is_forwarded_after_the_buffered_body():
    messages = iter(
        [
            {"type": "http.request", "body": b"ok", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )
    seen = []

    async def receive():
        return next(messages)

    async def send(message):
        pass

    async def downstream(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())

    await IngestBodySizeMiddleware(downstream)(
        {"type": "http", "path": "/events", "headers": []}, receive, send
    )
    assert seen[-1] == {"type": "http.disconnect"}
