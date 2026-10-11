"""Synchronous Caura client.

A thin wrapper over the Caura REST API. Point it at a managed
(``https://caura.ai``) or self-hosted (``http://localhost:8000``) deployment.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from typing import Any

import httpx

from ._version import __version__
from .exceptions import AuthError, CauraAPIError, NotFoundError, RateLimitError, TransportError
from .models import Memory, RecallResult, SearchResult

DEFAULT_BASE_URL = "https://caura.ai"

USER_AGENT = (
    f"caura-client-python/{__version__} (python/{sys.version_info.major}.{sys.version_info.minor})"
)
"""Sent on every request so a server can tell SDK families apart.

It names the package, its version and the Python major.minor, nothing more;
no other identifying information is added and the client never contacts
anything but ``base_url``.
"""


_LOOPBACK_V4 = re.compile(r"127\.\d{1,3}\.\d{1,3}\.\d{1,3}")


def _is_loopback(host: str) -> bool:
    """The plugin's ``isLoopbackHost``: traffic to these never leaves the machine."""
    host = host.lower()
    return host in ("localhost", "::1") or host.endswith(".localhost") or bool(_LOOPBACK_V4.fullmatch(host))


def _check_key_transport(base_url: str, allow_insecure_http: bool | None) -> None:
    """Refuse to send the API key in cleartext to another machine (L-66).

    https, or plain http to a loopback host, or an explicit opt-in: the same rule
    the OpenClaw plugin applies. ``None`` defers to ``CAURA_ALLOW_INSECURE_HTTP``.
    """
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(
            f"base_url must start with https:// (or http:// for a loopback host); got scheme {parts.scheme!r}"
        )
    if parts.scheme == "https" or _is_loopback(parts.hostname or ""):
        return
    if allow_insecure_http is None:
        allow_insecure_http = os.environ.get("CAURA_ALLOW_INSECURE_HTTP") in ("true", "1")
    if not allow_insecure_http:
        host = parts.netloc.rpartition("@")[2]  # never echo userinfo
        raise ValueError(
            f"Refusing to send the API key to {host}: base_url uses plain HTTP to a "
            "non-loopback host, so the key would cross the network in cleartext. Use https://, or "
            "pass allow_insecure_http=True (or set CAURA_ALLOW_INSECURE_HTTP=true) to accept the "
            "risk, e.g. on a trusted private network."
        )


class Caura:
    """Client for a Caura deployment.

    Example::

        from caura_client import Caura

        mc = Caura("mc_xxx", tenant_id="my-team", agent_id="my-agent")
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
        allow_insecure_http: bool | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not tenant_id:
            raise ValueError("tenant_id is required")
        _check_key_transport(base_url, allow_insecure_http)
        self.tenant_id = tenant_id
        self.agent_id = agent_id
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
        caller_agent_id: str | None = None,
        **extra: Any,
    ) -> SearchResult:
        """Hybrid vector + keyword search (POST /api/v1/search).

        Returns the ranked ``Memory`` objects as a ``SearchResult``: a list that
        also carries the response's ``recall_tracked``, ``diagnostic``,
        ``warnings`` and ``raw`` body.

        ``caller_agent_id`` runs the search as that agent without filtering to
        its own memories, so a tenant-scoped key can read the agent's
        ``scope_agent`` memories. The server then reads as that agent: it
        registers the agent if new, and holds the read to the agent's fleet
        and trust level. It is not sent unless given; an agent-scoped key may
        only name its own agent.
        """
        body: dict[str, Any] = {"tenant_id": self.tenant_id, "query": query, "top_k": top_k}
        if fleet_ids:
            body["fleet_ids"] = fleet_ids
        if filter_agent_id:
            body["filter_agent_id"] = filter_agent_id
        if caller_agent_id:
            body["caller_agent_id"] = caller_agent_id
        body.update(extra)
        data = self._post("/api/v1/search", body)
        if not isinstance(data, dict):
            raise CauraAPIError(200, "search response must be a JSON object")
        if "items" not in data:
            raise CauraAPIError(200, 'search response missing "items" list')
        items = data["items"]
        if not isinstance(items, list):
            raise CauraAPIError(200, 'search response "items" must be a list')
        return SearchResult.from_dict(data)

    def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        caller_agent_id: str | None = None,
        **extra: Any,
    ) -> RecallResult:
        """Search + LLM summary. Returns a ``RecallResult`` context brief (POST /api/v1/recall).

        Asks for the result list once (``items_alias=False``): the server would
        otherwise repeat it under ``items``, about half the response, and this
        client reads ``memories``. Pass ``items_alias=True`` to keep the copy in
        ``raw``. ``caller_agent_id`` is as for ``search``.
        """
        body: dict[str, Any] = {
            "tenant_id": self.tenant_id,
            "query": query,
            "top_k": top_k,
            "items_alias": False,
        }
        if caller_agent_id:
            body["caller_agent_id"] = caller_agent_id
        body.update(extra)
        data = self._post("/api/v1/recall", body)
        if not isinstance(data, dict):
            raise CauraAPIError(200, "recall response must be a JSON object")
        return RecallResult.from_dict(data)

    def health(self) -> dict[str, Any]:
        """Liveness probe (GET /api/v1/health)."""
        response = self._request("GET", "/api/v1/health")
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

        By default the server stores the window, advances the watermark and
        answers 200 ``"status": "accepted"`` with ``memories_written`` 0: it
        writes the memories in the background, after the response. A server
        with ``interview_async_submit`` off instead interviews the window
        in-line (its budget is 90s) and answers 200 ``committed`` or 207
        ``partial`` with the count, which is why ``timeout`` defaults well
        above the client-wide 30s. Returns the response body plus
        ``"http_status"``. Raises on 4xx/5xx via the shared error mapping
        (403 → ``AuthError``: tenant not enabled / bad key; 409 → this window
        or stream is refused, as the message says).
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
    def _post(self, path: str, body: dict[str, Any]) -> Any:
        response = self._request("POST", path, json=body)
        self._raise_for_status(response)
        return response.json()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise TransportError(f"Request failed: {exc}") from exc

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

    def __enter__(self) -> Caura:  # noqa: PYI034 - Self is unavailable on supported Python 3.9.
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
