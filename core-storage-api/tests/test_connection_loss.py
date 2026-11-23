"""A database connection that goes away under a request.

Cloud Run resets outbound connections now and then. In prod over the 7 days to
2026-11-19 that failed 6 requests with a 500: 5 while opening a connection
through the AlloyDB Auth Proxy, and 1 mid-statement on ``GET /tenants/active``
(06:12). The proxy below stands in for the AlloyDB one and resets connections
the same two ways.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import struct

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from core_storage_api.config import settings
from core_storage_api.database import connection_loss
from core_storage_api.database.connection_loss import is_connection_loss
from core_storage_api.database.init import _build_engine
from core_storage_api.services import postgres_service

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _reset(writer: asyncio.StreamWriter) -> None:
    # A zero linger turns the close into an RST, which is what the client saw.
    writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    writer.transport.abort()


# PostgreSQL's SSLRequest: length 8, then this code.
_SSL_REQUEST = struct.pack("!ii", 8, 80877103)


class _ResettingProxy:
    """Forwards to the test database. ``drop_opening`` resets that many new
    connections once the client has sent its startup message, as the AlloyDB
    proxy did when it could not reach the database. ``drop_on`` resets the
    connection carrying the first statement that contains it."""

    def __init__(self) -> None:
        self.accepted = 0
        self.drop_opening = 0
        self.drop_opening_after = 0.0
        self.drop_on: bytes | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._upstream = make_url(settings.database_url.get_secret_value())

    async def __aenter__(self) -> _ResettingProxy:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = self._upstream.set(host="127.0.0.1", port=port).render_as_string(hide_password=False)
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.close()
        for writer in self._writers:
            writer.transport.abort()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        self._writers.append(writer)
        if self.drop_opening:
            self.drop_opening -= 1
            if await reader.read(1024) == _SSL_REQUEST:
                # Decline TLS, as a local proxy does, so the reset comes where
                # it came in prod: after the startup message.
                writer.write(b"N")
                await reader.read(1024)
            await asyncio.sleep(self.drop_opening_after)
            _reset(writer)
            return
        up_reader, up_writer = await asyncio.open_connection(self._upstream.host, self._upstream.port or 5432)
        self._writers.append(up_writer)

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, *, watch: bool) -> None:
            with contextlib.suppress(OSError):
                while data := await src.read(65536):
                    if watch and self.drop_on and self.drop_on in data:
                        self.drop_on = None
                        _reset(writer)
                        _reset(up_writer)
                        return
                    dst.write(data)
                    await dst.drain()
                dst.close()

        await asyncio.gather(pump(reader, up_writer, watch=True), pump(up_reader, writer, watch=False))


async def test_opening_a_connection_retries_one_that_was_dropped() -> None:
    async with _ResettingProxy() as proxy:
        engine = _build_engine(proxy.url)
        try:
            proxy.drop_opening = 2
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT 1"))).scalar() == 1
            assert proxy.accepted == 3
        finally:
            await engine.dispose()


async def test_each_attempt_runs_do_connect_listeners_added_later() -> None:
    # A retry that skipped them would connect without what they set.
    async with _ResettingProxy() as proxy:
        engine = _build_engine(proxy.url)
        attempts = []

        @event.listens_for(engine.sync_engine, "do_connect")
        def _count(dialect, record, cargs, cparams):
            attempts.append(cparams["user"])

        try:
            proxy.drop_opening = 1
            async with engine.connect():
                pass
            assert len(attempts) == 2
            # ``dispose`` builds a new pool, and the retry goes with it.
            await engine.dispose()
            proxy.drop_opening = 1
            async with engine.connect():
                pass
            assert len(attempts) == 4
        finally:
            await engine.dispose()


async def test_connect_retries_stop_after_three_attempts() -> None:
    # An outage must still fail, fast enough to reach the alerts.
    async with _ResettingProxy() as proxy:
        engine = _build_engine(proxy.url)
        try:
            proxy.drop_opening = 3
            with pytest.raises(DBAPIError) as raised:
                async with engine.connect():
                    pass
            assert is_connection_loss(raised.value)
            assert proxy.accepted == 3
        finally:
            await engine.dispose()


async def test_a_connect_that_fails_slowly_is_retried(monkeypatch) -> None:
    # Prod 2026-11-20 to 11-23: the proxy's IAM check failed 6 connects at its
    # 5s deadline, past the retry window, and each request answered 500. The
    # next connect would have passed.
    monkeypatch.setattr(connection_loss, "_CONNECT_RETRY_WINDOW_S", 0.2)
    async with _ResettingProxy() as proxy:
        engine = _build_engine(proxy.url)
        try:
            proxy.drop_opening = 1
            proxy.drop_opening_after = 0.3
            async with engine.connect() as conn:
                assert (await conn.execute(text("SELECT 1"))).scalar() == 1
            assert proxy.accepted == 2
        finally:
            await engine.dispose()


async def test_a_connect_that_keeps_failing_slowly_is_retried_only_once(monkeypatch) -> None:
    # More retries would make the request take several times as long to fail.
    monkeypatch.setattr(connection_loss, "_CONNECT_RETRY_WINDOW_S", 0.2)
    async with _ResettingProxy() as proxy:
        engine = _build_engine(proxy.url)
        try:
            proxy.drop_opening = 3
            proxy.drop_opening_after = 0.3
            with pytest.raises(DBAPIError) as raised:
                async with engine.connect():
                    pass
            assert is_connection_loss(raised.value)
            assert proxy.accepted == 2
        finally:
            await engine.dispose()


async def test_a_refused_login_is_not_retried() -> None:
    async with _ResettingProxy() as proxy:
        engine = _build_engine(
            make_url(proxy.url).set(password="wrong").render_as_string(hide_password=False)
        )
        try:
            with pytest.raises(DBAPIError) as raised:
                async with engine.connect():
                    pass
            assert not is_connection_loss(raised.value)
            assert proxy.accepted == 1
        finally:
            await engine.dispose()


async def test_a_read_that_loses_its_connection_mid_statement_answers_503(client, monkeypatch) -> None:
    # Prod 2026-11-19 06:12:07: the connection under core-api's background
    # tenant discovery dropped mid-statement. It answered 500, which core-api
    # does not retry. A 503 it retries, and the retry finds a new connection.
    async with _ResettingProxy() as proxy:
        engine = _build_engine(proxy.url)
        monkeypatch.setattr(
            postgres_service, "_session_factory", async_sessionmaker(engine, expire_on_commit=False)
        )
        try:
            proxy.drop_on = b"due_tenants"
            path = "/api/v1/storage/tenants/active"
            dropped = await client.get(path, params={"background_due": "true"})
            assert proxy.drop_on is None
            assert dropped.status_code == 503
            assert dropped.headers["retry-after"] == "1"
            again = await client.get(path, params={"background_due": "true"})
            assert again.status_code == 200
        finally:
            await engine.dispose()


def _db_error(statement: str | None, cause: BaseException, *, invalidated: bool) -> DBAPIError:
    try:
        raise DBAPIError(statement, None, cause, connection_invalidated=invalidated) from cause
    except DBAPIError as exc:
        return exc


_DROPPED = asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed in the middle of operation")


@pytest.mark.parametrize(
    ("method", "error"),
    [
        # core-api repeats a 503 for PATCH, DELETE and idempotent POSTs, and a
        # write whose COMMIT was in flight may have committed.
        ("POST", _db_error("INSERT", _DROPPED, invalidated=True)),
        ("PATCH", _db_error("UPDATE", _DROPPED, invalidated=True)),
        # A connection that could not be opened has had its retries.
        ("GET", _db_error(None, _DROPPED, invalidated=False)),
        # The same deadlock happens again on a retry.
        ("GET", _db_error("SELECT", asyncpg.exceptions.DeadlockDetectedError("deadlock"), invalidated=False)),
    ],
    ids=["write-dropped", "patch-dropped", "read-could-not-connect", "read-deadlock"],
)
async def test_only_a_read_that_lost_its_connection_answers_503(method, error) -> None:
    from core_storage_api.app import create_app

    app = create_app()

    async def fail() -> None:
        raise error

    app.add_api_route("/fail", fail, methods=[method])
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Storage-Secret": settings.core_storage_shared_secret.get_secret_value()},
    ) as client:
        # Unhandled, so the server answers 500, as before.
        with pytest.raises(DBAPIError):
            await client.request(method, "/fail")
