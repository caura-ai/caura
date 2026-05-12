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

# Maximum content length the LLM sees. Inputs longer than this get
# truncated; ``ingest_preview`` reports the post-truncate length as
# ``content_length`` and sets ``truncated: true`` + ``original_length``
# so callers know the input was clipped. (Previously ``content_length``
# returned the pre-truncate length, lying about what the LLM actually
# processed.)
_INGEST_MAX_CONTENT_CHARS = 50_000

# Minimum content length before we'll even call the LLM. Whitespace-only
# inputs and trivially short ones ("hi") used to burn a real LLM call
# producing useless meta-facts ("The content begins with the greeting
# 'hi'"). We short-circuit instead and return ``skipped_reason``.
_INGEST_MIN_CONTENT_CHARS = 20

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
) -> list[dict]:
    """Extract atomic facts from text via LLM.

    Caller is responsible for truncation before calling — see
    ``_INGEST_MAX_CONTENT_CHARS`` and ``ingest_preview``. This function
    just builds the prompt, calls the LLM, validates the JSON shape,
    and drops meta-facts (P2.4).
    """
    provider_name = (
        tenant_config.enrichment_provider if tenant_config else None
    ) or settings.entity_extraction_provider

    focus_instruction = ""
    if focus:
        focus_instruction = f"Focus on facts relevant to {focus}. Deprioritize unrelated details."

    prompt = CHUNKING_PROMPT.format(content=text, focus_instruction=focus_instruction)

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
        if len(body.split()) < _MIN_FACT_WORDS:
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
    """Preview mode: extract facts from URL or text without writing anything.

    Response fields:
      url             — echoed from the request (None when content was pasted)
      content_length  — length of the string the LLM actually saw (post-truncate, P2.1)
      truncated       — True iff input exceeded _INGEST_MAX_CONTENT_CHARS (P2.1)
      original_length — pre-truncate length, only present when truncated=True (P2.1)
      facts           — list of {content, suggested_type, source_uri}
      chunk_ms        — LLM round-trip duration; 0 when short-circuited
      skipped_reason  — only present when no LLM call happened
                        ("content_too_short" today; future reasons may surface)
    """
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

    # ---- P2.1: honest truncation reporting ----
    original_length = len(content)
    truncated = original_length > _INGEST_MAX_CONTENT_CHARS
    if truncated:
        content = content[:_INGEST_MAX_CONTENT_CHARS]

    # ---- P2.3: whitespace / too-short short-circuit ----
    # Avoid burning an LLM call on input that can't produce meaningful
    # facts. The cap is generous (20 chars after strip()) so any
    # legitimate ingest still hits the LLM.
    source_uri_default = url or "text-input"
    if len(content.strip()) < _INGEST_MIN_CONTENT_CHARS:
        logger.info(
            "ingest_preview: short-circuited (content too short: %d chars stripped)",
            len(content.strip()),
        )
        response: dict = {
            "url": url,
            "content_length": len(content),
            "facts": [],
            "chunk_ms": 0,
            "skipped_reason": "content_too_short",
        }
        if truncated:
            response["truncated"] = True
            response["original_length"] = original_length
        return response

    # Extract facts via LLM
    t0 = time.perf_counter()
    try:
        facts = await _chunk_content(content, request.focus, tenant_config)
    except Exception as e:
        logger.exception("Ingest chunking failed")
        raise HTTPException(status_code=500, detail=f"Fact extraction failed: {e}")
    chunk_ms = int((time.perf_counter() - t0) * 1000)

    # P1.2: stamp source_uri on every fact so the commit path doesn't need
    # the caller to re-pass ``url``. Callers can still override per-fact.
    for f in facts:
        f.setdefault("source_uri", source_uri_default)

    response = {
        "url": url,
        "content_length": len(content),
        "facts": facts,
        "chunk_ms": chunk_ms,
    }
    if truncated:
        response["truncated"] = True
        response["original_length"] = original_length
    return response


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
    # ----- P1.C-lite: warn-and-continue on per-fact failure -----
    # Outcomes returned by ``_write_one`` and aggregated after gather.
    # Encoded as ints because gather's collection ordering doesn't matter
    # here — we only need totals.
    _OUTCOME_CREATED = 1
    _OUTCOME_DUPLICATE = 0
    _OUTCOME_ERRORED = -1

    sem = asyncio.Semaphore(_COMMIT_CONCURRENCY)

    async def _write_one(idx: int, fact) -> int:
        """Always returns; never raises.

        Returns one of: ``_OUTCOME_CREATED`` (created+1),
        ``_OUTCOME_DUPLICATE`` (409 from create_memory, skipped+1),
        ``_OUTCOME_ERRORED`` (any other failure — logged with run_id +
        fact index for manual cleanup, errored+1).

        P1.C-lite: pre-PR, any non-409 exception escaped out of gather
        and aborted the whole batch, leaving 0..N-1 memories already
        persisted under the run_id with no per-fact telemetry. Now each
        fact's outcome is captured independently; the run_id stamps
        whatever did land so it can be cleaned up via bulk-delete or
        the upcoming POST /ingest/undo/{run_id} (PR #6).
        """
        # P1.2: provenance precedence — caller-supplied request.url wins
        # (dashboard back-compat), else use the fact's own source_uri
        # (stamped by preview), else fall back to "text-input".
        effective_source = request_url_override or fact.source_uri or "text-input"
        mem_data = MemoryCreate(
            tenant_id=request.tenant_id,
            fleet_id=request.fleet_id,
            agent_id=request.agent_id,
            memory_type=fact.suggested_type,
            content=fact.content,
            source_uri=effective_source,
            run_id=run_id,
            write_mode="strong",
            metadata={
                "source": "ingest",
                "ingest_run_id": run_id,
                "ingest_url": request_url_override or fact.source_uri or None,
            },
        )
        async with sem:
            try:
                await create_memory(db, mem_data)
                return _OUTCOME_CREATED
            except HTTPException as e:
                if e.status_code == 409:
                    return _OUTCOME_DUPLICATE
                logger.exception(
                    "ingest_commit: fact[%d] write failed with HTTP %d "
                    "(run_id=%s) — tagged for manual cleanup",
                    idx,
                    e.status_code,
                    run_id,
                )
                return _OUTCOME_ERRORED
            except Exception:
                logger.exception(
                    "ingest_commit: fact[%d] write raised (run_id=%s) — tagged for manual cleanup",
                    idx,
                    run_id,
                )
                return _OUTCOME_ERRORED

    results = await asyncio.gather(*(_write_one(i, f) for i, f in enumerate(survivors)))
    created = sum(1 for r in results if r == _OUTCOME_CREATED)
    skipped_in_loop = sum(1 for r in results if r == _OUTCOME_DUPLICATE)
    errored = sum(1 for r in results if r == _OUTCOME_ERRORED)
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

    return {
        "url": request.url,
        "facts_extracted": len(facts),
        "memories_created": created,
        "skipped_duplicates": skipped,
        "errored": errored,
        "run_id": run_id,
        "ingest_ms": ingest_ms,
    }
