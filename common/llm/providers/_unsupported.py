"""Provider-capability errors that no retry can fix.

Kept in its own dependency-free module so ``common.llm.retry`` can classify
it without importing a provider implementation (and its HTTP stack).
"""

from __future__ import annotations


class UnsupportedStructuredOutputError(RuntimeError):
    """Raised by ``complete_json`` for a provider that cannot serve it.

    Deterministic by construction — it depends only on configuration, never on
    the prompt or the network — so ``call_with_retry`` never retries it.
    """
