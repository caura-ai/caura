"""Document/URL ingestion: extract atomic facts via LLM, preview, and commit as memories."""

import asyncio
import ipaddress
import logging
import re
import socket
import time
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.clients.storage_client import get_storage_client
from core_api.config import settings
from core_api.constants import MEMORY_TYPES
from core_api.providers._retry import call_with_fallback
from core_api.schemas import IngestCommitRequest, IngestRequest, MemoryCreate
from core_api.services.memory_service import _content_hash, create_memory
from core_api.services.organization_settings import resolve_config

logger = logging.getLogger(__name__)

# Allowed MIME types for URL ingest. Binary formats (PDF, DOCX, etc.) are
# rejected here; the optional Kreuzberg integration (PR #8) will add a
# separate path for them.
ALLOWED_INGEST_MIME_TYPES = frozenset(
    {
        "text/html",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "application/xhtml+xml",
    }
)

# Hard cap on fetched-body size (post-decompression). Defends against
# gzip-bomb URLs that claim Content-Length: 50KB but expand to gigabytes.
MAX_INGEST_CONTENT_BYTES = 200_000

# Explicit deny-list for cloud-metadata service IPs that aren't always
# caught by ipaddress.is_link_local (AWS 169.254.169.254 IS link-local;
# GCP metadata at metadata.google.internal resolves to 169.254.169.254 too;
# Azure uses the same IP). Listed defensively even though is_link_local
# covers them.
_CLOUD_METADATA_IPS = frozenset({"169.254.169.254", "fd00:ec2::254"})

# Max concurrent ``create_memory`` calls during commit. Strong-mode write
# runs sync enrichment per fact (a real LLM round-trip), so without
# parallelism a 10-fact batch is ~20s+. With Semaphore(4) it's ~5s.
# Bounded to avoid hammering the LLM provider with rate-limit failures.
_COMMIT_CONCURRENCY = 4

CHUNKING_PROMPT = """\
Extract discrete, atomic facts from the following content.
Each fact should be a single claim that can stand alone as a memory.

Guidelines:
- Extract 5-20 facts depending on content length
- Be specific: include names, numbers, dates, decisions
- Each fact: one claim, not a paragraph
- Suggest a memory_type for each: fact, decision, preference, task, plan, episode, semantic, intention, commitment, action, outcome, cancellation
{focus_instruction}

Content:
{content}

Return ONLY valid JSON object with a "facts" key containing an array:
{{"facts": [{{"content": "...", "suggested_type": "fact"}}, ...]}}
"""


def _fake_ingest() -> list:
    """No-LLM fallback: return empty list so validation yields 0 facts."""
    logger.warning("ingest: no LLM credentials — fact extraction skipped, returning 0 facts")
    return []


async def _chunk_content(
    text: str,
    focus: str | None = None,
    tenant_config=None,
) -> list[dict]:
    """Extract atomic facts from text via LLM."""
    provider_name = (
        tenant_config.enrichment_provider if tenant_config else None
    ) or settings.entity_extraction_provider

    focus_instruction = ""
    if focus:
        focus_instruction = f"Focus on facts relevant to {focus}. Deprioritize unrelated details."

    # Truncate very long content to avoid token limits
    content = text[:50_000]
    prompt = CHUNKING_PROMPT.format(content=content, focus_instruction=focus_instruction)

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
    facts = []
    if isinstance(raw, dict):
        # Handle {"facts": [...]} wrapper
        for v in raw.values():
            if isinstance(v, list):
                raw = v
                break
    for item in raw:
        if not isinstance(item, dict) or not item.get("content"):
            continue
        st = item.get("suggested_type", "fact")
        if st not in MEMORY_TYPES:
            st = "fact"
        facts.append({"content": str(item["content"]).strip(), "suggested_type": st})

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


def _check_hostname_safe(url: str) -> None:
    """Resolve the URL's hostname and reject if it points at private infra.

    Light-weight SSRF defense. Does NOT handle DNS rebinding between this
    resolution and the actual TCP connect — that's a Tier 3 hardening item.
    Covers the accidental-misuse case (localhost, RFC1918, cloud metadata).
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise HTTPException(status_code=400, detail=f"Invalid URL: no hostname in {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise HTTPException(status_code=400, detail=f"DNS resolution failed for {host}: {e}")
    for family, _, _, _, sockaddr in infos:
        addr = str(sockaddr[0])
        if _is_blocked_ip(addr) or addr in _CLOUD_METADATA_IPS:
            raise HTTPException(
                status_code=400,
                detail=f"Blocked: {host} resolves to {addr} (private/loopback/link-local/metadata)",
            )


async def _fetch_url_text(url: str) -> str:
    """Fetch URL, validate MIME + size, decode safely, and strip HTML.

    Raises ``HTTPException`` for:
    - 400: invalid URL, DNS failure, hostname resolves to a blocked IP range
    - 413: fetched body exceeds ``MAX_INGEST_CONTENT_BYTES``
    - 422: response Content-Type isn't in the text allowlist
    - 4xx/5xx: passed through from the upstream server
    """
    _check_hostname_safe(url)

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()

            # Re-validate the FINAL host post-redirect (the upstream may
            # have redirected us to a private host). httpx exposes the
            # ultimate URL via resp.url; ``follow_redirects=True`` already
            # walked the chain.
            _check_hostname_safe(str(resp.url))

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
                            detail=(f"Content too large: {cl_header} bytes (max {MAX_INGEST_CONTENT_BYTES})"),
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

            # Decode using the response's declared charset, falling back
            # to UTF-8. httpx's default is ISO-8859-1 when no charset is
            # advertised, which mojibakes any UTF-8 page that omits a
            # charset declaration.
            encoding = resp.charset_encoding or "utf-8"
            html = body.decode(encoding, errors="replace")

    # Strip HTML tags to get plain text. (BeautifulSoup-based extraction
    # ships in a later PR; this regex is the same as before.)
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    return text


async def ingest_preview(db: AsyncSession, request: IngestRequest) -> dict:
    """Preview mode: extract facts from URL or text without writing anything."""
    from core_api.services.organization_settings import resolve_config

    tenant_config = await resolve_config(db, request.tenant_id)

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

    # Extract facts via LLM
    t0 = time.perf_counter()
    try:
        facts = await _chunk_content(content, request.focus, tenant_config)
    except Exception as e:
        logger.exception("Ingest chunking failed")
        raise HTTPException(status_code=500, detail=f"Fact extraction failed: {e}")
    chunk_ms = int((time.perf_counter() - t0) * 1000)

    return {
        "url": url,
        "content_length": len(content),
        "facts": facts,
        "chunk_ms": chunk_ms,
    }


async def ingest_commit(db: AsyncSession, request: IngestCommitRequest) -> dict:
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
    source_uri = request.url or "text-input"
    facts = list(request.facts)

    t0 = time.perf_counter()

    # Pre-warm the tenant-config cache. The first cached lookup is the
    # only one that may touch ``db``; afterwards every per-fact pipeline
    # hits the in-process TTLCache. Avoids racing on the shared session
    # when the concurrent writes fan out below.
    await resolve_config(db, request.tenant_id)

    # ----- P1.4: pre-loop dedup -----
    # Compute the same content-hash the write pipeline uses for its 409
    # gate. Then batch-query for which hashes already exist. Hits get
    # filtered out here so they never reach enrichment.
    hashes = [_content_hash(request.tenant_id, request.fleet_id, fact.content) for fact in facts]
    pre_dedup_skipped = 0
    if hashes:
        try:
            sc = get_storage_client()
            existing = await sc.bulk_find_by_content_hashes(request.tenant_id, hashes)
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

    # ----- P1.3: parallel strong-mode writes -----
    sem = asyncio.Semaphore(_COMMIT_CONCURRENCY)

    async def _write_one(fact) -> int:
        """Return 1 on create, 0 on 409, raise on other failures.

        Errors from ``create_memory`` that aren't 409 propagate out of
        ``asyncio.gather`` and abort the batch. Tier-1 P1.C-lite (next
        PR) softens that to per-fact warn-and-continue.
        """
        mem_data = MemoryCreate(
            tenant_id=request.tenant_id,
            fleet_id=request.fleet_id,
            agent_id=request.agent_id,
            memory_type=fact.suggested_type,
            content=fact.content,
            source_uri=source_uri,
            run_id=run_id,
            write_mode="strong",
            metadata={
                "source": "ingest",
                "ingest_run_id": run_id,
                "ingest_url": request.url or None,
            },
        )
        async with sem:
            try:
                await create_memory(db, mem_data)
                return 1
            except HTTPException as e:
                if e.status_code == 409:
                    return 0
                raise

    results = await asyncio.gather(*(_write_one(f) for f in survivors))
    created = sum(results)
    skipped_in_loop = len(survivors) - created
    skipped = pre_dedup_skipped + skipped_in_loop
    ingest_ms = int((time.perf_counter() - t0) * 1000)

    logger.info(
        "ingest_commit: run_id=%s facts=%d created=%d skipped=%d (pre_dedup=%d, 409=%d) in %dms",
        run_id,
        len(facts),
        created,
        skipped,
        pre_dedup_skipped,
        skipped_in_loop,
        ingest_ms,
    )

    return {
        "url": request.url,
        "facts_extracted": len(facts),
        "memories_created": created,
        "skipped_duplicates": skipped,
        "run_id": run_id,
        "ingest_ms": ingest_ms,
    }
