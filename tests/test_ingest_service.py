"""Tests for the URL-fetch hardening introduced in PR #1.

Covers:
- P1.1 Content-Type allowlist + post-redirect re-validation
- P1.1 Content-Length pre-check (413)
- P1.1 Streaming abort on decompressed bytes (gzip-bomb guard)
- P2.5 UTF-8 charset fallback (no ISO-8859-1 mojibake)
- P1.A-lite Hostname IP blacklist
"""

from __future__ import annotations

import socket
from unittest.mock import patch

import httpx
import pytest

from core_api.services.ingest_service import (
    MAX_INGEST_CONTENT_BYTES,
    _check_hostname_safe,
    _fetch_url_text,
    _is_blocked_ip,
)

# Capture the un-patched factories. The tests monkeypatch
# ``ingest_service.httpx.AsyncClient`` to substitute a MockTransport-backed
# client, so we need direct references to the originals to avoid recursion
# when our helper itself wants to build a mock client.
_real_AsyncClient = httpx.AsyncClient
_real_MockTransport = httpx.MockTransport


# ---------------------------------------------------------------------------
# P1.A-lite — hostname IP blacklist
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBlockedIPClassification:
    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",  # loopback
            "10.0.0.5",  # RFC1918 private
            "172.16.0.1",  # RFC1918 private
            "192.168.1.1",  # RFC1918 private
            "169.254.169.254",  # AWS/GCP/Azure metadata (link-local)
            "::1",  # IPv6 loopback
            "fc00::1",  # IPv6 unique-local
            "fe80::1",  # IPv6 link-local
            "0.0.0.0",  # unspecified
        ],
    )
    def test_blocked_ranges_are_rejected(self, addr: str) -> None:
        assert _is_blocked_ip(addr) is True

    @pytest.mark.parametrize(
        "addr",
        [
            "1.1.1.1",  # Cloudflare DNS
            "8.8.8.8",  # Google DNS
            "93.184.216.34",  # example.com
            "2606:4700:4700::1111",  # public IPv6
        ],
    )
    def test_public_addresses_pass(self, addr: str) -> None:
        assert _is_blocked_ip(addr) is False

    def test_invalid_string_returns_false(self) -> None:
        # Defensive: non-IP strings shouldn't raise, just say "not blocked"
        assert _is_blocked_ip("not-an-ip") is False


@pytest.mark.unit
class TestHostnameSafetyCheck:
    def test_rejects_localhost_url(self) -> None:
        with (
            pytest.raises(httpx.HTTPError) if False else pytest.raises(Exception) as exc
        ):
            _check_hostname_safe("http://127.0.0.1:8000/health")
        # The exception is a starlette HTTPException — check the status_code attr
        assert exc.value.status_code == 400
        assert "127.0.0.1" in exc.value.detail

    def test_rejects_rfc1918_hostname(self) -> None:
        # Use the explicit IP as the hostname — getaddrinfo will return it back
        with pytest.raises(Exception) as exc:
            _check_hostname_safe("http://10.0.0.5/")
        assert exc.value.status_code == 400

    def test_rejects_metadata_ip(self) -> None:
        with pytest.raises(Exception) as exc:
            _check_hostname_safe("http://169.254.169.254/latest/meta-data/")
        assert exc.value.status_code == 400

    def test_rejects_invalid_url(self) -> None:
        with pytest.raises(Exception) as exc:
            _check_hostname_safe("not-a-valid-url")
        assert exc.value.status_code == 400
        assert "no hostname" in exc.value.detail.lower()

    def test_public_hostname_passes(self) -> None:
        # Mock getaddrinfo to avoid hitting real DNS in unit tests
        with patch(
            "core_api.services.ingest_service.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.1.1.1", 0))],
        ):
            # Should not raise
            _check_hostname_safe("https://example.com/page")


# ---------------------------------------------------------------------------
# P1.1 — Content-Type allowlist + size guard + streaming
# P2.5 — UTF-8 encoding fallback
# ---------------------------------------------------------------------------


def _make_client(handler) -> httpx.AsyncClient:
    """Helper: build an httpx client with a MockTransport for in-process testing.

    Uses the captured-at-import-time httpx references so it keeps working
    even after a test monkeypatches ``ingest_service.httpx.AsyncClient``.
    """
    return _real_AsyncClient(
        transport=_real_MockTransport(handler),
        follow_redirects=True,
        timeout=30.0,
    )


@pytest.mark.unit
@pytest.mark.asyncio
class TestFetchUrlText:
    async def test_text_html_succeeds(self, monkeypatch) -> None:
        """text/html with UTF-8 body returns extracted text."""
        # Skip the SSRF check by patching it (the URL we pass is public-ish)
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html><body><p>Hello world.</p></body></html>",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/")
        assert "Hello world" in result

    async def test_application_pdf_routed_through_kreuzberg(self, monkeypatch) -> None:
        """PR #8: PDF MIME no longer auto-rejected — Kreuzberg extracts text.

        Patches ``kreuzberg.extract_bytes`` to return a known string and
        asserts that ``_fetch_url_text`` returns it (i.e. binary types
        flow through the new path).
        """
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        async def fake_extract(data, mime, *_a, **_kw):
            assert mime == "application/pdf"
            assert data == b"%PDF-1.4\n%pdf bytes"

            class _R:
                content = "Hello extracted PDF body"
                metadata = {"is_encrypted": False}

            return _R()

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.4\n%pdf bytes",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/file.pdf")
        assert result == "Hello extracted PDF body"

    async def test_docx_routed_through_kreuzberg(self, monkeypatch) -> None:
        """Office DOCX MIME also goes through Kreuzberg."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        async def fake_extract(data, mime, *_a, **_kw):
            assert mime == (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            )

            class _R:
                content = "Title: Quarterly Review\nBody paragraphs go here."
                metadata = {}

            return _R()

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "content-type": (
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                    )
                },
                content=b"PK\x03\x04...",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/report.docx")
        assert "Quarterly Review" in result

    async def test_encrypted_pdf_returns_422(self, monkeypatch) -> None:
        """Encrypted PDF surfaces as 422 with a clean error message."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )
        import kreuzberg as _kz

        async def fake_extract(data, mime, *_a, **_kw):
            raise _kz.ParsingError("PDF encrypted: password required")

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.4\n%encrypted",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        with pytest.raises(Exception) as exc:
            await _fetch_url_text("https://example.com/secret.pdf")
        assert exc.value.status_code == 422
        assert "Encrypted PDF" in exc.value.detail

    async def test_malformed_pdf_parsing_error_422(self, monkeypatch) -> None:
        """Garbage PDF bytes → Kreuzberg raises ParsingError → 422."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )
        import kreuzberg as _kz

        async def fake_extract(data, mime, *_a, **_kw):
            raise _kz.ParsingError("Invalid PDF: PdfiumLibraryInternalError")

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"not a real pdf",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        with pytest.raises(Exception) as exc:
            await _fetch_url_text("https://example.com/broken.pdf")
        assert exc.value.status_code == 422

    async def test_empty_extracted_content_422(self, monkeypatch) -> None:
        """Image-only PDF (no OCR backend) → empty extraction → 422."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        async def fake_extract(data, mime, *_a, **_kw):
            class _R:
                content = "   "  # whitespace-only, no real text
                metadata = {"is_encrypted": False}

            return _R()

        monkeypatch.setattr(
            "core_api.services.ingest_service.kreuzberg.extract_bytes", fake_extract
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"%PDF-1.4\n%image-only",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        with pytest.raises(Exception) as exc:
            await _fetch_url_text("https://example.com/scanned.pdf")
        assert exc.value.status_code == 422
        assert "no text content" in exc.value.detail.lower()

    async def test_octet_stream_rejected_422(self, monkeypatch) -> None:
        """Unknown binary MIME → 422."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/octet-stream"},
                content=b"\x00" * 100,
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        with pytest.raises(Exception) as exc:
            await _fetch_url_text("https://example.com/data")
        assert exc.value.status_code == 422

    async def test_content_length_precheck_413(self, monkeypatch) -> None:
        """Honest Content-Length header > cap → 413 before downloading."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )
        oversized = str(MAX_INGEST_CONTENT_BYTES + 1)

        def handler(request: httpx.Request) -> httpx.Response:
            # Headers claim it's large; we should reject without reading the body
            return httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": oversized},
                content=b"x" * 10,  # body itself is small; pre-check should fire first
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        with pytest.raises(Exception) as exc:
            await _fetch_url_text("https://example.com/")
        assert exc.value.status_code == 413
        assert oversized in exc.value.detail

    async def test_streaming_abort_on_oversize_body(self, monkeypatch) -> None:
        """Body exceeds cap mid-stream → 413 (gzip-bomb guard)."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )
        oversize_body = b"x" * (MAX_INGEST_CONTENT_BYTES + 5_000)

        def handler(request: httpx.Request) -> httpx.Response:
            # No content-length header; force streaming path
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=oversize_body
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        with pytest.raises(Exception) as exc:
            await _fetch_url_text("https://example.com/")
        assert exc.value.status_code == 413

    async def test_under_cap_succeeds(self, monkeypatch) -> None:
        """Body comfortably under cap → success."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/plain"},
                content=b"short content here",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/")
        assert "short content here" in result

    async def test_utf8_body_with_no_charset_header(self, monkeypatch) -> None:
        """P2.5: UTF-8 body without a charset declaration should NOT mojibake."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )
        # Japanese characters in UTF-8, no charset in Content-Type
        body = "<html><body>こんにちは世界</body></html>".encode("utf-8")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=body
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/")
        assert "こんにちは世界" in result

    async def test_markdown_mime_allowed(self, monkeypatch) -> None:
        """text/markdown is explicitly in the allowlist."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._check_hostname_safe", lambda url: None
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/markdown"},
                content=b"# Title\n\nBody.",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/doc.md")
        assert "Title" in result
        assert "Body" in result
