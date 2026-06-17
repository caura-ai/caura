"""Fast business-vs-personal classifier for the ingestion pre-gate.

A minimal, cheap LLM call answering a single question — is this memory
business-relevant or personal? — used by ``BusinessPersonalPregate`` to reject
personal content *before* the expensive enrichment / embedding / entity
extraction run. It takes its own provider/model (resolved by the caller), so the
signal is independent of the enrichment provider and survives
``enrichment_provider=none`` (e.g. CI).

Never raises: on a ``none`` provider or any LLM failure it returns a fail-open
"business" verdict, so the pre-gate can never block a write merely because the
classifier was unavailable. (The accurate post-enrichment ``GovernanceDecision``
remains the backstop.)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from common.llm.constants import PREGATE_CLASSIFIER_TIMEOUT_SECONDS
from common.llm.protocols import LLMProvider
from common.llm.retry import call_with_fallback
from common.provider_names import ProviderName

logger = logging.getLogger(__name__)

# Kept deliberately short and structured: one classification, low token cost.
_PROMPT = """Classify whether the following content is BUSINESS-relevant or PERSONAL.

BUSINESS: work, projects, customers, code, decisions, operations — anything an
organization would keep in shared memory.
PERSONAL: private individual matters unrelated to work (health, family, personal
opinions or plans).

Return ONLY JSON: {{"business_relevance": "business" | "personal", "confidence": 0.0-1.0}}

Content:
{content}"""


@dataclass(frozen=True)
class BusinessClassification:
    business_relevance: str  # "business" | "personal"
    confidence: float  # 0.0 to 1.0
    llm_ms: int  # 0 when no live model ran (fail-open)
    # The provider/model that ACTUALLY produced this verdict, captured from the
    # live ``LLMProvider`` instance. ``call_with_fallback`` may run the primary
    # OR the tenant's fallback, so recording the intended provider/model would
    # mis-attribute the decision in the audit log when a fallback hop occurred.
    # ``None`` on the fail-open paths (no live model ran).
    provider: str | None = None
    model: str | None = None


def _fail_open() -> BusinessClassification:
    """Safe default: treat as business so the pre-gate never blocks on failure."""
    return BusinessClassification(business_relevance="business", confidence=0.0, llm_ms=0)


def _build_prompt(content: str) -> str:
    """Build the classifier prompt. ``str.format`` only parses the TEMPLATE for
    fields — the substituted ``content`` value is inserted literally — so the
    content must NOT be brace-escaped (escaping would corrupt JSON / code / any
    braces in it). The template's own JSON example is escaped with ``{{ }}``."""
    return _PROMPT.format(content=content)


async def classify_business_personal(
    content: str,
    tenant_config: object | None = None,
    *,
    provider: str | None,
    model: str | None,
) -> BusinessClassification:
    """Classify *content* as business/personal via a fast LLM call.

    ``provider``/``model`` are the pre-gate's own settings (the caller resolves
    a fallback). A ``none`` provider short-circuits to the fail-open default.
    """
    provider_name = provider or ProviderName.OPENAI
    if provider_name == ProviderName.NONE:
        return _fail_open()

    async def _do(llm: LLMProvider) -> BusinessClassification:
        prompt = _build_prompt(content)
        t0 = time.perf_counter()
        raw = await llm.complete_json(prompt)
        llm_ms = int((time.perf_counter() - t0) * 1000)
        rel = raw.get("business_relevance")
        rel = rel if rel in ("business", "personal") else "business"
        try:
            conf = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return BusinessClassification(
            business_relevance=rel,
            confidence=min(1.0, max(0.0, conf)),
            llm_ms=llm_ms,
            # The provider actually used (primary or fallback) — see the field docs.
            provider=llm.provider_name,
            model=llm.model,
        )

    # Hard ceiling across the whole chain (retries + fallback). The pre-gate is
    # fail-open and inline on the write path, so a slow/unreachable provider must
    # never stall a write: on timeout we fail open and surface an alertable
    # signal (a sustained outage silently disables the gate otherwise).
    # ``call_with_fallback``'s own ``timeout=`` is applied PER ATTEMPT, so it
    # can't bound the whole chain (N retries per provider) — hence the outer
    # ``wait_for``. Don't "simplify" this to the param: the semantics differ.
    try:
        return await asyncio.wait_for(
            call_with_fallback(
                primary_provider_name=provider_name,
                call_fn=_do,
                fake_fn=_fail_open,
                tenant_config=tenant_config,
                service_label="business_pregate",
                model_override=model,
            ),
            timeout=PREGATE_CLASSIFIER_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        # asyncio.wait_for raises asyncio.TimeoutError, which IS the builtin
        # TimeoutError on 3.11+ (this project is 3.12+) — and ruff UP041 enforces
        # the builtin spelling, so don't "fix" this to asyncio.TimeoutError. Fail
        # open on it so a slow/unreachable provider never escapes as a 500.
        logger.warning(
            "business_pregate: classifier timed out after %ss; failing open (provider=%s, model=%s)",
            PREGATE_CLASSIFIER_TIMEOUT_SECONDS,
            provider_name,
            model,
        )
        return _fail_open()
