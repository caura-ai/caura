"""CAURA-651: shared base for ``*ResponseShapeError`` exceptions
raised by individual LLM providers when the parsed JSON response
isn't the expected ``dict`` shape.

Lives under ``common/llm/providers/`` (private module) because each
provider class file imports it directly. Monitoring / fallback code
can ``except ProviderResponseShapeError`` to catch all three at once.
"""

from __future__ import annotations

# Captured-content cap. 1 KiB is enough to identify the schema-miss
# class while keeping log lines bounded — a megabyte-scale aberrant
# response would otherwise blow up log ingestion.
_CONTENT_TRUNCATION_LIMIT = 1024


class ProviderResponseShapeError(ValueError):
    """Base for ``Vertex/Gemini/OpenAI ResponseShapeError``.

    Stored attributes (``content`` / ``parsed_type``) follow the
    ``json.JSONDecodeError`` convention so monitoring code can read
    structured fields without scraping the message string.
    """

    def __init__(self, provider: str, content: str, parsed_type: str) -> None:
        self.provider = provider
        self.content = content[:_CONTENT_TRUNCATION_LIMIT]
        self.parsed_type = parsed_type
        # Conditional label: "(truncated)" only when content actually
        # got cut, so log readers don't suspect missing context for
        # short aberrant responses.
        label = (
            "Response content (truncated)"
            if len(content) > _CONTENT_TRUNCATION_LIMIT
            else "Response content"
        )
        super().__init__(
            f"{provider} returned a JSON {parsed_type} where a dict was expected. "
            f"{label}: {self.content!r}"
        )
