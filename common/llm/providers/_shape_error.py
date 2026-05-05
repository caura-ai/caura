"""CAURA-651: shared base for ``*ResponseShapeError`` exceptions
raised by individual LLM providers when the parsed JSON response
isn't the expected ``dict`` shape.

Lives under ``common/llm/providers/`` (private module). Monitoring /
fallback code should import ``ProviderResponseShapeError`` from
``common.llm.providers`` (re-exported in __init__.py) to catch all
three subclasses at once.
"""

from __future__ import annotations

# Captured-content cap. 1 KiB is enough to identify the schema-miss
# class while keeping log lines bounded — a megabyte-scale aberrant
# response would otherwise blow up log ingestion.
_CONTENT_TRUNCATION_LIMIT = 1024


class ProviderResponseShapeError(ValueError):
    """Base for ``Vertex/Gemini/OpenAI ResponseShapeError``.

    Stored attributes (``provider`` / ``content`` / ``parsed_type``)
    follow the ``json.JSONDecodeError`` convention so monitoring code
    can read structured fields without scraping the message string.

    ``self.args`` is set to ``(provider, truncated_content,
    parsed_type)`` so the default ``__reduce__`` round-trips
    correctly under pickle — pytest-xdist and any multiprocessing
    worker pool serialise exception results across process boundaries
    and would otherwise crash with ``TypeError`` reconstructing the
    instance from a single formatted-message string. Subclasses with
    a narrower ``__init__`` signature override ``__reduce__`` to drop
    the hardcoded provider arg.

    The ``content`` carried in ``self.args`` is the post-truncation
    slice (capped at ``_CONTENT_TRUNCATION_LIMIT``); the full
    pre-truncation string is intentionally NOT retained so a
    megabyte-scale aberrant response can't bloat log lines or pickle
    payloads.
    """

    def __init__(self, provider: str, content: str, parsed_type: str) -> None:
        self.provider = provider
        self.content = content[:_CONTENT_TRUNCATION_LIMIT]
        self.parsed_type = parsed_type
        super().__init__(provider, self.content, parsed_type)

    def __str__(self) -> str:
        # Truncation is detected from the post-init slice length so
        # the "(truncated)" label survives a pickle round-trip — a
        # ``_was_truncated`` instance attribute would always
        # reconstruct as False on the receiving side because
        # ``__init__`` re-runs on the already-clipped 1 KiB slice.
        # False positive only when the original content was exactly
        # ``_CONTENT_TRUNCATION_LIMIT`` characters (rare; acceptable
        # given the 1 KiB safety cap).
        label = (
            "Response content (truncated)"
            if len(self.content) == _CONTENT_TRUNCATION_LIMIT
            else "Response content"
        )
        return (
            f"{self.provider} returned a JSON {self.parsed_type} where a dict was expected. "
            f"{label}: {self.content!r}"
        )
