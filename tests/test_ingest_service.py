"""Tests for the URL-fetch hardening introduced in PR #1.

Covers:
- P1.1 Content-Type allowlist
- M-43 per-hop redirect validation (which replaced the post-redirect
  re-validation this docstring used to name — that check ran after the
  request it was supposed to prevent)
- P1.1 Content-Length pre-check (413)
- P1.1 Streaming abort on decompressed bytes (gzip-bomb guard)
- P2.5 UTF-8 charset fallback (no ISO-8859-1 mojibake)
- P1.A-lite Hostname IP blacklist
"""

from __future__ import annotations

import socket
import threading
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from core_api.services import ingest_service
from core_api.services.ingest_service import (
    INGEST_MAX_INPUT_BYTES,
    MAX_INGEST_CONTENT_BYTES,
    _fetch_url_text,
    _is_blocked_ip,
    _resolve_and_vet,
    decode_text_body,
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
            _resolve_and_vet("http://127.0.0.1:8000/health")
        # The exception is a starlette HTTPException — check the status_code attr
        assert exc.value.status_code == 400
        assert "127.0.0.1" in exc.value.detail

    def test_rejects_rfc1918_hostname(self) -> None:
        # Use the explicit IP as the hostname — getaddrinfo will return it back
        with pytest.raises(Exception) as exc:
            _resolve_and_vet("http://10.0.0.5/")
        assert exc.value.status_code == 400

    def test_rejects_metadata_ip(self) -> None:
        with pytest.raises(Exception) as exc:
            _resolve_and_vet("http://169.254.169.254/latest/meta-data/")
        assert exc.value.status_code == 400

    def test_rejects_invalid_url(self) -> None:
        with pytest.raises(Exception) as exc:
            _resolve_and_vet("not-a-valid-url")
        assert exc.value.status_code == 400
        assert "no hostname" in exc.value.detail.lower()

    def test_public_hostname_passes(self) -> None:
        # Mock getaddrinfo to avoid hitting real DNS in unit tests
        with patch(
            "core_api.services.ingest_service.socket.getaddrinfo",
            return_value=[(socket.AF_INET, 0, 0, "", ("1.1.1.1", 0))],
        ):
            # Should not raise
            _resolve_and_vet("https://example.com/page")


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
        # Answer the SSRF vetting with a fixed public address rather than
        # hitting live DNS. The check still runs, and pinning still happens.
        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        # Japanese characters in UTF-8, no charset in Content-Type
        body = "<html><body>こんにちは世界</body></html>".encode()

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
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
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

    async def test_csv_mime_allowed(self, monkeypatch) -> None:
        """PR #9: text/csv is in the allowlist and rows are preserved."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        csv_body = b"name,age,role\nAlice,30,founder\nBob,28,engineer\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/csv"}, content=csv_body
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/people.csv")
        # Newlines must be preserved or the LLM can't tell rows apart
        assert "\nAlice" in result
        assert "\nBob" in result
        assert "name,age,role" in result

    async def test_markdown_preserves_newlines(self, monkeypatch) -> None:
        """PR #9: markdown via URL no longer collapses whitespace, so the
        chunker can detect heading boundaries on URL-fetched docs."""
        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        md = b"# H1\n\nPara 1.\n\n## H2\n\nPara 2.\n"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/markdown"}, content=md
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _make_client(handler),
        )
        result = await _fetch_url_text("https://example.com/doc.md")
        assert "# H1" in result
        assert "## H2" in result
        assert "\n\n" in result  # blank line between sections preserved


# ---------------------------------------------------------------------------
# PR #9 — decode_text_body helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDecodeTextBody:
    def test_html_strips_scripts_and_collapses_whitespace(self) -> None:
        body = b"<html><body><script>evil()</script><p>Hello   world</p></body></html>"
        assert decode_text_body(body, "text/html") == "Hello world"

    def test_xhtml_strips_tags(self) -> None:
        body = b"<root><a>x</a> <b>y</b></root>"
        out = decode_text_body(body, "application/xhtml+xml")
        assert "x" in out and "y" in out
        assert "<" not in out

    def test_plain_preserves_newlines(self) -> None:
        body = b"line one\n\nline two\n"
        assert decode_text_body(body, "text/plain") == "line one\n\nline two"

    def test_markdown_preserves_headings(self) -> None:
        body = b"# Title\n\nBody.\n"
        out = decode_text_body(body, "text/markdown")
        assert "# Title" in out
        assert "\n\n" in out

    def test_csv_preserves_rows(self) -> None:
        body = b"a,b,c\n1,2,3\n4,5,6\n"
        out = decode_text_body(body, "text/csv")
        assert out == "a,b,c\n1,2,3\n4,5,6"

    def test_normalizes_crlf_to_lf(self) -> None:
        body = b"line1\r\nline2\r\n"
        assert decode_text_body(body, "text/plain") == "line1\nline2"

    def test_utf8_decode_with_fallback(self) -> None:
        body = "héllo wörld".encode()
        assert "héllo wörld" in decode_text_body(body, "text/plain")


# ---------------------------------------------------------------------------
# PR #9 — 3 MB unified cap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unified_cap_is_3mb() -> None:
    """The single ``INGEST_MAX_INPUT_BYTES`` knob governs every entry point."""
    assert INGEST_MAX_INPUT_BYTES == 3_000_000
    # MAX_INGEST_CONTENT_BYTES is kept as a back-compat alias.
    assert MAX_INGEST_CONTENT_BYTES == INGEST_MAX_INPUT_BYTES


@pytest.mark.unit
async def test_extract_with_kreuzberg_501_when_extra_absent(monkeypatch) -> None:
    """Slim build (no ``ingest`` extra): kreuzberg is None, so the extractor
    must fail clearly with 501 — not a NameError/500 — and point at the extra."""
    monkeypatch.setattr(ingest_service, "kreuzberg", None)
    with pytest.raises(HTTPException) as exc:
        await ingest_service._extract_with_kreuzberg(
            b"%PDF-1.4 fake", "application/pdf"
        )
    assert exc.value.status_code == 501
    assert "ingest" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# M-43 — the redirect chain is validated hop by hop, before each request
# ---------------------------------------------------------------------------


def _passthrough_client(handler, **kw) -> httpx.AsyncClient:
    """Mock client built with the REAL kwargs the code under test passed.

    ``_make_client`` hardcodes ``follow_redirects=True`` and every call site
    drops ``**kw``. That is harmless for handlers answering 200 immediately, and
    fatal here: it would put redirect-following back inside httpx — the exact
    behaviour M-43 removed — so the per-hop check would never run and these
    tests would report on a code path that does not exist any more.
    """
    return _real_AsyncClient(transport=_real_MockTransport(handler), **kw)


_BLOCKED_TEST_HOSTS = {"169.254.169.254", "10.0.0.5", "127.0.0.1"}
_TEST_PUBLIC_ADDR = "93.184.216.34"


def _recording_resolver(checked: list[str]):
    """Stand-in for ``_resolve_and_vet`` that records, enforces, and answers.

    Deliberately not the real function: that one calls ``socket.getaddrinfo``,
    so asserting on ORDER would make the public hops depend on live DNS. What
    the real vetting blocks is already covered by ``TestHostnameSafetyCheck``
    above — these tests are about WHEN it runs, which is the whole of M-43.

    It patches ``_resolve_and_vet`` rather than a yes/no checker so that pinning
    stays ON. An earlier version substituted a checker that returned nothing,
    and the production code carried a branch to cope with that — a test seam
    that could silently disable pinning. Returning a real address instead means
    the tests exercise the same path production does.
    """

    def resolve(url: str) -> list[str]:
        checked.append(url)
        if httpx.URL(url).host in _BLOCKED_TEST_HOSTS:
            raise HTTPException(status_code=400, detail=f"Blocked: {url}")
        return [_TEST_PUBLIC_ADDR]

    return resolve


def _logical_url(request: httpx.Request) -> str:
    """The URL a pinned request is *for*, as opposed to the one it connects to.

    Pinning rewrites the URL to the vetted address, so ``request.url`` is the
    address form. The logical host travels in the ``Host`` header, which is what
    these tests mean when they talk about which URL was requested.
    """
    host = request.headers.get("Host") or request.url.netloc.decode()
    return f"{request.url.scheme}://{host}{request.url.raw_path.decode()}"


@pytest.mark.unit
@pytest.mark.asyncio
class TestRedirectSsrf:
    async def test_a_redirect_to_a_private_host_is_never_requested(
        self, monkeypatch
    ) -> None:
        """The finding. ``follow_redirects=True`` sent the GET before any check.

        httpx walked the chain inside ``client.stream`` and only handed back the
        final response, so the old ``_resolve_and_vet(str(resp.url))`` ran
        after the request to the metadata service had been sent and answered. It
        stopped the body being read; it did not stop the request.

        Asserting on the transport rather than on the exception is the point: a
        version that raises 400 while still having issued the GET passes any
        test that only checks the status code.
        """
        requested: list[str] = []
        checked: list[str] = []
        metadata = "http://169.254.169.254/latest/meta-data/"

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(_logical_url(request))
            # Dispatch on the Host header: pinning means ``request.url.host`` is
            # the vetted address, identical for every hop.
            if request.headers["Host"] == "attacker.example":
                return httpx.Response(302, headers={"location": metadata})
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"secrets"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            _recording_resolver(checked),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://attacker.example/r")

        assert exc.value.status_code == 400
        assert metadata in checked, "the redirect target was never validated"
        assert metadata not in requested, (
            f"the GET to the metadata service was issued anyway: {requested}"
        )

    async def test_a_public_redirect_chain_still_works(self, monkeypatch) -> None:
        """OVER-REFUSAL GUARD. Redirects are normal; only unsafe hops are refused.

        Walking the chain by hand is easy to get wrong in the direction of
        refusing everything, which would pass the test above and break ingest.
        """
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(_logical_url(request))
            if request.url.path == "/start":
                return httpx.Response(
                    302, headers={"location": "https://elsewhere.example/final"}
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=b"<html><body><p>Arrived.</p></body></html>",
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            _recording_resolver([]),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        result = await _fetch_url_text("https://example.com/start")
        assert "Arrived" in result
        assert requested == [
            "https://example.com/start",
            "https://elsewhere.example/final",
        ]

    async def test_a_relative_location_is_resolved_against_its_hop(
        self, monkeypatch
    ) -> None:
        """``Location`` may be relative, and the resolved URL is what gets checked.

        Handing the raw header to the next iteration would check ``/next`` — no
        hostname at all — and the real ``_resolve_and_vet`` 400s on that
        rather than following it anywhere. Resolving keeps the redirect working
        AND keeps the thing we validate identical to the thing we request.
        """
        requested: list[str] = []
        checked: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(_logical_url(request))
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/next"})
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"done"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            _recording_resolver(checked),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        result = await _fetch_url_text("https://example.com/start")
        assert result == "done"
        assert checked == ["https://example.com/start", "https://example.com/next"]
        assert requested == checked, (
            "a URL was requested that was not the one validated"
        )

    async def test_an_endless_redirect_chain_is_capped(self, monkeypatch) -> None:
        """``follow_redirects=False`` means httpx no longer enforces a limit.

        Without a cap of our own this loops forever, holding a worker and firing
        an outbound request per iteration — a denial of service handed to us by
        the caller's own URL.
        """
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(_logical_url(request))
            nxt = int(request.url.params.get("n", "0")) + 1
            return httpx.Response(
                302, headers={"location": f"https://example.com/r?n={nxt}"}
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            _recording_resolver([]),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://example.com/r?n=0")

        assert exc.value.status_code == 400
        assert "too many redirects" in exc.value.detail.lower()
        assert len(requested) == ingest_service.MAX_INGEST_REDIRECTS + 1, requested

    async def test_the_hostname_check_runs_off_the_event_loop(
        self, monkeypatch
    ) -> None:
        """``socket.getaddrinfo`` blocks and takes no timeout.

        Checking each hop means running it up to six times per fetch where the
        old code ran it twice, so it moved to an executor. Asserting on the
        THREAD rather than on timing: the property is categorical, and a timing
        assertion here would be flaky for no extra confidence.
        """
        loop_thread = threading.current_thread().name
        ran_on: list[str] = []

        def check(url: str) -> list[str]:
            ran_on.append(threading.current_thread().name)
            return [_TEST_PUBLIC_ADDR]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"ok"
            )

        monkeypatch.setattr("core_api.services.ingest_service._resolve_and_vet", check)
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        await _fetch_url_text("https://example.com/")
        assert ran_on, "the hostname check never ran"
        assert all(t != loop_thread for t in ran_on), (
            f"the blocking resolver ran on the event loop thread: {ran_on}"
        )


def _fake_getaddrinfo(*addrs: str):
    """Stand in for DNS so the REAL ``_resolve_and_vet`` runs against known answers."""

    def resolver(host, port, *a, **kw):
        return [(socket.AF_INET, 0, 0, "", (addr, 0)) for addr in addrs]

    return resolver


@pytest.mark.unit
@pytest.mark.asyncio
class TestAddressPinning:
    """The connection goes to the address that was vetted, not to the name again.

    ``_resolve_and_vet`` resolves a name and httpx used to resolve the SAME name
    when connecting. An attacker answering the first lookup with a public address
    and the second with a private one walks through a check that passed honestly
    — DNS rebinding, which the first cut of M-43 left open on purpose.

    These are mock-transport tests, so they cover URL construction and the
    headers/extensions plumbing. They CANNOT cover the TLS handshake: MockTransport
    replaces the transport entirely. That half was verified against a real server —
    pinned fetch succeeds with the SNI override, and is refused without it.
    """

    async def test_the_request_goes_to_the_vetted_address(self, monkeypatch) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"ok"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34"),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        assert await _fetch_url_text("https://example.com/doc") == "ok"
        req = seen[0]
        assert req.url.host == "93.184.216.34", (
            f"connected by name, not address: {req.url}"
        )
        assert req.url.path == "/doc", "pinning lost the path"
        assert req.headers["Host"] == "example.com", "vhost routing would break"
        assert req.extensions.get("sni_hostname") == "example.com", (
            "without the SNI override the certificate is verified against the IP "
            "and every https fetch fails"
        )

    async def test_a_relative_redirect_keeps_the_hostname(self, monkeypatch) -> None:
        """The bug pinning nearly introduced.

        ``resp.url`` is the PINNED url once we rewrite it, so resolving a
        relative ``Location`` against it yields an address-based URL — the
        hostname is gone, and with it the next hop's Host header, SNI name and
        its own vetting. The join has to use the logical URL.
        """
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "/next"})
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"done"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34"),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        assert await _fetch_url_text("https://example.com/start") == "done"
        assert len(seen) == 2, seen
        assert seen[1].url.path == "/next"
        assert seen[1].headers["Host"] == "example.com", (
            f"the second hop lost the hostname: Host={seen[1].headers.get('Host')!r}"
        )
        assert seen[1].extensions.get("sni_hostname") == "example.com"

    async def test_one_private_answer_poisons_the_whole_name(self, monkeypatch) -> None:
        """Every address must pass, not just the one we would have picked.

        A name answering with both a public and a private address must be
        refused outright — otherwise an attacker chooses which answer we use by
        controlling ordering.
        """
        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34", "169.254.169.254"),
        )
        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://example.com/")
        assert exc.value.status_code == 400
        assert "169.254.169.254" in exc.value.detail


@pytest.mark.unit
class TestPinnedUrlFormatting:
    """Pure formatting — no network, no loop, and easy to get wrong."""

    def test_pinning_preserves_port_and_brackets_ipv6(self) -> None:
        assert ingest_service._pin_url_to_address(
            "https://example.com/x", "1.2.3.4"
        ) == (
            "https://1.2.3.4/x",
            "example.com",
        )
        assert ingest_service._pin_url_to_address(
            "https://example.com:8443/x", "1.2.3.4"
        ) == (
            "https://1.2.3.4:8443/x",
            "example.com:8443",
        )
        pinned, host = ingest_service._pin_url_to_address(
            "https://example.com/x", "2606:2800::1"
        )
        assert pinned == "https://[2606:2800::1]/x", (
            "an unbracketed IPv6 authority does not parse"
        )
        assert host == "example.com"

    def test_host_header_brackets_an_ipv6_literal_source_host(self) -> None:
        """``urlparse`` hands back v6 hostnames unbracketed; the header needs them.

        A public IPv6 literal passes vetting and reaches here, so the malformed
        header is reachable, not theoretical.
        """
        _, host = ingest_service._pin_url_to_address(
            "https://[2606:2800::1]/x", "93.184.216.34"
        )
        assert host == "[2606:2800::1]", f"invalid Host header: {host!r}"

        _, host = ingest_service._pin_url_to_address(
            "https://[2606:2800::1]:8443/x", "93.184.216.34"
        )
        assert host == "[2606:2800::1]:8443", (
            f"unbracketed v6 host + port is unparseable: {host!r}"
        )

    def test_pinning_keeps_url_credentials(self) -> None:
        """httpx turns URL userinfo into Basic auth; dropping it breaks the fetch.

        The Host header never carries userinfo.
        """
        pinned, host = ingest_service._pin_url_to_address(
            "https://user:pa%40ss@example.com:8443/x", "1.2.3.4"
        )
        assert pinned == "https://user:pa%40ss@1.2.3.4:8443/x", (
            f"credentials silently stripped by pinning: {pinned!r}"
        )
        assert host == "example.com:8443", "userinfo must not reach the Host header"


@pytest.mark.unit
@pytest.mark.asyncio
class TestCredentialsAcrossRedirects:
    """Guards the risk that keeping userinfo introduces.

    Carrying credentials through pinning is only safe while they stop at the
    host they were given for. This is the companion to
    ``test_pinning_keeps_url_credentials``: that one keeps them, this one
    bounds them.
    """

    async def test_credentials_do_not_survive_a_cross_host_redirect(
        self, monkeypatch
    ) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            # Exact match, not a prefix: a prefix test on a host is the shape
            # that makes "example.com.attacker.test" pass, and CodeQL is right
            # to flag it even in a mock router. There is no port here — the URL
            # carries none, so the pinned Host header is the bare name.
            if request.headers["Host"] == "example.com":
                return httpx.Response(
                    302, headers={"location": "https://elsewhere.test/b"}
                )
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"ok"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34"),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        assert await _fetch_url_text("https://user:pass@example.com/a") == "ok"
        assert len(seen) == 2, f"expected two hops, got {len(seen)}"
        assert "authorization" in seen[0].headers, (
            "credentials were dropped before they reached the host they belong to"
        )
        assert "authorization" not in seen[1].headers, (
            "credentials leaked to a host the user never gave them to: "
            f"{seen[1].headers.get('authorization')!r}"
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestPerHopConnections:
    """Each hop gets its own connection — a real-socket test, because it must be.

    httpcore pools by the request URL's origin and reads ``sni_hostname`` only
    when it opens a connection. Pinning collapses every hop onto one origin when
    the addresses match, so a reused connection would carry a later hop over TLS
    verified for an earlier hop's name.

    Nothing is pooled today, but only because a redirect response is closed
    without its body being read. This does not fail without the accompanying
    ``max_keepalive_connections=0`` — it fails if some later change starts
    draining redirect bodies, which is exactly the regression that setting makes
    unreachable. MockTransport models no pooling at all, so it cannot be written
    against a mock.
    """

    async def test_each_hop_opens_its_own_connection(self, monkeypatch) -> None:
        import asyncio

        seen: list[tuple[int, str]] = []

        async def serve(reader, writer) -> None:
            peer_port = writer.get_extra_info("peername")[1]
            while True:
                line = await reader.readline()
                if not line:
                    break
                path = line.decode().split(" ")[1]
                headers = {}
                while True:
                    hl = await reader.readline()
                    if hl in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = hl.decode().partition(":")
                    headers[k.strip().lower()] = v.strip()
                seen.append((peer_port, headers.get("host", "")))
                if path == "/":
                    # Empty body keeps the connection cleanly reusable, which is
                    # the shape where reuse could actually bite.
                    writer.write(
                        b"HTTP/1.1 302 Found\r\n"
                        b"Location: http://second.test:%d/final\r\n"
                        b"Content-Length: 0\r\n"
                        b"Connection: keep-alive\r\n\r\n" % port
                    )
                else:
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                        b"Content-Length: 2\r\nConnection: keep-alive\r\n\r\nok"
                    )
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        # Point the fake hostnames at the loopback server. Pinning stays on:
        # patching the vetting step is how these tests supply an answer, not a
        # way to switch pinning off.
        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: ["127.0.0.1"],
        )

        async with server:
            body = await asyncio.wait_for(
                _fetch_url_text(f"http://first.test:{port}/"), timeout=10
            )

        assert body == "ok"
        assert len(seen) == 2, f"expected two hops, got {seen}"
        assert seen[0][1] != seen[1][1], f"hops did not differ in Host: {seen}"
        assert seen[0][0] != seen[1][0], (
            "both hops rode one TCP connection; over TLS the second host would "
            f"have used the first host's verified session: {seen}"
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestOverallFetchBudget:
    """Per-hop budgets multiply; only a whole-fetch deadline bounds the total.

    Six hops x (5s DNS + 30s request) is 210s a caller-supplied chain can spend
    holding a coroutine, which overruns the 120s request timeout this platform
    deploys with — so the caller would get the proxy's 504 instead of this
    module's 400.
    """

    async def test_a_slow_chain_is_cut_off_with_a_400(self, monkeypatch) -> None:
        import asyncio as _asyncio

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            await _asyncio.sleep(10)
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"too late"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(slow_handler, **kw),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.MAX_INGEST_FETCH_SECONDS", 0.2
        )

        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://slow.example/x")
        assert exc.value.status_code == 400
        assert "budget" in exc.value.detail.lower()

    async def test_a_prompt_fetch_is_untouched(self, monkeypatch) -> None:
        """OVER-REFUSAL GUARD. A deadline that fires on normal traffic is worse
        than no deadline — it turns a working ingest into a 400."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"quick"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )
        assert await _fetch_url_text("https://example.com/x") == "quick"


@pytest.mark.unit
@pytest.mark.asyncio
class TestRedirectWithoutLocation:
    """A 3xx carrying no ``Location`` is not a redirect, and must not crash.

    ``resp.is_redirect`` is the STATUS CODE alone — httpx's own docstring says
    to use ``has_redirect_location`` when the header matters. Indexing
    ``headers["location"]`` behind an ``is_redirect`` check therefore raises
    KeyError on a malformed 3xx, which leaves this module as a 500 from a
    function that returns clean 4xx for everything else. The fetched URL is
    tenant-controlled by design, so a server can simply choose to answer this
    way.

    The comment on that line used to assert ``is_redirect`` meant "status AND
    Location". It never did. Same defect class this PR fixed elsewhere: a
    comment claiming a property the code does not have.

    Note what this is NOT: it is not a regression the redirect work introduced.
    Falling through to the normal path hits ``raise_for_status()``, which raises
    on 3xx too, and ``upstream_http_error_handler`` re-raises sub-500s into the
    catch-all — so the pre-M-43 code answered this with a 500 as well. The
    KeyError was a worse 500, not a new failure. 400 is better than either.
    """

    async def test_a_3xx_without_a_location_header_is_a_400(self, monkeypatch) -> None:
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(_logical_url(request))
            return httpx.Response(
                302, headers={"content-type": "text/plain"}, content=b"no location here"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://example.com/x")
        assert exc.value.status_code == 400
        assert "no location" in exc.value.detail.lower()
        assert requested == ["https://example.com/x"], (
            f"a Location-less 3xx was treated as a hop: {requested}"
        )

    async def test_a_3xx_with_a_location_is_still_followed(self, monkeypatch) -> None:
        """Over-refusal guard: the new branch must not swallow real redirects."""
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(_logical_url(request))
            if request.headers["Host"] == "example.com":
                return httpx.Response(
                    302, headers={"location": "https://onward.test/y"}
                )
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"arrived"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service._resolve_and_vet",
            lambda url: [_TEST_PUBLIC_ADDR],
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        assert await _fetch_url_text("https://example.com/x") == "arrived"
        assert requested == ["https://example.com/x", "https://onward.test/y"]


@pytest.mark.unit
class TestSchemeAllowlistVetting:
    """Only http/https. A redirect target is attacker-controlled.

    Nothing downstream rejects another scheme: ``ftp://public-host/x`` resolves,
    vets and pins cleanly, and only dies inside httpx's transport selection as
    ``UnsupportedProtocol`` — uncaught here, so a 500 out of the one function
    that turns every other malformed input into a 400.
    """

    @pytest.mark.parametrize("url", ["ftp://example.com/x", "gopher://example.com/x"])
    def test_a_non_http_scheme_with_a_host_is_refused(self, url: str) -> None:
        with pytest.raises(HTTPException) as exc:
            ingest_service._resolve_and_vet(url)
        assert exc.value.status_code == 400
        assert "scheme" in exc.value.detail.lower()

    def test_a_hostless_url_is_still_refused_on_its_hostname(self) -> None:
        """``file:///etc/passwd`` never needed the scheme check — it has no host.

        Pins the ORDER: the hostname branch runs first, so inputs that were
        already refused keep the message they had. Putting the scheme check
        ahead of it silently rewrote this case, and ``not-a-valid-url`` with it.
        """
        with pytest.raises(HTTPException) as exc:
            ingest_service._resolve_and_vet("file:///etc/passwd")
        assert exc.value.status_code == 400
        assert "no hostname" in exc.value.detail.lower()


@pytest.mark.unit
@pytest.mark.asyncio
class TestSchemeAllowlistOverRedirects:
    async def test_a_redirect_to_a_non_http_scheme_is_a_400(self, monkeypatch) -> None:
        """The reachable path: the caller's URL is fine, the Location is not."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "ftp://elsewhere.test/x"})

        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34"),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://example.com/x")
        assert exc.value.status_code == 400
        assert "scheme" in exc.value.detail.lower()

    async def test_an_ordinary_https_chain_is_unaffected(self, monkeypatch) -> None:
        """OVER-REFUSAL GUARD, including the http hop.

        An https -> http downgrade stays PERMITTED: this fetches public content,
        and credentials cannot ride a downgrade — only an absolute ``Location``
        can change scheme, and an absolute reference replaces the authority,
        which drops userinfo with it.
        """

        def handler(request: httpx.Request) -> httpx.Response:
            if request.headers["Host"] == "example.com":
                return httpx.Response(302, headers={"location": "http://plain.test/y"})
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"fetched"
            )

        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34"),
        )
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )
        assert await _fetch_url_text("https://example.com/x") == "fetched"


@pytest.mark.unit
class TestMalformedPort:
    """A bad port must 400 like every other bad URL, not escape as a 500."""

    def test_a_non_integer_port_is_rejected_by_vetting(self) -> None:
        """``.port`` raises ValueError, and it is read later by the pinning step.

        ``_resolve_and_vet`` only ever looked at ``.hostname``, so such a URL
        passed vetting and then blew up inside ``_pin_url_to_address`` — an
        unhandled ValueError, i.e. a 500, from a module that returns a clean 400
        for every other malformed input.
        """
        with pytest.raises(HTTPException) as exc:
            ingest_service._resolve_and_vet("https://example.com:abc/x")
        assert exc.value.status_code == 400
        assert "port" in exc.value.detail.lower()

    def test_a_valid_port_still_resolves(self, monkeypatch) -> None:
        """Over-refusal guard: the check must not reject ordinary ports."""
        monkeypatch.setattr(
            "core_api.services.ingest_service.socket.getaddrinfo",
            _fake_getaddrinfo("93.184.216.34"),
        )
        assert ingest_service._resolve_and_vet("https://example.com:8443/x") == [
            "93.184.216.34"
        ]


@pytest.mark.unit
@pytest.mark.asyncio
class TestDnsTimeout:
    """Hostile DNS must not hold a request open, nor reach the shared pool.

    The two guards do different jobs and neither replaces the other:
    ``wait_for`` bounds the REQUEST but cannot reclaim the thread (a running
    executor future is not cancellable), so the dedicated pool is what bounds
    the BLAST RADIUS.
    """

    async def test_a_hanging_resolver_becomes_a_400(self, monkeypatch) -> None:
        import time

        def hang(url: str) -> list[str]:
            time.sleep(30)
            return ["93.184.216.34"]

        monkeypatch.setattr("core_api.services.ingest_service._resolve_and_vet", hang)
        monkeypatch.setattr(
            "core_api.services.ingest_service.DNS_RESOLUTION_TIMEOUT", 0.2
        )

        with pytest.raises(HTTPException) as exc:
            await _fetch_url_text("https://slow-dns.example/")
        assert exc.value.status_code == 400
        assert "timed out" in exc.value.detail.lower()

    async def test_dns_does_not_run_on_the_shared_default_executor(
        self, monkeypatch
    ) -> None:
        """Confinement is the half that stops a cross-tenant DoS.

        Asserting the thread name rather than timing: the property is
        categorical, and the default executor's threads are the ones the whole
        ASGI app shares for blocking work.
        """
        ran_on: list[str] = []

        def check(url: str) -> list[str]:
            ran_on.append(threading.current_thread().name)
            return [_TEST_PUBLIC_ADDR]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=b"ok"
            )

        monkeypatch.setattr("core_api.services.ingest_service._resolve_and_vet", check)
        monkeypatch.setattr(
            "core_api.services.ingest_service.httpx.AsyncClient",
            lambda **kw: _passthrough_client(handler, **kw),
        )

        await _fetch_url_text("https://example.com/")
        assert ran_on, "the resolver never ran"
        assert all(name.startswith("ingest-dns") for name in ran_on), (
            "DNS ran outside the dedicated pool — on the default executor a "
            f"hostile resolver stalls unrelated blocking work: {ran_on}"
        )
