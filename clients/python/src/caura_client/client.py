"""Synchronous Caura client.

A thin wrapper over the Caura REST API. Point it at a managed
(``https://caura.ai``) or self-hosted (``http://localhost:8000``) deployment.
"""

from __future__ import annotations

import sys
import time
import urllib.parse
from typing import Any

import httpx

from ._version import __version__
from .exceptions import AuthError, CauraAPIError, NotFoundError, RateLimitError, TransportError
from .models import Memory, RecallResult

DEFAULT_BASE_URL = "https://caura.ai"

_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

USER_AGENT = f"caura-client-python/{__version__} (python/{sys.version_info.major}.{sys.version_info.minor})"
"""Sent on every request so a server can tell SDK families apart.

It names the package, its version and the Python major.minor, nothing more;
no other identifying information is added and the client never contacts
anything but ``base_url``.
"""


class Caura:
    """Client for a Caura deployment.

    Example::

        from caura_client import Caura

        mc = Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent")
        mc.write("Q3 revenue target is $4M, set on 2026-04-15.")
        for m in mc.search("Q3 revenue target"):
            print(m.title, m.content)

    Pass ``retries`` to retry transient failures (transport errors and
    429/502/503/504) on read calls (``search``, ``recall``, ``health``,
    ``get_document``) with exponential backoff, honoring ``Retry-After``
    when present. Off by default (``retries=0``). ``write`` and
    ``submit_interview`` are never retried, to avoid duplicating a write
    whose result is unknown.
    """

    def __init__(
        self,
        api_key: str,
        *,
        tenant_id: str,
        base_url: str = DEFAULT_BASE_URL,
        agent_id: str | None = None,
        timeout: float = 30.0,
        retries: int = 0,
        retry_backoff: float = 0.5,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be >= 0")
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self._retries = retries
        self._retry_backoff = retry_backoff
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "X-API-Key": api_key,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
        )

    # ------------------------------------------------------------------ ops
    def write(
        self,
        content: str,
        *,
        agent_id: str | None = None,
        memory_type: str | None = None,
        fleet_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        **extra: Any,
    ) -> Memory:
        """Persist a memory. Returns the enriched ``Memory`` (POST /api/v1/memories)."""
        body: dict[str, Any] = {"tenant_id": self.tenant_id, "content": content}
        resolved_agent = agent_id or self.agent_id
        if resolved_agent:
            body["agent_id"] = resolved_agent
        if memory_type:
            body["memory_type"] = memory_type
        if fleet_id:
            body["fleet_id"] = fleet_id
        if metadata is not None:
            body["metadata"] = metadata
        body.update(extra)
        return Memory.from_dict(self._post("/api/v1/memories", body))

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        fleet_ids: list[str] | None = None,
        filter_agent_id: str | None = None,
        **extra: Any,
    ) -> list[Memory]:
        """Hybrid vector + keyword search. Returns ranked ``Memory`` objects (POST /api/v1/search)."""
        body: dict[str, Any] = {"tenant_id": self.tenant_id, "query": query, "top_k": top_k}
        if fleet_ids:
            body["fleet_ids"] = fleet_ids
        if filter_agent_id:
            body["filter_agent_id"] = filter_agent_id
        body.update(extra)
        data = self._post("/api/v1/search", body, retryable=True)
        if not isinstance(data, dict):
            raise CauraAPIError(200, "search response must be a JSON object")
        if "items" not in data:
            raise CauraAPIError(200, 'search response missing "items" list')
        items = data["items"]
        if not isinstance(items, list):
            raise CauraAPIError(200, 'search response "items" must be a list')
        return [Memory.from_dict(m) for m in items]

    def recall(self, query: str, *, top_k: int = 5, **extra: Any) -> RecallResult:
        """Search + LLM summary. Returns a ``RecallResult`` context brief (POST /api/v1/recall)."""
        body: dict[str, Any] = {"tenant_id": self.tenant_id, "query": query, "top_k": top_k}
        body.update(extra)
        data = self._post("/api/v1/recall", body, retryable=True)
        if not isinstance(data, dict):
            raise CauraAPIError(200, "recall response must be a JSON object")
        return RecallResult.from_dict(data)

    def health(self) -> dict[str, Any]:
        """Liveness probe (GET /api/v1/health)."""
        response = self._request("GET", "/api/v1/health", retryable=True)
        self._raise_for_status(response)
        return response.json()

    def get_document(
        self,
        doc_id: str,
        *,
        collection: str,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch one structured document (GET /api/v1/documents/{doc_id}).

        Returns the full ``DocOut`` envelope — the stored record is under
        the ``"data"`` key. Raises ``NotFoundError`` if absent.
        """
        # Percent-encode the path segment: httpx does not encode f-string
        # paths, so a doc_id containing '/' would hit a different route and
        # '?' would inject query params.
        encoded = urllib.parse.quote(doc_id, safe="")
        response = self._request(
            "GET",
            f"/api/v1/documents/{encoded}",
            params={"tenant_id": tenant_id or self.tenant_id, "collection": collection},
            retryable=True,
        )
        self._raise_for_status(response)
        return response.json()

    def submit_interview(
        self,
        *,
        node_id: str,
        agent_id: str,
        cursor_from: int,
        cursor_to: int,
        events: list[dict[str, Any]],
        tenant_id: str | None = None,
        fleet_id: str | None = None,
        command_id: str | None = None,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """Submit one Interviewer window (POST /api/v1/interview/submit).

        The server interviews the window synchronously (its budget is 90s),
        so ``timeout`` defaults well above the client-wide 30s. Returns the
        response body plus ``"http_status"`` so callers can distinguish a
        207 partial from a 200 committed. Raises on 4xx/5xx via the shared
        error mapping (403 → ``AuthError``: tenant not enabled / bad key).
        """
        body: dict[str, Any] = {
            "tenant_id": tenant_id or self.tenant_id,
            "node_id": node_id,
            "agent_id": agent_id,
            "cursor_from": cursor_from,
            "cursor_to": cursor_to,
            "events": events,
        }
        if fleet_id:
            body["fleet_id"] = fleet_id
        if command_id:
            body["command_id"] = command_id
        response = self._request("POST", "/api/v1/interview/submit", json=body, timeout=timeout)
        self._raise_for_status(response)
        result = response.json()
        if isinstance(result, dict):
            result["http_status"] = response.status_code
        return result

    # ------------------------------------------------------------- internals
    def _post(self, path: str, body: dict[str, Any], *, retryable: bool = False) -> Any:
        response = self._request("POST", path, json=body, retryable=retryable)
        self._raise_for_status(response)
        return response.json()

    def _request(self, method: str, path: str, *, retryable: bool = False, **kwargs: Any) -> httpx.Response:
        attempts = self._retries + 1 if retryable else 1
        attempt = 0
        while True:
            try:
                response = self._http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt + 1 >= attempts:
                    raise TransportError(f"Request failed: {exc}") from exc
                delay = self._retry_delay(attempt, None)
            else:
                if attempt + 1 >= attempts or response.status_code not in _RETRYABLE_STATUS_CODES:
                    return response
                delay = self._retry_delay(attempt, self._parse_retry_after(response))
            time.sleep(delay)
            attempt += 1

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        """Seconds to wait before the next attempt: ``Retry-After`` if present, else exponential."""
        if retry_after is not None:
            return retry_after
        return self._retry_backoff * (2**attempt)

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        try:
            return float(response.headers["Retry-After"])
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {}
        message = ""
        details: Any = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message") or ""
                details = error.get("details")
            message = message or payload.get("detail") or payload.get("message") or response.text
        else:
            message = response.text
        if response.status_code in (401, 403):
            raise AuthError(response.status_code, message or "authentication failed", details=details)
        if response.status_code == 404:
            raise NotFoundError(response.status_code, message or "not found", details=details)
        if response.status_code == 429:
            try:
                retry_after = float(response.headers["Retry-After"])
            except (KeyError, ValueError):
                retry_after = None
            raise RateLimitError(
                response.status_code,
                message or "rate limit exceeded",
                details=details,
                retry_after=retry_after,
            )
        raise CauraAPIError(response.status_code, message or "request failed", details=details)

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Caura:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
