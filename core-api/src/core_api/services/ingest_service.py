"""Document/URL ingestion: extract atomic facts via LLM, preview, and commit as memories."""

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import BULK_MAX_ITEMS, MEMORY_TYPES
from core_api.providers._retry import call_with_fallback
from core_api.schemas import (
    BulkMemoryCreate,
    BulkMemoryItem,
    IngestCommitRequest,
    IngestRequest,
)
from core_api.services.ingest_chunking import (
    DOC_HARD_TOKEN_LIMIT,
    chunk_blocks,
    doc_token_count,
    parse,
)
from core_api.services.memory_service import _content_hash, create_memories_bulk
from core_api.services.organization_settings import resolve_config

# Document extraction (PDF / Office / EPUB / …) is an optional ``ingest`` extra:
# kreuzberg is ~64MB and is excluded from the default image to cut cold-start
# pull + import time. When absent, the ingest endpoints return 501 (see
# ``_extract_with_kreuzberg``); every other code path works unchanged.
try:
    import kreuzberg
except ImportError:  # pragma: no cover - only in slim (no-ingest) builds
    kreuzberg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# MIME types decoded as text. HTML/XHTML get the tag-strip + whitespace-
# collapse treatment; everything else (plain/markdown/csv) preserves
# newlines so the chunker's heading detection and CSV row structure stay
# intact. Bypasses Kreuzberg entirely (cheaper).
TEXT_INGEST_MIME_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",  # PR #9: CSV support
        "application/xhtml+xml",
    }
)

# Binary MIME types we route through Kreuzberg's ``extract_bytes`` to recover
# plain text (PR #8). Kreuzberg supports 88+ formats; the list below is the
# curated subset we accept — everything we expect users to ingest from a URL.
# Adding a new format = append to this set; no code change.
#
# Notes:
#   - PDFs: text-PDFs work out of the box; image-only PDFs need Tesseract on
#     the host (not currently installed in our images, so they'll either
#     return empty content or raise ParsingError → we surface as 422).
#   - Encrypted PDFs without a password raise ParsingError → 422.
BINARY_INGEST_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        "application/epub+zip",
        "application/rtf",
        "application/vnd.oasis.opendocument.text",  # .odt
    }
)

# Union for the allowlist check. Anything outside both sets is a 422.
ALLOWED_INGEST_MIME_TYPES = TEXT_INGEST_MIME_TYPES | BINARY_INGEST_MIME_TYPES

# Hard byte cap on every ingest input path (PR #9):
#   - HTTP request body for /ingest/preview, /ingest/commit, /ingest/file
#   - URL-fetched body (post-decompression — gzip-bomb guard)
#   - Multipart file upload
# Single knob so "max file size" is consistent across surfaces.
INGEST_MAX_INPUT_BYTES = 3_000_000  # 3 MB

# Back-compat alias for code/tests written before the unification.
MAX_INGEST_CONTENT_BYTES = INGEST_MAX_INPUT_BYTES

# M-43. Redirects are walked by hand so every hop can be checked BEFORE it is
# requested, which means this module owns the cap httpx used to own. Lower than
# httpx's default of 20: a legitimate document fetch does not need five hops,
# and each one is an outbound request from inside the deployment network.
#
# This also TIGHTENS the wall-clock ceiling rather than loosening it. httpx
# applies ``timeout`` per hop (``_send_handling_redirects`` calls
# ``_send_single_request`` in a loop), so the old ``follow_redirects=True`` with
# the default 20-redirect cap allowed ~21 x 30s. Six hops is 3.5x less.
MAX_INGEST_REDIRECTS = 5

# Explicit deny-list for cloud-metadata service IPs that aren't always
# caught by ipaddress.is_link_local (AWS 169.254.169.254 IS link-local;
# GCP metadata at metadata.google.internal resolves to 169.254.169.254 too;
# Azure uses the same IP). Listed defensively even though is_link_local
# covers them.
_CLOUD_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})

# Max concurrent ``_chunk_content`` LLM calls during preview. Post-PR#7
# the chunker emits N sections per doc; this bounds the LLM fanout to
# avoid rate-limit storms while still parallelizing the ~5s per-section
# latency.
_PREVIEW_CONCURRENCY = 4

# Minimum content length before we'll even call the LLM. Whitespace-only
# inputs and trivially short ones ("hi") used to burn a real LLM call
# producing useless meta-facts ("The content begins with the greeting
# 'hi'"). We short-circuit instead and return ``skipped_reason``.
_INGEST_MIN_CONTENT_CHARS = 20


# Parent-Document collection name. Each successful ingest_commit writes ONE
# row into ``documents`` with this collection and ``doc_id=<run_id>`` so the
# batch of memories has a queryable, embeddable parent record. Each memory
# joins back via the top-level ``memories.run_id`` column.
INGEST_DOCUMENTS_COLLECTION = "ingest-sources"

# Max chars from joined fact contents to put in the parent Document's
# ``data["summary"]`` (which gets embedded for semantic search). Keeps the
# embed payload bounded; ~500 chars is plenty for a useful similarity hit.
_PARENT_DOC_SUMMARY_CAP = 500


# A2: doc-hash for ingest idempotency. We hash the post-truncate text the LLM
# will actually see so two calls with identical content (modulo the bytes we
# don't process) deterministically collide. Tenant-scoped so the same content
# from different tenants doesn't accidentally share a cache. Note we do NOT
# include focus / source_uri / fleet_id — re-running with a different focus
# on the same doc should still hit cache (we extracted EVERYTHING; the focus
# was only a prompt hint). If we ever switch to focus-targeted extraction
# the keying needs to change.
def _doc_hash(tenant_id: str, content: str) -> str:
    """SHA-256 of (tenant_id, content). Returns the hex digest."""
    return hashlib.sha256(f"{tenant_id}:{content}".encode()).hexdigest()


# A1: drop extracted facts whose LLM-emitted salience falls below this floor.
# Lower-numbered = essential standalone fact, higher number toward 1.0. The
# 0.5 threshold is empirically anchored — adjustable via tenant config in a
# follow-up if needed.
_SALIENCE_FLOOR = 0.5

# Minimum word count for a kept fact. A5 forbids the LLM from emitting these
# in the first place, but real-world prompts still produce short fragments;
# the validator drops them. "≥ 5 words" is the boundary — anything shorter
# is almost always a heading, label, or one-word fragment.
_MIN_FACT_WORDS = 5

# Scripts that do not put spaces between words. A whitespace split reports 1
# for an entire Chinese or Japanese sentence, so ``len(body.split())`` dropped
# every CJK fact as a "sub-5-word fragment" — the filter deleted the content it
# was meant to protect, and only for those languages.
#
# Hangul is deliberately EXCLUDED: Korean is space-delimited, so counting each
# syllable as a word would over-count it and let real fragments through.
_CJK_RE = re.compile(
    "["
    "\u3040-\u309f"  # hiragana
    "\u30a0-\u30ff"  # katakana
    "\u3400-\u4dbf"  # CJK unified ext A
    "\u4e00-\u9fff"  # CJK unified
    "\uf900-\ufaff"  # CJK compatibility
    "\uff66-\uff9f"  # halfwidth katakana
    "]"
)


def _fact_word_count(text: str) -> int:
    """Word count that survives a script without spaces.

    Each CJK character counts as one unit and the remainder is split on
    whitespace, so mixed text ("Acme の売上は 12% 増加した") is counted once,
    not twice. One character per unit is deliberately generous — a CJK word is
    typically one to two characters, so this errs toward KEEPING a short fact
    rather than silently dropping a real one, which is the failure being fixed.
    """
    cjk = len(_CJK_RE.findall(text))
    return len(_CJK_RE.sub(" ", text).split()) + cjk


# Drop facts that describe the input itself rather than extracting from it.
# These show up when the LLM has nothing real to chunk — typical on short
# inputs that slipped past ``_INGEST_MIN_CONTENT_CHARS``. Belt-and-braces
# with the prompt guidance below.
_META_FACT_RE = re.compile(
    r"^\s*(?:"
    r"the\s+(?:provided|user|input)\s+(?:content|text|document)"  # "the provided content"
    r"|this\s+(?:content|text|document)"  # "this document describes"
    r"|the\s+content\s+(?:begins|starts|consists|is)"  # "the content begins"
    r"|the\s+(?:document|text)\s+(?:provided|given|describes|is)"  # "the document describes"
    r")",
    re.IGNORECASE,
)

CHUNKING_PROMPT = """\
Extract discrete, atomic facts from the following content for storage in an
agent's long-term memory. Each fact must stand on its own when retrieved
later without the surrounding document.
{breadcrumb_instruction}
## Rules

1. **Self-contained.** Every fact must be understandable without the original
   document. Resolve pronouns, references, and "the" + ambiguous noun against
   the document. "He shipped it" is bad; "Bob shipped v2.3 of the SDK on
   March 15" is good.

2. **Atomic.** One claim per fact. A sentence containing "X happened and Y
   was decided" becomes two facts unless one of them is trivial.

3. **No duplicates or near-duplicates.** If the document repeats a claim,
   emit it once. If two paragraphs say the same thing in different words,
   emit it once. Better to under-extract than to clone.

4. **Substantive only.** At least 5 words per fact. No UI labels ("Learn
   more", "Subscribe"), no headings ("Section 3"), no boilerplate ("All
   rights reserved"), no questions, no TODOs without an answer.

5. **No meta-facts.** Do not describe the input itself. Avoid claims like
   "The content begins with...", "The provided text says...", "This
   document is about...". Extract facts FROM the content, not facts ABOUT
   the content.

6. **Salience score** (0.0 to 1.0). Rate how essential each fact is for
   later recall. Use this scale:
     - 1.0 = critical, would be sorely missed if absent
     - 0.7 = useful specifics: names, numbers, dates, decisions, outcomes
     - 0.5 = relevant but not load-bearing
     - 0.3 = arguably extractable but mostly noise
     - 0.0 = filler / restatement
   Be honest. Anything below 0.5 will be dropped automatically.

7. **memory_type.** Pick the most specific tag. When in doubt prefer the
   left option in each pair:
     - fact         — a stable proposition about the world ("Iron melts at 1538°C")
     - decision     — a chosen course of action by an identified actor
     - task         — work item assigned but not yet finished
     - plan         — intended future action stated as plan
     - outcome      — past event/result; if you'd write "X happened" or "Y
                      was completed", use this (not "fact")
     - preference   — a stated like/dislike
     - intention    — what someone aims to do
     - commitment   — explicit promise
     - action       — something done (granular than outcome)
     - episode      — narrative event tied to a specific moment
     - semantic     — definitional/conceptual relationship
     - cancellation — explicit revocation of a prior plan/commitment

## Quantity guidance

Extract 5-20 facts depending on content length. Err toward fewer, higher-
salience facts over many low-salience ones.

{focus_instruction}

## Content

{content}

## Output

Return ONLY a valid JSON object with a "facts" key. Each item:

{{"facts": [
  {{"content": "...", "suggested_type": "fact", "salience": 0.9}},
  ...
]}}
"""


def _fake_ingest() -> list:
    """No-LLM fallback: return empty list so validation yields 0 facts."""
    logger.warning("ingest: no LLM credentials — fact extraction skipped, returning 0 facts")
    return []


async def _chunk_content(
    text: str,
    focus: str | None = None,
    tenant_config=None,
    breadcrumb: str | None = None,
) -> list[dict]:
    """Extract atomic facts from text via LLM.

    The chunker module produces section-sized text up to ~3k tokens;
    callers pass each section here. The optional ``breadcrumb`` is the
    heading trail of where this section sits in its source document
    (e.g. ``"Release Notes > v2.3 > Performance"``) and gets injected
    into the prompt as separate context so the LLM can disambiguate
    references like "this release" or "the migration".

    ``breadcrumb`` is the only addition; everything else (focus, meta-fact
    filter, salience floor, short-fact filter) is unchanged from PR #5.
    """
    provider_name = (
        tenant_config.enrichment_provider if tenant_config else None
    ) or settings.entity_extraction_provider

    focus_instruction = ""
    if focus:
        focus_instruction = f"Focus on facts relevant to {focus}. Deprioritize unrelated details."

    breadcrumb_instruction = ""
    if breadcrumb:
        breadcrumb_instruction = (
            f"\n## Document context\n\nThis section appears under: {breadcrumb}\n"
            "Use this trail to resolve ambiguous references (e.g. 'this version', "
            "'the migration') when extracting facts.\n"
        )

    prompt = CHUNKING_PROMPT.format(
        content=text,
        focus_instruction=focus_instruction,
        breadcrumb_instruction=breadcrumb_instruction,
    )

    async def _do_chunk(llm):
        return await llm.complete_json(prompt)

    raw = await call_with_fallback(
        primary_provider_name=provider_name,
        call_fn=_do_chunk,
        fake_fn=_fake_ingest,
        tenant_config=tenant_config,
        service_label="ingest",
    )

    # Validate: must be a list of objects with "content"
    facts: list[dict] = []
    if isinstance(raw, dict):
        # Handle {"facts": [...]} wrapper
        for v in raw.values():
            if isinstance(v, list):
                raw = v
                break
    dropped_meta = 0
    dropped_low_salience = 0
    dropped_short = 0
    for item in raw:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        body = str(item["content"]).strip()

        # P2.4: drop facts that describe the input rather than extract
        # from it. Prompt forbids them but the LLM still produces them
        # occasionally — especially on short/trivial input.
        if _META_FACT_RE.search(body):
            dropped_meta += 1
            continue

        # A5: drop sub-5-word fragments. Prompt forbids them but the LLM
        # still emits short headings/labels on noisy inputs.
        if _fact_word_count(body) < _MIN_FACT_WORDS:
            dropped_short += 1
            continue

        # A1: drop low-salience facts. The LLM emits a per-fact 0-1
        # salience score (see CHUNKING_PROMPT); anything below the floor
        # is dropped without ever reaching the write pipeline.
        salience_raw = item.get("salience")
        try:
            salience = float(salience_raw) if salience_raw is not None else None
        except (TypeError, ValueError):
            salience = None
        if salience is not None and salience < _SALIENCE_FLOOR:
            dropped_low_salience += 1
            continue

        st = item.get("suggested_type", "fact")
        if st not in MEMORY_TYPES:
            st = "fact"

        fact_out: dict = {"content": body, "suggested_type": st}
        # Surface salience on the returned fact when present, so the
        # caller (preview UI / agent) can sort / threshold / display it.
        # Existing callers ignore the new field — backward compatible.
        if salience is not None:
            fact_out["salience"] = salience
        facts.append(fact_out)

    if dropped_meta or dropped_low_salience or dropped_short:
        logger.info(
            "ingest: filtered %d fact(s) from extraction output (meta=%d, low_salience=%d, short=%d)",
            dropped_meta + dropped_low_salience + dropped_short,
            dropped_meta,
            dropped_low_salience,
            dropped_short,
        )

    return facts


def _is_blocked_ip(addr: str) -> bool:
    """Return True if the address falls in a range we must not fetch from.

    Covers RFC1918 private ranges, loopback, link-local (incl. AWS/GCP/Azure
    metadata IPs), multicast, and reserved. IPv6 unique-local fc00::/7 is
    classified as private by the ipaddress module.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


# Wall-clock ceiling for one hop's DNS. Real answers land in milliseconds; this
# is generous for a slow-but-honest resolver and short against a hostile one.
DNS_RESOLUTION_TIMEOUT = 5.0

# Ceiling for the WHOLE fetch, redirect chain included. Reasoned about in
# ``_fetch_url_text``; the short version is that per-hop budgets multiply and
# this is the only number a caller-supplied chain cannot inflate.
MAX_INGEST_FETCH_SECONDS = 60.0

# DNS runs HERE rather than on the default executor, and the two protections
# are not interchangeable:
#
#   asyncio.wait_for bounds the REQUEST. It does NOT reclaim the thread — a
#   concurrent.futures future that is already running cannot be cancelled, so
#   the worker stays inside getaddrinfo until the resolver answers or the OS
#   gives up. Measured: after a 0.5s wait_for timeout on a 3s blocking call,
#   the next task on a 1-worker pool still waited 2.52s for its turn.
#
#   The dedicated pool bounds the BLAST RADIUS. Because the timeout cannot free
#   threads, hostile DNS can pin every worker it is allowed to reach; the only
#   question is which pool those are. On the default executor that is the one
#   the whole ASGI app shares, so one tenant's slow resolver could stall
#   unrelated blocking work — a cross-tenant DoS. Confined here, a saturated
#   pool degrades ingest (later hops queue, then time out with a 400) and
#   nothing else.
#
# The hostname is attacker-supplied by design on this endpoint, so treat both
# as load-bearing rather than defensive decoration.
#
# KNOWN LIMITATION, recorded because there is no call that fixes it. A worker
# stuck in getaddrinfo delays interpreter exit: concurrent.futures.thread's
# atexit hook joins it, so SIGTERM during a hostile-DNS fetch can outlast the
# shutdown grace period. Registering a shutdown on the app's lifespan does NOT
# help — a running worker cannot be cancelled, the same limitation that makes
# the timeout above insufficient on its own. Measured, 5s blocking call, time
# to process exit: no shutdown 5.03s, shutdown(wait=False) 5.03s,
# shutdown(wait=False, cancel_futures=True) 5.03s.
#
# And do not reach for the workaround: dropping these threads from
# ``concurrent.futures.thread._threads_queues`` so atexit skips them leaves
# ``threading._shutdown`` waiting on a non-daemon thread that can no longer be
# woken, and the process hangs forever instead of for seconds. Measured too.
#
# What actually bounds this is the OS resolver's own timeout and max_workers.
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ingest-dns")


def _resolve_and_vet(url: str) -> list[str]:
    """Resolve the URL's hostname, reject private infra, and RETURN the addresses.

    Returning the vetted addresses rather than just raising is what lets the
    caller CONNECT to one of them instead of resolving the name a second time.
    Every address the name resolves to must pass — an attacker who can return
    one public and one private answer must not get to pick.
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail=f"Invalid URL: no hostname in {url!r}")
    # A redirect ``Location`` is attacker-controlled once the fetched server
    # answers, and nothing downstream rejects a non-HTTP scheme:
    # ``ftp://public-host/x`` resolves, vets and pins cleanly, then dies inside
    # httpx's transport selection as ``UnsupportedProtocol`` — an exception
    # this module does not catch, so it escapes as a 500 from the one function
    # that turns every other malformed input into a 400.
    #
    # AFTER the hostname check, not before: the schemes that reach here are the
    # ones carrying a host. ``file:///etc/passwd`` and a bare
    # ``not-a-valid-url`` have none, and both were already refused above —
    # putting scheme first would only change which message they get.
    #
    # An https -> http downgrade stays permitted. This fetches public content,
    # and credentials cannot ride a downgrade: only an ABSOLUTE ``Location``
    # can change the scheme, and an absolute reference replaces the whole
    # authority, dropping userinfo with it.
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported URL scheme {parsed.scheme!r} in {url!r} (http/https only)",
        )
    # ``.port`` raises ValueError on a non-integer port, and it is read later by
    # _pin_url_to_address. Touching it HERE means a malformed port is rejected
    # at the same point as every other invalid URL, with the same 400, instead
    # of passing vetting and then escaping this module as a 500.
    try:
        _ = parsed.port
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid port in URL {url!r}: {e}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed for {host}: {e}")
    addrs: list[str] = []
    for _family, _, _, _, sockaddr in infos:
        addr = str(sockaddr[0])
        if _is_blocked_ip(addr) or addr in _CLOUD_METADATA_IPS:
            raise HTTPException(
                status_code=400,
                detail=f"Blocked: {host} resolves to {addr} (private/loopback/link-local/metadata)",
            )
        if addr not in addrs:
            addrs.append(addr)
    if not addrs:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed for {host}: no addresses")
    return addrs


async def _resolve_and_vet_async(url: str) -> list[str]:
    """``_resolve_and_vet``, off the event loop.

    ``socket.getaddrinfo`` blocks and takes no timeout, and M-43's per-hop loop
    runs it up to ``MAX_INGEST_REDIRECTS + 1`` times where the old code ran it
    twice. Six blocking resolutions inline is six chances for one tenant's slow
    DNS to stall every other request sharing the worker — a fair objection to
    the per-hop check, and cheaper to answer than to argue with.

    A wrapper rather than making the vetting itself async: it is the policy
    function and its logic has no business knowing about event loops.

    This deliberately has NO branch for "skip pinning". An earlier cut decided
    that by comparing the module-level checker against a snapshot taken at
    import, so that tests substituting a yes/no stand-in kept working — which
    made a security-critical behaviour toggle on whether a private module
    attribute had been reassigned, silently and without a log line. Tests patch
    this function or :func:`_resolve_and_vet` instead.

    The timeout and the dedicated pool do two DIFFERENT jobs, and neither
    substitutes for the other. See :data:`_DNS_EXECUTOR`.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_DNS_EXECUTOR, _resolve_and_vet, url),
            timeout=DNS_RESOLUTION_TIMEOUT,
        )
    except TimeoutError:  # asyncio.TimeoutError is an alias of this since 3.11
        host = urlparse(url).hostname or url
        raise HTTPException(
            status_code=400,
            detail=f"DNS resolution timed out after {DNS_RESOLUTION_TIMEOUT}s for {host}",
        )


@asynccontextmanager
async def _stream_request(client: httpx.AsyncClient, request: httpx.Request):
    """``client.stream(...)``, but for a request built in advance.

    ``client.stream`` builds its own request internally, so it cannot carry the
    pinned URL, the ``Host`` header and the SNI extension. ``send(stream=True)``
    can, but hands back a plain ``Response`` that has to be closed by hand —
    ``httpx.Response`` is not an async context manager.
    """
    resp = await client.send(request, stream=True)
    try:
        yield resp
    finally:
        await resp.aclose()


def _pin_url_to_address(url: str, addr: str) -> tuple[str, str]:
    """Rewrite ``url`` to connect to ``addr``, returning (connect_url, host_header).

    DNS rebinding is the gap :func:`_resolve_and_vet` cannot close on its own:
    it resolves a name, and then httpx resolves the SAME name again when it
    connects. An attacker serving a public answer to the first lookup and a
    private one to the second walks straight through a check that passed
    honestly. Connecting to the address we already vetted removes the second
    lookup, and with it the window.

    The hostname still travels — as the ``Host`` header, and as the TLS SNI name
    the caller sets — so virtual hosting still works and the certificate is
    still verified against the NAME, not the address. Verified against a real
    server: with the SNI override the request succeeds; without it, or with the
    wrong name, the handshake is refused.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    # Both halves need brackets around an IPv6 literal: the authority so the URL
    # parses back, and the Host header so it is valid per RFC 7230. ``hostname``
    # strips the brackets the source URL had, so a v6 literal SOURCE host needs
    # them put back too — otherwise "::1" with port 8443 emits the unparseable
    # ``Host: ::1:8443``.
    host_literal = f"[{host}]" if ":" in host else host
    host_header = f"{host_literal}:{parsed.port}" if parsed.port else host_literal
    literal = f"[{addr}]" if ":" in addr else addr
    authority = f"{literal}:{parsed.port}" if parsed.port else literal
    # Userinfo rides along, or pinning would silently strip credentials that
    # httpx turns into a Basic auth header — a URL the old code fetched fine
    # would start coming back 401. Taken verbatim from the netloc rather than
    # via ``parsed.username``/``password`` so percent-encoding round-trips.
    # It never belongs in the Host header.
    #
    # Cross-host redirects do not carry it: the next hop's URL comes from
    # joining Location onto the logical URL, and an absolute Location replaces
    # the whole authority, userinfo included.
    userinfo = parsed.netloc.rpartition("@")[0]
    if userinfo:
        authority = f"{userinfo}@{authority}"
    return parsed._replace(netloc=authority).geturl(), host_header


# Kreuzberg config used by ``_extract_with_kreuzberg``. We request markdown
# output so the structure-aware chunker (PR #7) can detect headings in
# extracted PDFs/Office docs and produce breadcrumb-tagged sections, instead
# of dumping one giant plaintext blob. Created once at module load to avoid
# rebuilding it on every request.
_KREUZBERG_CFG = (
    kreuzberg.ExtractionConfig(output_format=kreuzberg.OutputFormat.MARKDOWN)
    if kreuzberg is not None
    else None
)


def decode_text_body(body: bytes, mime: str, encoding: str | None = None) -> str:
    """Decode a body classified as one of ``TEXT_INGEST_MIME_TYPES``.

    HTML / XHTML get the legacy aggressive scrub: drop ``<script>`` and
    ``<style>`` blocks, strip all tags, then collapse whitespace — web
    pages have copious noise.

    ``text/plain``, ``text/markdown``, ``text/csv``: decode + normalize
    CRLF / CR to LF, preserve every other byte. The chunker's heading
    detection relies on real newlines, and CSV becomes unreadable
    without row breaks.
    """
    decoded = body.decode(encoding or "utf-8", errors="replace")
    if mime in ("text/html", "application/xhtml+xml"):
        text = re.sub(r"<script[^>]*>.*?</script>", "", decoded, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    return decoded.replace("\r\n", "\n").replace("\r", "\n").strip()


async def _extract_with_kreuzberg(body: bytes, mime: str) -> str:
    """Hand a binary blob to Kreuzberg for text extraction (PR #8).

    Supports PDFs, Office formats, EPUB, RTF, ODT, etc. — anything in
    ``BINARY_INGEST_MIME_TYPES``. Requests markdown output so the chunker's
    heading-aware path stays useful for extracted documents. Maps Kreuzberg's
    failure modes to clean HTTP responses:

    - Encrypted PDF (``metadata.is_encrypted=True`` with empty content, or
      a ``ParsingError`` mentioning encryption) → 422.
    - Garbage / malformed blob → 422 with the Kreuzberg error message
      (callers see "Could not parse <type>: <reason>").
    - Empty extracted content (image-only PDF with no Tesseract installed)
      → 422 — better to fail loudly than to send the LLM 0 bytes.
    """
    if kreuzberg is None:
        raise HTTPException(
            status_code=501,
            detail="Document ingest is not available in this build — install the 'ingest' extra (kreuzberg).",
        )
    try:
        result = await kreuzberg.extract_bytes(body, mime, _KREUZBERG_CFG)
    except kreuzberg.ParsingError as e:
        detail = str(e)
        status = 422
        # Surface a friendlier error for the common encrypted-PDF case
        # rather than dumping Kreuzberg's raw "PdfiumLibraryInternalError".
        if "encrypted" in detail.lower() or "password" in detail.lower():
            detail = "Encrypted PDF: password-protected documents are not supported."
        raise HTTPException(status_code=status, detail=detail)
    except kreuzberg.KreuzbergError as e:
        # Catch-all for other Kreuzberg failures (OCR errors, image
        # processing errors, etc.) — never let them escape as 500s.
        raise HTTPException(status_code=422, detail=f"Document extraction failed: {e}")

    # Defensive: an encrypted PDF *can* slip past ParsingError if the
    # extractor returns metadata.is_encrypted=True with empty content
    # (depends on backend). Catch that case here.
    if (result.metadata or {}).get("is_encrypted") and not (result.content or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Encrypted PDF: password-protected documents are not supported.",
        )

    text = (result.content or "").strip()
    if not text:
        # Most likely cause: image-only PDF and no OCR backend on the host.
        # Returning empty would feed the LLM 0 bytes and produce 0 facts —
        # the caller is better served by a clear 422.
        raise HTTPException(
            status_code=422,
            detail=(
                f"Extracted document has no text content (mime={mime}). "
                f"If this is a scanned/image PDF, OCR is required and is "
                f"not currently enabled."
            ),
        )
    return text


async def _fetch_url_text(url: str) -> str:
    """One deadline for the whole fetch, however the hops divide it up.

    The per-hop budgets do not compose into a bound worth having. Each hop
    pays its own DNS timeout plus the client's request timeout, and with
    ``MAX_INGEST_REDIRECTS`` that is 6 x (5 + 30) = 210s of wall clock a
    caller-supplied chain can spend, every second of it holding a request
    coroutine — a server answering each hop just under the timeout gets that
    for free.

    That ceiling is LOWER than what this code replaced, which is why it is
    being tightened rather than introduced: httpx's ``DEFAULT_MAX_REDIRECTS``
    is 20 and its timeout applies per hop, so ``follow_redirects=True`` allowed
    ~21 x 30s, plus two unbounded ``getaddrinfo`` calls ON the event loop.
    Lower is not the same as low enough: 210s still overruns the 120s request
    timeout this platform's services are deployed with, so the caller would
    see the proxy's 504 rather than the clean 400 this module otherwise
    promises.

    60s is chosen against the size cap, not picked round: at 100 KB/s — far
    below any real server — ``MAX_INGEST_CONTENT_BYTES`` (3 MB) transfers in
    30s, so this leaves 2x headroom for the body plus every redirect hop, and
    still lands well inside 120s.
    """
    try:
        async with asyncio.timeout(MAX_INGEST_FETCH_SECONDS):
            return await _walk_redirects_and_fetch(url)
    except TimeoutError:
        raise HTTPException(
            status_code=400,
            detail=f"URL fetch exceeded its {MAX_INGEST_FETCH_SECONDS}s budget: {url!r}",
        )


async def _walk_redirects_and_fetch(url: str) -> str:
    """Fetch URL, validate MIME + size, decode safely, and extract text.

    For ``TEXT_INGEST_MIME_TYPES`` the body is decoded with the response
    charset (or UTF-8 fallback) and HTML tags are stripped. For
    ``BINARY_INGEST_MIME_TYPES`` (PR #8) the body bytes are handed to
    Kreuzberg for format-specific extraction.

    Raises ``HTTPException`` for:
    - 400: invalid URL, DNS failure, hostname resolves to a blocked IP range,
           or the redirect chain exceeds ``MAX_INGEST_REDIRECTS``
    - 413: fetched body exceeds ``MAX_INGEST_CONTENT_BYTES``
    - 422: response Content-Type isn't in the allowlist, or Kreuzberg
           rejected the content (encrypted PDF, malformed file, empty
           text extraction, etc.)
    - 4xx/5xx: passed through from the upstream server

    M-43. The redirect chain is walked HERE, one hop at a time, because
    ``follow_redirects=True`` checks nothing on the way. httpx walks the whole
    chain inside ``client.stream`` — issuing a real GET to every hop — and only
    hands back the final response, so a check on ``resp.url`` afterwards runs
    long after the request to the private host was sent and answered. The
    previous version did exactly that, and its comment claimed the check
    "re-validates the FINAL host post-redirect (the upstream may have redirected
    us to a private host)". It did not prevent that request; it only prevented
    reading its body. ``raise_for_status()`` even ran first, so the internal
    host's status code surfaced to the caller too.

    That is a live SSRF: an authenticated tenant submits a URL they control
    which 302s to ``169.254.169.254`` or any RFC1918 address, and core-api
    issues the GET from inside the deployment network.

    Checking each hop before requesting it is the only ordering that helps,
    which is why the cap moved here as well — ``follow_redirects=False`` means
    httpx no longer enforces one.

    DNS rebinding, the gap this deliberately left open at first, is closed too:
    each hop CONNECTS to an address that was just vetted rather than resolving
    the name a second time. See :func:`_pin_url_to_address`.
    """
    # No keep-alive. httpcore pools connections by the request URL's origin and
    # reads ``sni_hostname`` only when it OPENS one, so once every hop is pinned
    # to an address, two hops with different hostnames on one IP look like one
    # origin — and the second would ride a TLS session verified for the first
    # one's name. Today nothing is pooled anyway, because a redirect response is
    # closed without its body being read and httpcore cannot resync a half-read
    # HTTP/1.1 connection. That is an accident of body handling, not a promise:
    # draining a redirect body, here or in some later change, restores reuse and
    # the hole with it. Measured — drained bodies reuse, this setting stops it.
    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=30.0,
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        current = url
        for _hop in range(MAX_INGEST_REDIRECTS + 1):
            # BEFORE the request, every time — including the first.
            # Non-empty or it raised — there is no path here that vets a name
            # and then connects to it by name anyway.
            addrs = await _resolve_and_vet_async(current)
            connect_url, host_header = _pin_url_to_address(current, addrs[0])
            request = client.build_request(
                "GET",
                connect_url,
                headers={"Host": host_header},
                extensions={"sni_hostname": urlparse(current).hostname or ""},
            )
            async with _stream_request(client, request) as resp:
                if resp.is_redirect and not resp.has_redirect_location:
                    # A 3xx with no Location. ``is_redirect`` is the STATUS CODE
                    # ALONE — httpx's own docstring says to use
                    # ``has_redirect_location`` when the header matters — so
                    # indexing ``headers["location"]`` behind an ``is_redirect``
                    # check raises KeyError on a response a hostile server can
                    # simply choose to send.
                    #
                    # 400 rather than falling through to the normal path: there
                    # ``raise_for_status()`` raises on 3xx as well, and
                    # ``upstream_http_error_handler`` re-raises anything under
                    # 500 into the catch-all, so falling through is another 500
                    # — which is what the pre-M-43 code did here too. Neither
                    # the old 500 nor a KeyError is right for a malformed
                    # upstream reply to a caller-supplied URL. Same 400 as the
                    # redirect cap, for the same reason.
                    raise HTTPException(
                        status_code=400,
                        detail=f"Upstream returned {resp.status_code} with no Location header for {current!r}",
                    )

                if resp.has_redirect_location:
                    # Location may be relative, so it is resolved against a
                    # base.
                    #
                    # That base is ``current`` — the LOGICAL url — and NOT
                    # ``resp.url``, which since pinning is the address-rewritten
                    # one. Joining "/next" against the pinned form would produce
                    # an address-based URL, dropping the hostname needed for the
                    # next hop's Host header, its SNI name and its own vetting.
                    current = str(httpx.URL(current).join(resp.headers["location"]))
                    continue

                resp.raise_for_status()

                # MIME allowlist on the final response, not the initial request.
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type and content_type not in ALLOWED_INGEST_MIME_TYPES:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Unsupported content type: {content_type}. "
                            f"Allowed: {sorted(ALLOWED_INGEST_MIME_TYPES)}"
                        ),
                    )

                # Pre-check Content-Length if the server bothered to send it.
                # Saves us from downloading anything when the server is honest.
                cl_header = resp.headers.get("content-length")
                if cl_header:
                    try:
                        if int(cl_header) > MAX_INGEST_CONTENT_BYTES:
                            raise HTTPException(
                                status_code=413,
                                detail=(
                                    f"Content too large: {cl_header} bytes (max {MAX_INGEST_CONTENT_BYTES})"
                                ),
                            )
                    except ValueError:
                        # Malformed Content-Length — fall through to streaming.
                        pass

                # Stream the body, abort if it exceeds the cap after
                # decompression. httpx transparently decompresses gzip/br
                # within ``aiter_bytes`` so this measures decompressed bytes
                # (gzip-bomb guard).
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_INGEST_CONTENT_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"Content too large: exceeded {MAX_INGEST_CONTENT_BYTES} bytes "
                                f"after decompression"
                            ),
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)

                # ---- PR #8: binary formats route through Kreuzberg ----
                if content_type in BINARY_INGEST_MIME_TYPES:
                    return await _extract_with_kreuzberg(body, content_type)

                # Decode using the response's declared charset, falling back
                # to UTF-8. httpx's default is ISO-8859-1 when no charset is
                # advertised, which mojibakes any UTF-8 page that omits a
                # charset declaration.
                encoding = resp.charset_encoding or "utf-8"

            # PR #9: shared decoder. HTML still gets the tag-strip path;
            # markdown / plain / csv preserve newlines. Outside the ``stream``
            # context so the connection is released before we decode.
            return decode_text_body(body, content_type, encoding)

        # Fell out of the loop: every iteration was a redirect. httpx used to
        # raise ``TooManyRedirects`` here; walking the chain by hand means this
        # module has to say so itself, and as a 400 rather than a 500 — a URL
        # that redirects forever is the caller's input, not our fault.
        raise HTTPException(
            status_code=400,
            detail=f"Too many redirects (max {MAX_INGEST_REDIRECTS}) starting from {url!r}",
        )


async def _find_prior_ingest_by_doc_hash(tenant_id: str, doc_hash: str) -> list[dict]:
    """A2 cache lookup. Returns memory rows from the most recent prior ingest of
    the same content for the same tenant — or empty list if no cache hit.

    A "prior ingest" means a non-deleted row whose metadata carries the same
    ``doc_hash`` value and was tagged as ``source="ingest"``. When multiple
    runs match, the storage endpoint returns the rows tagged with the
    most-recent ``run_id`` (the newest-run filter runs server-side).
    """
    return await get_storage_client().find_prior_ingest_by_doc_hash(tenant_id, doc_hash)


async def ingest_preview(request: IngestRequest) -> dict:
    """Preview mode: extract facts from URL or text without writing anything.

    Response fields:
      url             — echoed from the request (None when content was pasted)
      content_length  — length of the full input (no longer truncated post-PR#7)
      facts           — list of {content, suggested_type, source_uri[, salience]}
      chunk_ms        — total LLM time across all sections; 0 when short-circuited
      doc_hash        — sha256 of (tenant, content); caller echoes to commit for cache
      sections        — A4: number of Sections the chunker produced (= number of LLM
                        calls made). 0 when the parser yielded no usable content.
      skipped_reason  — only present when no LLM call happened
                        ("content_too_short" today; future reasons may surface)
      cached          — A2: present and True iff this content was previously
                        ingested by the same tenant. ``facts`` then come from
                        the prior run, ``run_id`` is set to the prior run's id,
                        and no LLM call was made.
      run_id          — only set when cached=True; the prior ingest_run_id.
    """
    tenant_config = await resolve_config(request.tenant_id)

    # Get content
    url = request.url
    if url:
        try:
            content = await _fetch_url_text(url)
        except HTTPException:
            # Preserve the specific 400/413/422 from _fetch_url_text — these
            # carry meaningful status codes the caller needs to see.
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")
    elif request.content:
        content = request.content
    else:
        raise HTTPException(status_code=400, detail="Either url or content is required")

    # ---- A2: doc-hash idempotency ----
    # Hash the full content (post-PR#7 there's no more truncate-to-50k cap
    # — the chunker handles arbitrarily large docs up to ``DOC_HARD_TOKEN_LIMIT``).
    # If a prior ingest of identical content already exists for this tenant,
    # return the cached facts straight from those memories — no LLM call.
    # Per-fact source_uri precedence:
    #   1. Caller-supplied ``request.source_uri`` — used by ``/ingest/file``
    #      to thread ``upload:<filename>`` through so the filename survives.
    #   2. URL the body was fetched from.
    #   3. ``"text-input"`` marker for pasted-content / no-source ingests.
    source_uri_default = request.source_uri or url or "text-input"
    doc_hash = _doc_hash(request.tenant_id, content)
    cached_memories = await _find_prior_ingest_by_doc_hash(request.tenant_id, doc_hash)
    if cached_memories:
        prior_run_id = cached_memories[0]["run_id"]
        cached_facts = []
        for m in cached_memories:
            md = m.get("metadata_") or {}
            fact: dict = {
                "content": m["content"],
                "suggested_type": m["memory_type"],
                "source_uri": m.get("source_uri") or source_uri_default,
            }
            if md.get("salience") is not None:
                fact["salience"] = md["salience"]
            cached_facts.append(fact)
        logger.info(
            "ingest_preview: doc-hash cache hit (tenant=%s prior_run=%s facts=%d)",
            request.tenant_id,
            prior_run_id,
            len(cached_facts),
        )
        return {
            "url": url,
            "content_length": len(content),
            "facts": cached_facts,
            "chunk_ms": 0,
            "cached": True,
            "run_id": prior_run_id,
            # The contract this response documents: the caller echoes
            # ``doc_hash`` to commit so the NEXT preview can hit this cache. The
            # cache-hit branch omitted it, so a client that followed the
            # documented flow lost the hash precisely when the cache was
            # working — the second ingest of a document could never cache.
            "doc_hash": doc_hash,
            # Zero LLM calls were made, for the same reason ``chunk_ms`` is 0.
            # Absent would read as "unknown"; 0 is the true count.
            "sections": 0,
        }

    # ---- P2.3: whitespace / too-short short-circuit ----
    # Avoid burning an LLM call on input that can't produce meaningful
    # facts. The cap is generous (20 chars after strip()) so any
    # legitimate ingest still hits the LLM.
    if len(content.strip()) < _INGEST_MIN_CONTENT_CHARS:
        logger.info(
            "ingest_preview: short-circuited (content too short: %d chars stripped)",
            len(content.strip()),
        )
        return {
            "url": url,
            "content_length": len(content),
            "facts": [],
            "chunk_ms": 0,
            "skipped_reason": "content_too_short",
        }

    # ---- A4 (PR #7): doc-level token refuse + structure-aware chunking ----
    # Reject pathologically large docs up front so the chunker doesn't
    # waste work. The boundary is a clean 413 with the token count.
    total_tokens = doc_token_count(content)
    if total_tokens > DOC_HARD_TOKEN_LIMIT:
        raise HTTPException(
            status_code=413,
            detail=(
                f"Document too large: {total_tokens} tokens "
                f"(max {DOC_HARD_TOKEN_LIMIT}). Split the doc and ingest in pieces."
            ),
        )

    # Parse → Block list → Section list. Format detection is heuristic
    # (looks for markdown headings / fenced code); fallback is plain-text.
    blocks = parse(content)
    sections = chunk_blocks(blocks)
    if not sections:
        # Defensive: parser produced nothing usable. Treat as a no-op
        # rather than crashing the request.
        logger.warning(
            "ingest_preview: chunker produced 0 sections from %d-char content; returning empty",
            len(content),
        )
        return {
            "url": url,
            "content_length": len(content),
            "facts": [],
            "chunk_ms": 0,
            "doc_hash": doc_hash,
            "sections": 0,
        }

    # Extract facts from every section in parallel. Each section gets
    # its own LLM call with its breadcrumb threaded into the prompt as
    # context. Bound concurrency to avoid hammering the LLM provider.
    t0 = time.perf_counter()
    sem = asyncio.Semaphore(_PREVIEW_CONCURRENCY)

    async def _extract_section(sec) -> list[dict]:
        async with sem:
            try:
                return await _chunk_content(
                    sec.text,
                    focus=request.focus,
                    tenant_config=tenant_config,
                    breadcrumb=sec.breadcrumb or None,
                )
            except Exception:
                # Per-section failure shouldn't tank the whole preview.
                # Log and return empty so other sections still contribute.
                logger.exception(
                    "ingest_preview: section extraction failed (breadcrumb=%r tokens=%d)",
                    sec.breadcrumb,
                    sec.token_count,
                )
                return []

    section_results = await asyncio.gather(*(_extract_section(s) for s in sections))
    facts: list[dict] = []
    for sec, sec_facts in zip(sections, section_results):
        # Stamp the section's breadcrumb into each fact's metadata-like
        # field so the commit path can persist provenance at the
        # section level later (Tier 2 follow-up may surface it on memory).
        for f in sec_facts:
            f.setdefault("source_uri", source_uri_default)
        facts.extend(sec_facts)
    chunk_ms = int((time.perf_counter() - t0) * 1000)

    logger.info(
        "ingest_preview: chunked %d sections (total %d tokens) into %d facts in %dms",
        len(sections),
        total_tokens,
        len(facts),
        chunk_ms,
    )

    return {
        "url": url,
        "content_length": len(content),
        "facts": facts,
        "chunk_ms": chunk_ms,
        "doc_hash": doc_hash,  # A2: caller echoes this to commit for future cache hits
        "sections": len(sections),  # A4: diagnostic — how many LLM calls did this run?
    }


def _summarize_batch_for_embedding(survivors: list, cap: int = _PARENT_DOC_SUMMARY_CAP) -> str | None:
    """Build a short, embeddable summary from the first few facts of an ingest batch.

    Returned string goes into the parent Document's ``data["summary"]`` field
    which the storage layer embeds for semantic search. Returns ``None`` when
    no useful summary can be assembled (no facts / all empty), in which case
    the caller MUST omit the field so the Document is stored without an
    embedding (per ``doc_indexing.resolve_embed_source`` contract: present
    summary keys must be non-empty strings).

    The current heuristic is "concat fact contents up to ``cap`` chars". Good
    enough for v1: gives semantic search a representative chunk without
    needing a per-batch LLM summarization call. Can be upgraded later.
    """
    parts: list[str] = []
    total = 0
    for f in survivors:
        text = (getattr(f, "content", None) or "").strip()
        if not text:
            continue
        parts.append(text)
        total += len(text) + 1  # +1 for joining space
        if total >= cap:
            break
    if not parts:
        return None
    summary = " ".join(parts).strip()
    if not summary:
        return None
    return summary[:cap]


async def _write_parent_ingest_document(
    *,
    request: IngestCommitRequest,
    run_id: str,
    survivors: list,
    created: int,
    errored: int,
    skipped: int,
    ingest_ms: int,
) -> None:
    """Upsert one row into ``documents (collection='ingest-sources')`` so each
    ingest batch has a queryable parent record. Each persisted memory joins
    back via the ``memories.run_id`` column. Best-effort — failures here MUST
    NOT roll back the memories the commit just wrote; we log and continue.

    The Document's ``data["summary"]`` is populated from the first ~500 chars
    of fact contents so the storage layer embeds it (free semantic search
    over uploaded files). Other fields are pure provenance metadata.

    Skipped entirely when ``created == 0`` — a batch that produced no new
    memories doesn't need a parent record (it was a full dedup hit; the
    prior batch's Document still describes the content).
    """
    if created <= 0:
        return

    # Resolve a source label: caller-supplied request.url (URL ingest, dashboard
    # back-compat) wins; else the first fact's source_uri (stamped by preview
    # — could be "https://...", "upload:report.pdf", or "text-input"); else
    # the fallback marker.
    source_label = request.url or (survivors[0].source_uri if survivors else None) or "text-input"
    filename: str | None = (
        source_label.removeprefix("upload:") if source_label.startswith("upload:") else None
    )

    data: dict = {
        "source_uri": source_label,
        "filename": filename,
        "url": request.url,
        "memory_count": created,
        "errored": errored,
        "skipped_duplicates": skipped,
        "doc_hash": request.doc_hash,
        "uploaded_at": datetime.now(UTC).isoformat(),
        "ingest_ms": ingest_ms,
        "agent_id": request.agent_id,
    }
    summary = _summarize_batch_for_embedding(survivors)
    if summary is not None:
        data["summary"] = summary  # triggers embedding population in storage

    payload: dict = {
        "tenant_id": request.tenant_id,
        "fleet_id": request.fleet_id,
        "collection": INGEST_DOCUMENTS_COLLECTION,
        "doc_id": run_id,
        "data": data,
    }
    try:
        sc = get_storage_client()
        await sc.upsert_document(payload)
        logger.info(
            "ingest_commit: parent Document written (run_id=%s collection=%s memory_count=%d)",
            run_id,
            INGEST_DOCUMENTS_COLLECTION,
            created,
        )
    except Exception:
        # Best-effort: the memories are already persisted; a missing parent
        # Document is a degraded but recoverable state, not a commit failure.
        # Future re-runs of the same content will hit doc-hash cache and
        # cleanup tooling can backfill the parent.
        logger.exception(
            "ingest_commit: parent Document write failed (run_id=%s) — "
            "memories are committed; parent record missing, run again to retry",
            run_id,
        )


async def ingest_commit(request: IngestCommitRequest) -> dict:
    """Commit mode: write previewed facts as memories.

    Three correctness/quality moves over the original loop:

    1. **Strong write_mode** (P1.3). Each ``MemoryCreate`` carries
       ``write_mode="strong"``, forcing the inline enrichment path so
       title/tags/weight are populated synchronously. Previously these
       went out via the fast path's deferred-enrichment queue, which
       isn't consumed in some deployments — leaving memories with
       ``title=null`` indefinitely.

    2. **Pre-loop content-hash dedup** (P1.4). Before any enrichment
       LLM call, batch-query existing content hashes for this tenant.
       Facts whose hash already exists short-circuit straight into
       ``skipped_duplicates``. Without this gate, every duplicate
       paid a full strong-mode LLM round-trip before being rejected
       with a 409 inside ``create_memory`` — pure waste on overlap-
       heavy batches (the common re-ingest case).

    3. **Bounded-parallel writes** (P1.3). Survivors go through
       ``create_memory`` concurrently with ``Semaphore(_COMMIT_CONCURRENCY)``
       Strong-mode runs a real OpenAI enrichment per fact (~2s); without
       parallelism, 10 facts is 20s+. ``tenant_config`` is pre-warmed
       once so the per-fact pipeline reuses the cache instead of racing
       on the shared session.
    """
    run_id = request.run_id or str(uuid.uuid4())
    # Caller-supplied url wins (dashboard back-compat). When the caller
    # round-trips preview output without re-passing url, each fact carries
    # its own source_uri (P1.2 — stamped by ingest_preview).
    request_url_override = request.url
    facts = list(request.facts)

    # ---- P1.E: validate suggested_type before any work ----
    # Without this, a forged/malformed suggested_type leaks all the way to
    # MemoryCreate and surfaces as a Pydantic ValidationError → 500. Catch
    # it here with a clean 422 listing the offending values.
    bad = [(i, f.suggested_type) for i, f in enumerate(facts) if f.suggested_type not in MEMORY_TYPES]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid suggested_type on facts {[i for i, _ in bad]}: "
                f"{[t for _, t in bad]}. Allowed: {sorted(MEMORY_TYPES)}"
            ),
        )

    t0 = time.perf_counter()

    # Pre-warm the tenant-config cache so every per-fact pipeline below hits
    # the in-process TTLCache instead of each issuing its own storage fetch
    # when the concurrent writes fan out.
    await resolve_config(request.tenant_id)

    # ----- P1.4: pre-loop dedup -----
    # Compute the same content-hash the write pipeline uses for its 409
    # gate. Then batch-query for which hashes already exist. Hits get
    # filtered out here so they never reach enrichment.
    hashes = [_content_hash(request.tenant_id, request.fleet_id, fact.content) for fact in facts]
    pre_dedup_skipped = 0
    if hashes:
        try:
            sc = get_storage_client()
            # Stage 5: scope dedup to (tenant, fleet, agent) so an ingest
            # run by one agent doesn't silently dedup against another agent's
            # identical content. Falls back to (tenant, fleet) when agent_id
            # is omitted.
            existing = await sc.bulk_find_by_content_hashes(
                request.tenant_id,
                hashes,
                fleet_id=request.fleet_id,
                agent_id=request.agent_id,
            )
        except Exception:
            # Fail-open: if the dedup query fails, fall through to the
            # per-fact path. ``create_memory`` still 409s exact dups, so
            # correctness is unchanged — we just lose the cost optimization.
            logger.warning(
                "ingest_commit: bulk dedup query failed; falling through to per-fact", exc_info=True
            )
            existing = {}
    else:
        existing = {}

    survivors: list = []
    for fact, h in zip(facts, hashes):
        if h in existing:
            pre_dedup_skipped += 1
        else:
            survivors.append(fact)

    if pre_dedup_skipped:
        logger.info(
            "ingest_commit: pre-loop dedup eliminated %d/%d facts before enrichment",
            pre_dedup_skipped,
            len(facts),
        )

    # ----- Bulk write -----
    # Survivors are committed via ``create_memories_bulk`` so the per-fact
    # OpenAI embed/enrich round-trips share a single batched provider
    # call: 1 ``get_embeddings_batch`` for the whole batch, semaphored
    # enrichment with the same concurrency budget as the bulk endpoint,
    # and a single bulk storage insert. Pre-fix this fanned out N parallel
    # ``create_memory`` calls (each owning its own embed+enrich+dedup
    # round-trip) capped at Semaphore(4); wet-tested at 7.93s for 4 facts
    # and 8.87s for 8 facts. Audit finding #28.
    #
    # Idempotency token: ``run_id`` is the natural per-attempt key here —
    # ``ingest_commit`` is invoked once per run, and a client retry that
    # reuses the same ``run_id`` should see ``duplicate_attempt`` on
    # already-committed rows from the previous attempt (the same contract
    # the dashboard's bulk-write path already relies on).
    #
    # This is only safe because ``create_memories_bulk`` keys each item on
    # its CONTENT (H-08). The pre-loop dedup above removes facts that a
    # previous attempt already committed, so the body a retry sends is
    # SHORTER than the first attempt's while carrying the same ``run_id``.
    # Under the old positional keys every survivor slid down onto an index
    # an earlier fact's row already owned, the insert was skipped as a
    # conflict, and the response reported ``duplicate_attempt`` against a
    # row holding entirely different content — a fully successful-looking
    # commit that persisted nothing. The pre-dedup is what makes retries
    # cheap and it is also what shifts the indices; keying on content is
    # what lets both be true at once.
    #
    # Behavior tradeoff: ``create_memories_bulk`` performs content-hash
    # dedup but skips the per-write semantic-duplicate check that
    # individual ``create_memory`` calls run via ``CheckSemanticDuplicate``.
    # The preview step has already shown the user the candidate facts, so
    # a semantic-dup "safety net" at commit catches a tight race window
    # only — the latency win outweighs the loss. Verbatim re-ingest of
    # the same content is still caught by the content-hash dedup at the
    # top of this function and inside ``create_memories_bulk``.
    if survivors:
        bulk_items: list[BulkMemoryItem] = []
        for fact in survivors:
            # P1.2 provenance precedence preserved verbatim — caller-
            # supplied request URL wins, else the fact's own source_uri
            # from preview, else "text-input".
            effective_source = request_url_override or fact.source_uri or "text-input"
            # CAURA-703 used to be stamped here as
            # ``metadata["memory_type_agent_set"] = False``. It moved to the
            # ``memory_type_is_agent_set`` argument on the bulk call below,
            # because C25 sanitation now strips PLATFORM_ONLY_KEYS from item
            # metadata — this dict is caller-adjacent, and a platform flag put
            # in it is indistinguishable from a forged one.
            metadata: dict = {
                "source": "ingest",
                "ingest_url": request_url_override or fact.source_uri or None,
            }
            if request.doc_hash:
                metadata["doc_hash"] = request.doc_hash
            salience_value = getattr(fact, "salience", None)
            if salience_value is not None:
                metadata["salience"] = salience_value
            bulk_items.append(
                BulkMemoryItem(
                    memory_type=fact.suggested_type,
                    content=fact.content,
                    source_uri=effective_source,
                    run_id=run_id,
                    metadata=metadata,
                )
            )
        # H-07: chunked, because ``BulkMemoryCreate.items`` carries
        # ``max_length=BULK_MAX_ITEMS`` (100) and nothing upstream caps the
        # fact count — not ``IngestCommitRequest.facts``, not preview. Preview
        # accepts documents up to 100k tokens, sections them at ~2k tokens, and
        # asks the extractor for 5-20 facts per section, so a ~40k-token
        # document routinely clears 100 facts.
        #
        # Building one oversized model raised ``pydantic.ValidationError`` —
        # and it was constructed OUTSIDE the try below, which catches only
        # ``HTTPException`` anyway. core-api registers no handler for the raw
        # pydantic error (``RequestValidationError`` is FastAPI's request-body
        # wrapper and a hand-built model does not raise it), so it reached the
        # global handler as an opaque 500. Every fact was lost, the retry
        # re-extracted the same over-100 set and 500'd again, and the user's
        # only recourse was a smaller document.
        #
        # Batches share ONE ``bulk_attempt_id`` — ``run_id``, unchanged. That
        # is only correct because H-08 made the per-item key content-derived:
        # batch boundaries no longer enter the key at all. Per-batch attempt
        # ids (``f"{run_id}:batch{n}"``) would actively hurt, because the
        # pre-dedup above shrinks the survivor list between attempts, so a
        # retry re-cuts the boundaries and the same fact lands in a different
        # batch — computing a different key and losing the
        # ``duplicate_attempt`` resolution that reusing ``run_id`` exists for.
        #
        # Sequential, not gathered: each batch already fans out its own
        # embed/enrich internally and takes a per-tenant storage slot, so
        # concurrency here would multiply pressure on the same bulkhead. It
        # also keeps the abort-on-failure semantics below honest.
        created = 0
        skipped_in_loop = 0
        errored = 0
        for batch_start in range(0, len(bulk_items), BULK_MAX_ITEMS):
            batch = bulk_items[batch_start : batch_start + BULK_MAX_ITEMS]
            bulk_data = BulkMemoryCreate(
                tenant_id=request.tenant_id,
                fleet_id=request.fleet_id,
                agent_id=request.agent_id,
                items=batch,
            )
            try:
                bulk_response = await create_memories_bulk(
                    bulk_data,
                    bulk_attempt_id=run_id,
                    # CAURA-703: every item's type came from the extraction
                    # LLM's ``suggested_type``, never from the calling agent.
                    memory_type_is_agent_set=False,
                )
                created += bulk_response.created
                skipped_in_loop += bulk_response.duplicates
                errored += bulk_response.errors
                # Surface per-item error reasons in the logs so the cleanup
                # message at the bottom of this function still points at the
                # offending facts.
                for item in bulk_response.results:
                    if item.status == "error":
                        # Mirror the legacy "fact[N]" log format the
                        # P1.C-lite runbook + operator greps depend on.
                        # ``item.index`` is batch-relative, so it is offset back
                        # into the survivor list the format has always counted
                        # in — otherwise every batch would restart at fact[0]
                        # and point an operator at the wrong fact.
                        logger.warning(
                            "ingest_commit: fact[%d] write failed (run_id=%s): %s",
                            batch_start + item.index,
                            run_id,
                            item.error,
                        )
            except HTTPException as e:
                # A 4xx/5xx from the bulk endpoint aborts this batch (e.g. 504
                # from the bulk-embedding timeout). Stop rather than carry on:
                # these failures are overwhelmingly systemic (storage down,
                # budget burned), so the remaining batches would queue behind
                # the same wall and turn one failure into N.
                #
                # Counts accumulated by EARLIER batches are kept. This used to
                # log "0 facts persisted on this attempt" and zero them, which
                # was true when there was only ever one batch and would now be
                # a false statement to an operator deciding whether to clean
                # up. Everything from this batch onward is reported errored.
                unattempted = len(bulk_items) - batch_start
                logger.exception(
                    "ingest_commit: bulk write failed with HTTP %d (run_id=%s) on the "
                    "batch starting at fact[%d] — %d fact(s) persisted before it, "
                    "%d not attempted; safe to retry, which re-sends every fact and "
                    "resolves the committed ones as duplicates",
                    e.status_code,
                    run_id,
                    batch_start,
                    created,
                    unattempted,
                )
                errored += unattempted
                break
    else:
        # All facts pre-deduped by the content-hash sweep above; nothing
        # to do here.
        created = 0
        skipped_in_loop = 0
        errored = 0

    skipped = pre_dedup_skipped + skipped_in_loop
    ingest_ms = int((time.perf_counter() - t0) * 1000)

    if errored:
        logger.warning(
            "ingest_commit: run_id=%s had %d errored fact(s) — "
            "find them in the logs above, or DELETE FROM memories WHERE "
            "ingest_run_id='%s' to wipe partial batch",
            run_id,
            errored,
            run_id,
        )

    logger.info(
        "ingest_commit: run_id=%s facts=%d created=%d skipped=%d errored=%d (pre_dedup=%d, 409=%d) in %dms",
        run_id,
        len(facts),
        created,
        skipped,
        errored,
        pre_dedup_skipped,
        skipped_in_loop,
        ingest_ms,
    )

    # Write the parent Document so this batch shows up in the dashboard
    # Documents tab and is queryable as a unit. Best-effort — won't unwind
    # the memories if it fails. Skipped when ``created == 0`` (all dedup'd).
    await _write_parent_ingest_document(
        request=request,
        run_id=run_id,
        survivors=survivors,
        created=created,
        errored=errored,
        skipped=skipped,
        ingest_ms=ingest_ms,
    )

    return {
        "url": request.url,
        "facts_extracted": len(facts),
        "memories_created": created,
        "skipped_duplicates": skipped,
        "errored": errored,
        "run_id": run_id,
        "ingest_ms": ingest_ms,
    }
