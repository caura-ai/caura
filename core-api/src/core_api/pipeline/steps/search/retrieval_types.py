"""Shared retrieval types for the search pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID


class RetrievalStrategy(str, Enum):
    ENTITY_LOOKUP = "entity_lookup"
    TEMPORAL = "temporal"
    RECENT_CONTEXT = "recent_context"
    KEYWORD_SEARCH = "keyword_search"
    SEMANTIC_SEARCH = "semantic_search"


@dataclass
class RetrievalPlan:
    strategy: RetrievalStrategy
    matched_entity_ids: list[UUID] = field(default_factory=list)
    skip_embedding: bool = False
    skip_scored_search: bool = False
    search_param_overrides: dict[str, Any] = field(default_factory=dict)
