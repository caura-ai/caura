"""Canonical service-agent IDs and their permanent client-input aliases."""

# Dedicated system identity for the automated nightly insights run. It is
# registered per tenant with ``belonging_type='service'`` on first use.
INSIGHTER_AGENT_ID = "caura-insighter"

# Fallback for document-derived memories when the caller has no identity. A
# real document writer's identity is preferred whenever one exists.
DOC_INDEXER_AGENT_ID = "caura-doc-indexer"

# These names are persisted; keep their supported retired inputs as translation
# only, never as write identities.
_SERVICE_AGENT_INPUT_ALIASES = {
    "memclaw-insighter": INSIGHTER_AGENT_ID,  # legacy-name-ok: supported client input alias
    "memclaw-doc-indexer": DOC_INDEXER_AGENT_ID,  # legacy-name-ok: supported client input alias
}


def canonical_service_agent_id(agent_id: str) -> str:
    """Map a retired client-supplied service ID to its canonical identity."""
    return _SERVICE_AGENT_INPUT_ALIASES.get(agent_id, agent_id)
