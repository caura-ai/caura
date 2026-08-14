"""Synchronous MemClaw client.

A thin wrapper over the MemClaw REST API. Point it at a managed
(``https://caura.ai``) or self-hosted (``http://localhost:8000``) deployment.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

from .exceptions import AuthError, MemClawAPIError, NotFoundError
from .models import Memory, RecallResult

DEFAULT_BASE_URL = "https://caura.ai"


class MemClaw:
    """Client for a MemClaw deployment.

    Example::

        from memclaw_client import MemClaw

        mc = MemClaw("mc_xxx", tenant_id="my-team", agent_id="my-agent")
        mc.write("Q3 revenue target is $4M, set on 2026-04-15.")
        for m in mc.search("Q3 revenue target"):
            print(m.title, m.content)
    """

    def __init__(
        self,
        api_key: str,
        *,
        tenant_id: str,
        base_url: str = DEFAULT_BASE_URL,
        agent_id: str | None = None,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-API-Key": api_key, "Content-Type": "application/json"},
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
        data = self._post("/api/v1/search", body)
        if not isinstance(data, dict):
            raise MemClawAPIError(200, "search response must be a JSON object")
        if "items" not in data:
            raise MemClawAPIError(200, 'search response missing "items" list')
        items = data["items"]
        if not isinstance(items, list):
            raise MemClawAPIError(200, 'search response "items" must be a list')
        return [Memory.from_dict(m) for m in items]

    def recall(self, query: str, *, top_k: int = 5, **extra: Any) -> RecallResult:
        """Search + LLM summary. Returns a ``RecallResult`` context brief (POST /api/v1/recall)."""
        body: dict[str, Any] = {"tenant_id": self.tenant_id, "query": query, "top_k": top_k}
        body.update(extra)
        return RecallResult.from_dict(self._post("/api/v1/recall", body))

    def health(self) -> dict[str, Any]:
        """Liveness probe (GET /api/v1/health)."""
        response = self._http.get("/api/v1/health")
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
        response = self._http.get(
            f"/api/v1/documents/{encoded}",
            params={"tenant_id": tenant_id or self.tenant_id, "collection": collection},
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
        response = self._http.post("/api/v1/interview/submit", json=body, timeout=timeout)
        self._raise_for_status(response)
        result = response.json()
        if isinstance(result, dict):
            result["http_status"] = response.status_code
        return result

    # ------------------------------------------------------------- internals
    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._http.post(path, json=body)
        self._raise_for_status(response)
        return response.json()

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
        raise MemClawAPIError(response.status_code, message or "request failed", details=details)

    # ------------------------------------------------------------- lifecycle
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> MemClaw:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
