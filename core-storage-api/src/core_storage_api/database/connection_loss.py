"""Database connections that go away under a request.

Cloud Run resets an instance's outbound connections now and then, sometimes all
of them at once ("Outbound connection resets" in its container contract). Behind
the AlloyDB Auth Proxy that reaches this service in two ways, and in prod over
the 7 days to 2026-11-19 it failed 6 requests with a 500:

* 5 while opening a connection. The proxy accepted it, could not reach the
  database (a reset, or its IAM check failing), and dropped it. Nothing was
  sent, so :func:`retry_dropped_connects` opens it again.
* 1 while a statement ran (``GET /tenants/active``). SQLAlchemy marks that
  error ``connection_invalidated``. The app answers 503 for a read so core-api's
  GET retry repeats it, and keeps the 500 for anything else (``app.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time

from asyncpg.exceptions import ConnectionDoesNotExistError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.util import await_

logger = logging.getLogger(__name__)

# Three attempts, 0.1s then 0.2s apart. No retry starts more than 2s after the
# first attempt, so an outage, or a connect that fails slowly, fails about as
# fast as it did without retries and still reaches the alerts.
_CONNECT_ATTEMPTS = 3
_CONNECT_BACKOFF_S = 0.1
_CONNECT_RETRY_WINDOW_S = 2.0


def is_connection_loss(exc: BaseException) -> bool:
    """True when ``exc`` comes from the database connection itself going away.

    Follows the ``raise ... from`` chain, since SQLAlchemy wraps asyncpg's error
    and asyncpg wraps the socket's. Any other database error, a timeout
    included, is not a loss.
    """
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ConnectionError | ConnectionDoesNotExistError):
            return True
        current = current.__cause__
    return False


def retry_dropped_connects(engine: AsyncEngine) -> AsyncEngine:
    """Open a connection again when opening it failed because it was dropped.

    A failed login, a missing database or any other error is raised at once.

    It wraps the pool's creator, not a ``do_connect`` listener: SQLAlchemy uses
    the first listener that returns a connection, so a retrying one would skip
    every listener added after it. The creator runs them all on each attempt.
    The pool's ``recreate``, which ``dispose`` calls, keeps the wrapper.
    """
    pool = engine.sync_engine.pool
    create = pool._invoke_creator

    def _connect(connection_record=None):
        started = time.monotonic()
        attempt = 1
        while True:
            try:
                return create(connection_record)
            except Exception as exc:
                delay = _CONNECT_BACKOFF_S * 2 ** (attempt - 1)
                if (
                    attempt >= _CONNECT_ATTEMPTS
                    or not is_connection_loss(exc)
                    or time.monotonic() - started + delay > _CONNECT_RETRY_WINDOW_S
                ):
                    raise
                logger.warning(
                    "Database connection dropped while opening (%s); attempt %d of %d in %.1fs",
                    exc,
                    attempt + 1,
                    _CONNECT_ATTEMPTS,
                    delay,
                )
            # The pool opens connections inside SQLAlchemy's greenlet, where
            # asyncpg's own connect runs too, so it can wait without blocking.
            await_(asyncio.sleep(delay))
            attempt += 1

    pool._creator = _connect
    return engine
