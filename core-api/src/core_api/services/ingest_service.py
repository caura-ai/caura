"""Document/URL ingestion: extract atomic facts via LLM, preview, and commit as memories."""

import logging
import re
import time
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from core_api.config import settings
from core_api.constants import MEMORY_TYPES
from core_api.providers._retry import call_with_fallback
from core_api.schemas import IngestCommitRequest, IngestRequest, MemoryCreate
from core_api.services.memory_service import create_memory

logger = logging.getLogger(__name__)

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


async def _fetch_url_text(url: str) -> str:
    """Fetch URL and strip HTML to plain text."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    html = resp.text

    # Strip HTML tags to get plain text
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
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
    """Commit mode: write previewed facts as memories."""
    run_id = request.run_id or str(uuid.uuid4())
    source_uri = request.url or "text-input"

    created = 0
    skipped = 0

    t0 = time.perf_counter()
    for fact in request.facts:
        mem_data = MemoryCreate(
            tenant_id=request.tenant_id,
            fleet_id=request.fleet_id,
            agent_id=request.agent_id,
            memory_type=fact.suggested_type,
            content=fact.content,
            source_uri=source_uri,
            run_id=run_id,
            metadata={
                "source": "ingest",
                "ingest_run_id": run_id,
                "ingest_url": request.url or None,
            },
        )
        try:
            await create_memory(db, mem_data)
            created += 1
        except HTTPException as e:
            if e.status_code == 409:
                skipped += 1
            else:
                raise
    ingest_ms = int((time.perf_counter() - t0) * 1000)

    return {
        "url": request.url,
        "facts_extracted": len(request.facts),
        "memories_created": created,
        "skipped_duplicates": skipped,
        "run_id": run_id,
        "ingest_ms": ingest_ms,
    }
