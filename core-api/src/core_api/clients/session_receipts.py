"""Bounded, service-authenticated reads of stored session receipt assertions."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx

from core_api.clients.identity_token import fetch_auth_header

MAX_CHECKS = 100
_MAX_RESPONSE_BYTES = 65536
_MAX_INFLIGHT = 8


class ReceiptLookupUnavailable(Exception):
    """The dependency supplied no usable evidence."""


def validate_results(body: object, checks: list[dict]) -> list[dict]:
    """Require one ordered, event-bound answer per check, without foreign data."""
    if not isinstance(body, dict) or set(body) != {"results"}:
        raise ReceiptLookupUnavailable
    results = body["results"]
    if not isinstance(results, list) or len(results) != len(checks):
        raise ReceiptLookupUnavailable
    for result, check in zip(results, checks, strict=True):
        if not isinstance(result, dict) or set(result) - {"event_id", "status", "reason", "delivered_at"}:
            raise ReceiptLookupUnavailable
        try:
            if str(UUID(result["event_id"])) != check["event_id"]:
                raise ValueError
            status = result["status"]
            if status not in {"matched", "not_found", "mismatch", "unverifiable"}:
                raise ValueError
            reason = result.get("reason")
            if reason is not None and (
                status != "unverifiable" or reason not in {"missing_binding", "no_rule_set_hash"}
            ):
                raise ValueError
            delivered = result.get("delivered_at")
            if status == "matched":
                if not isinstance(delivered, str):
                    raise ValueError
                timestamp = datetime.fromisoformat(delivered)
                if timestamp.utcoffset() is None:
                    raise ValueError
            elif delivered is not None:
                raise ValueError
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ReceiptLookupUnavailable from None
    return results


class SessionReceiptMatchClient:
    def __init__(
        self,
        *,
        base_url: str,
        audience: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 0.25,
    ):
        self.base_url = base_url.rstrip("/")
        self.audience = audience
        self.http_client = http_client
        self.timeout_seconds = timeout_seconds
        self._inflight = 0
        self._identity_task: asyncio.Task[dict[str, str]] | None = None

    async def _headers(self) -> dict[str, str]:
        # A caller deadline must not cancel the provider's lock while its
        # bounded synchronous metadata fetch is still running in a thread.
        # Share at most one refresh, including across timed-out callers.
        if self._identity_task is None or self._identity_task.done():
            self._identity_task = asyncio.create_task(fetch_auth_header(self.audience))
            self._identity_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        headers = await asyncio.shield(self._identity_task)
        authorization = headers.get("Authorization", "")
        if not authorization.startswith("Bearer ") or not authorization[7:].strip():
            raise ReceiptLookupUnavailable
        return {"Authorization": authorization}

    async def _request(
        self, http: httpx.AsyncClient, url: str, headers: dict, checks: list[dict]
    ) -> list[dict]:
        async with http.stream(
            "POST",
            url,
            json={"checks": checks},
            headers=headers,
            timeout=self.timeout_seconds,
            follow_redirects=False,
        ) as response:
            if response.status_code != 200:
                raise ReceiptLookupUnavailable
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                    raise ReceiptLookupUnavailable
                body.extend(chunk)
            return validate_results(json.loads(body), checks)

    async def match(self, tenant_id: str, checks: list[dict]) -> list[dict]:
        if not 1 <= len(checks) <= MAX_CHECKS:
            raise ValueError("Receipt checks must contain 1 to 100 items")
        parsed = urlsplit(self.base_url)
        if (
            not self.audience
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or not tenant_id
            or self._inflight >= _MAX_INFLIGHT
        ):
            raise ReceiptLookupUnavailable
        self._inflight += 1
        try:
            async with asyncio.timeout(self.timeout_seconds):
                headers = await self._headers()
                url = f"{self.base_url}/api/v1/storage/tenants/{quote(tenant_id, safe='')}/session-receipts/match"
                if self.http_client is not None:
                    return await self._request(self.http_client, url, headers, checks)
                async with httpx.AsyncClient(trust_env=False) as http:
                    return await self._request(http, url, headers, checks)
        except Exception:
            # Do not include credential/provider, response or exception data.
            raise ReceiptLookupUnavailable from None
        finally:
            self._inflight -= 1
