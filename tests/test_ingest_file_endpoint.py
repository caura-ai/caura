"""Tests for PR #9 — POST /ingest/file and the 3 MB body-size dependency.

Covers:
- ``_enforce_ingest_body_size`` rejects Content-Length > cap with 413
- Under-cap requests pass through
- ``/ingest/file`` dispatches text MIMEs through ``decode_text_body``
- ``/ingest/file`` dispatches binary MIMEs through Kreuzberg
- Unsupported MIME → 422
- Oversized multipart payload caught at the post-read check
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core_api.auth import AuthContext, get_auth_context
from core_api.db.session import get_db
from core_api.middleware.ingest_body_size import IngestBodySizeMiddleware
from core_api.routes.memories import router
from core_api.services.ingest_service import INGEST_MAX_INPUT_BYTES

pytestmark = pytest.mark.unit


def _build_app(monkeypatch, fake_preview_response: dict | None = None) -> TestClient:
    """Tiny test app wired with dep overrides + a stubbed ``ingest_preview``.

    The real preview path needs Postgres, OpenAI, etc. We replace it with an
    AsyncMock so the endpoint's job here is only: parse body, dispatch by
    MIME, hand the extracted text to preview, return whatever preview said.
    """
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    # Install the same body-size middleware production wires up in app.py
    # so tests exercise the real cap behavior, not a route-level fake.
    app.add_middleware(IngestBodySizeMiddleware)

    # Override auth → always returns an admin context (bypasses tenant checks)
    app.dependency_overrides[get_auth_context] = lambda: AuthContext(
        tenant_id=None, is_admin=True
    )
    # Override DB → returns a sentinel; the stubbed ingest_preview ignores it
    app.dependency_overrides[get_db] = lambda: object()

    preview_mock = AsyncMock(
        return_value=fake_preview_response or {"facts": [], "sections": 0}
    )
    monkeypatch.setattr("core_api.routes.memories.ingest_preview", preview_mock)
    return TestClient(app), preview_mock


# ---------------------------------------------------------------------------
# _enforce_ingest_body_size
# ---------------------------------------------------------------------------


class TestBodySizeMiddleware:
    def test_oversize_body_returns_413(self, monkeypatch) -> None:
        """Oversized request body → 413 from the middleware BEFORE FastAPI
        parses anything. httpx sets Content-Length from real bytes, so we
        have to allocate them."""
        client, _ = _build_app(monkeypatch)
        oversize = b"x" * (INGEST_MAX_INPUT_BYTES + 1)
        resp = client.post(
            "/api/v1/ingest/preview",
            content=oversize,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 413
        assert "3 MB" in resp.json()["detail"]

    def test_under_cap_passes_middleware(self, monkeypatch) -> None:
        """A small valid JSON body must not 413."""
        client, _ = _build_app(monkeypatch)
        resp = client.post(
            "/api/v1/ingest/preview",
            json={"tenant_id": "t", "content": "hello world"},
        )
        assert resp.status_code != 413

    def test_middleware_skips_non_ingest_paths(self, monkeypatch) -> None:
        """A different path with an oversize body must not be blocked by
        the ingest-scoped middleware."""
        from fastapi import APIRouter

        client, _ = _build_app(monkeypatch)
        # Add a throwaway route on the test app's underlying router so we
        # can verify the middleware doesn't catch unrelated paths.
        other = APIRouter()

        @other.post("/echo")
        async def _echo() -> dict:
            return {"ok": True}

        client.app.include_router(other, prefix="/api/v1")
        oversize = b"x" * (INGEST_MAX_INPUT_BYTES + 1)
        resp = client.post(
            "/api/v1/echo",
            content=oversize,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code != 413  # middleware did NOT fire


# ---------------------------------------------------------------------------
# /ingest/file dispatch
# ---------------------------------------------------------------------------


class TestIngestFileEndpoint:
    def test_uploads_text_markdown(self, monkeypatch) -> None:
        client, preview_mock = _build_app(
            monkeypatch,
            fake_preview_response={"facts": [{"content": "x"}], "sections": 1},
        )
        md_bytes = b"# Title\n\nBody."
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("doc.md", md_bytes, "text/markdown")},
            data={"tenant_id": "t1"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["sections"] == 1
        # Verify the text was passed through unchanged (line endings intact)
        called_req = preview_mock.call_args.args[1]
        assert "# Title" in called_req.content
        assert "\n\n" in called_req.content
        # Filename survives as upload:<name> on source_uri so each derived
        # memory carries its origin (not the generic "text-input" marker).
        assert called_req.source_uri == "upload:doc.md"

    def test_upload_with_whitespace_only_filename_falls_back(self, monkeypatch) -> None:
        """When the client sends a whitespace-only filename, source_uri
        should fall back to the bare ``"upload"`` marker — not
        ``upload:    `` (which would render as just the prefix in the UI)."""
        client, preview_mock = _build_app(monkeypatch)
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("   ", b"some plain text content here", "text/plain")},
            data={"tenant_id": "t1"},
        )
        assert resp.status_code == 200, resp.text
        called_req = preview_mock.call_args.args[1]
        assert called_req.source_uri == "upload"

    def test_uploads_csv_preserves_rows(self, monkeypatch) -> None:
        client, preview_mock = _build_app(monkeypatch)
        csv_bytes = b"a,b\n1,2\n3,4\n"
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("data.csv", csv_bytes, "text/csv")},
            data={"tenant_id": "t1"},
        )
        assert resp.status_code == 200, resp.text
        called_req = preview_mock.call_args.args[1]
        # Row breaks must survive — otherwise the LLM sees one mashed line.
        assert "\n" in called_req.content
        assert called_req.content == "a,b\n1,2\n3,4"

    def test_pdf_upload_sets_upload_filename_source_uri(self, monkeypatch) -> None:
        """Filename of a binary upload also survives via Kreuzberg dispatch."""

        async def fake_extract(data, mime, *_a, **_kw):
            class _R:
                content = "Extracted body."
                metadata = {"is_encrypted": False}

            return _R()

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )
        client, preview_mock = _build_app(monkeypatch)
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("report.pdf", b"%PDF-1.4\nbytes", "application/pdf")},
            data={"tenant_id": "t1"},
        )
        assert resp.status_code == 200, resp.text
        called_req = preview_mock.call_args.args[1]
        assert called_req.source_uri == "upload:report.pdf"

    def test_uploads_pdf_routes_through_kreuzberg(self, monkeypatch) -> None:
        """PDFs (and other binary MIMEs) hand off to Kreuzberg."""

        async def fake_extract(data, mime, *_a, **_kw):
            assert mime == "application/pdf"

            class _R:
                content = "# Extracted Heading\n\nBody text."
                metadata = {"is_encrypted": False}

            return _R()

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )

        client, preview_mock = _build_app(monkeypatch)
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("doc.pdf", b"%PDF-1.4\n%bytes", "application/pdf")},
            data={"tenant_id": "t1"},
        )
        assert resp.status_code == 200, resp.text
        called_req = preview_mock.call_args.args[1]
        assert "Extracted Heading" in called_req.content

    def test_unsupported_mime_returns_422(self, monkeypatch) -> None:
        client, _ = _build_app(monkeypatch)
        resp = client.post(
            "/api/v1/ingest/file",
            files={
                "file": ("blob.bin", b"\x00" * 16, "application/octet-stream")
            },
            data={"tenant_id": "t1"},
        )
        assert resp.status_code == 422
        assert "Unsupported content type" in resp.json()["detail"]

    def test_oversized_file_caught_post_read(self, monkeypatch) -> None:
        """Defensive check: even if the multipart envelope hides the real
        payload size from Content-Length, the post-read assertion fires."""
        client, _ = _build_app(monkeypatch)
        big = b"x" * (INGEST_MAX_INPUT_BYTES + 1)
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("big.txt", big, "text/plain")},
            data={"tenant_id": "t1"},
        )
        # Either the Content-Length dep (413) or the post-read check (413)
        # catches it — both are correct.
        assert resp.status_code == 413
        assert "3 MB" in resp.json()["detail"]

    def test_forwards_focus_field(self, monkeypatch) -> None:
        client, preview_mock = _build_app(monkeypatch)
        resp = client.post(
            "/api/v1/ingest/file",
            files={"file": ("doc.md", b"# Hi", "text/markdown")},
            data={"tenant_id": "t1", "focus": "release notes"},
        )
        assert resp.status_code == 200, resp.text
        called_req = preview_mock.call_args.args[1]
        assert called_req.focus == "release notes"


# ---------------------------------------------------------------------------
# Routes are registered on the router
# ---------------------------------------------------------------------------


def test_ingest_file_route_registered() -> None:
    paths = [getattr(r, "path", None) for r in router.routes]
    assert "/ingest/file" in paths
    assert "/ingest/preview" in paths
    assert "/ingest/commit" in paths
