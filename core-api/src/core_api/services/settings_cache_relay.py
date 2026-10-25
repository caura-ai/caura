"""Settings-cache invalidations between core-api processes, over Redis (#663).

``update_settings`` evicts its own process's cached settings and broadcasts
``Org.SETTINGS_CHANGED`` so every other process drops its copy too
(CAURA-571). That broadcast crosses processes only on a shared bus. On the
in-process bus the self-hosted stack runs, it reaches the process that
published it and no other, and core-api runs ``WEB_CONCURRENCY=2`` workers: a
settings write was enforced at once by one worker and up to the 5-minute TTL
later by the other. ``interviewer.enabled`` is a consent gate and
``governance.*`` holds compliance gates, so a GET answered by the fresh worker
read as proof of an enforcement the stale one did not apply.

Redis is already part of that stack, so it carries the invalidation instead:
the writer PUBLISHes the org id and every process runs a listener that evicts
it. Only on the in-process bus, since a shared bus already delivers the
broadcast, and only with ``REDIS_URL`` set. Every failure degrades to what
happened before: the TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable

from common.events.factory import get_event_bus
from common.events.inprocess import InProcessEventBus
from core_api.cache import cache_publish, cache_pubsub
from core_api.config import settings

logger = logging.getLogger(__name__)

CHANNEL = "caura:org-settings-invalidated"

# How long one read waits for a message. Bounded rather than blocking: how a
# blocking read treats the socket timeout differs across redis-py releases, and
# a bounded one returns None on silence under all of them.
_POLL_SECONDS = 1.0
# How long to wait before subscribing again after Redis was unreachable or the
# subscription broke.
_RETRY_SECONDS = 5.0


def relay_enabled() -> bool:
    """True when an invalidation reaches sibling processes only through Redis."""
    return bool(settings.redis_url) and isinstance(get_event_bus(), InProcessEventBus)


async def publish_invalidation(org_id: str) -> None:
    """Tell every core-api process to drop ``org_id``'s cached settings. Never raises."""
    if not relay_enabled():
        return
    if not await cache_publish(CHANNEL, org_id):
        logger.warning(
            "failed to relay the settings invalidation for %s; sibling workers "
            "will pick up the change within the cache TTL",
            org_id,
        )


async def listen_for_invalidations(
    invalidate: Callable[[str], None],
    clear: Callable[[], None],
) -> None:
    """Evict each org id published on ``CHANNEL``, until cancelled.

    A message published while this process is not subscribed is lost, so each
    subscription starts by clearing the whole cache: whatever was cached while
    the listener could not hear may be stale, and reloading it costs one
    storage read per tenant.
    """
    while True:
        pubsub = await cache_pubsub()
        if pubsub is None:
            await asyncio.sleep(_RETRY_SECONDS)
            continue
        try:
            await pubsub.subscribe(CHANNEL)
            clear()
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=_POLL_SECONDS)
                if message is not None and message.get("type") == "message":
                    invalidate(message["data"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "settings invalidation listener lost its subscription; subscribing again in %ss",
                _RETRY_SECONDS,
                exc_info=True,
            )
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()
        await asyncio.sleep(_RETRY_SECONDS)


def start_listener(
    invalidate: Callable[[str], None],
    clear: Callable[[], None],
) -> asyncio.Task | None:
    """Start the listener where the relay applies, for ``stop_listener`` at shutdown.

    Its own task rather than a tracked one: it never finishes, so the shutdown
    grace ``cancel_all_tasks`` grants tracked tasks would be spent waiting it out.
    """
    if not relay_enabled():
        return None
    return asyncio.create_task(
        listen_for_invalidations(invalidate, clear), name="settings-invalidation-listener"
    )


async def stop_listener(task: asyncio.Task | None) -> None:
    """Cancel the listener ``start_listener`` started and wait for it to close."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
