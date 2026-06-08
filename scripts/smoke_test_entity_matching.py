"""Smoke test for the improved entity matching (stopwords + PG FTS).

Run: python -m scripts.smoke_test_entity_matching
"""

from core_api.constants import ENTITY_STOPWORDS, ENTITY_TOKEN_MIN_LENGTH

# ── Pure Python: token filtering ──

def extract_tokens(query: str) -> list[str]:
    """Mirrors the new logic in memory_service.search_memories."""
    return [
        t.lower() for t in query.split()
        if len(t) >= ENTITY_TOKEN_MIN_LENGTH and t.lower() not in ENTITY_STOPWORDS
    ]


CASES = [
    # (query, should_include, should_exclude)
    # --- Basic stopword filtering ---
    ("What is the project status?", ["project", "status"], ["what", "the"]),
    ("How does the deployment pipeline work?", ["deployment", "pipeline"], ["how", "does", "the"]),
    ("Tell me about art and design", ["art", "design"], ["tell", "about", "and"]),
    ("Where is John Smith located?", ["john", "smith", "located"], ["where"]),
    ("a]", [], []),  # short tokens filtered by length

    # --- Common conversational verbs (should be stopwords) ---
    ("Show me the latest deployments", ["latest", "deployments"], ["show"]),
    ("Give me information about Kafka", ["information", "kafka"], ["give"]),
    ("Can you find the database config?", ["database", "config"], ["can", "you", "find"]),
    ("Let me know about the API gateway", ["api", "gateway"], ["let", "know", "about"]),
    ("Please explain the auth service", ["explain", "auth", "service"], ["please"]),
    ("I want to see the user table", ["user", "table"], ["want", "see"]),
    ("Help me understand Redis caching", ["understand", "redis", "caching"], ["help"]),
    ("I need to check the logs", ["check", "logs"], ["need"]),
    ("Could you look at the error handler?", ["error", "handler"], ["could", "you", "look"]),
    ("Do you remember the migration issue?", ["remember", "migration", "issue"], ["you"]),

    # --- Question patterns (interrogatives as noise) ---
    ("What happened with the payment service?", ["happened", "payment", "service"], ["what", "with", "the"]),
    ("When was the last incident?", ["last", "incident"], ["when", "was", "the"]),
    ("Who created the billing module?", ["created", "billing", "module"], ["who"]),
    ("Which team owns the search service?", ["team", "owns", "search", "service"], ["which"]),
    ("Why did the pipeline fail?", ["pipeline", "fail"], ["why", "did", "the"]),

    # --- Agent-style prompts ---
    ("Retrieve all memories about project alpha", ["retrieve", "memories", "project", "alpha"], ["all", "about"]),
    ("List everything related to onboarding", ["everything", "related", "onboarding"], []),
    ("Summarize what we know about the client", ["summarize", "client"], ["what"]),
    ("Update me on the sprint progress", ["update", "sprint", "progress"], ["the"]),
    ("Remind me about the deadline for Q2", ["remind", "deadline"], ["about", "the", "for"]),

    # --- Should NOT strip domain-relevant short words ---
    ("API key rotation policy", ["api", "key", "rotation", "policy"], []),
    ("CI CD pipeline status", ["pipeline", "status"], []),
    ("SQL injection vulnerability", ["sql", "injection", "vulnerability"], []),
    ("AWS S3 bucket permissions", ["aws", "bucket", "permissions"], []),
]

print("=" * 60)
print("SMOKE TEST: Entity Token Filtering")
print("=" * 60)

all_pass = True
for query, should_include, should_exclude in CASES:
    tokens = extract_tokens(query)
    # Check inclusions (strip punctuation for comparison)
    clean_tokens = [t.rstrip("?.,!") for t in tokens]
    included_ok = all(s.rstrip("?.,!") in clean_tokens for s in should_include)
    excluded_ok = all(s not in clean_tokens for s in should_exclude)
    passed = included_ok and excluded_ok
    all_pass = all_pass and passed

    status = "PASS" if passed else "FAIL"
    print(f"\n[{status}] Query: \"{query}\"")
    print(f"  Tokens: {tokens}")
    if not included_ok:
        missing = [s for s in should_include if s.rstrip("?.,!") not in clean_tokens]
        print(f"  MISSING expected: {missing}")
    if not excluded_ok:
        leaked = [s for s in should_exclude if s in clean_tokens]
        print(f"  LEAKED stopwords: {leaked}")

# ── PG FTS simulation (no DB needed) ──
# Show what PG would do with plainto_tsquery('english', ...)

print("\n" + "=" * 60)
print("SMOKE TEST: PG FTS Query Preview")
print("=" * 60)

FTS_CASES = [
    ("What is the deployment status?", "deployment & status"),
    ("Tell me about Kafka cluster configuration", "kafka & cluster & configuration"),
    ("art gallery exhibits", "art & gallery & exhibit"),  # PG stems "exhibits" → "exhibit"
]

for query, expected_behavior in FTS_CASES:
    tokens = extract_tokens(query)
    fts_input = " ".join(tokens)
    print(f"\n  Query: \"{query}\"")
    print(f"  Tokens after stopword filter: {tokens}")
    print(f"  → plainto_tsquery('english', '{fts_input}')")
    print(f"  Expected PG behavior: {expected_behavior}")

# ── Substring vs FTS comparison ──

print("\n" + "=" * 60)
print("SMOKE TEST: Old vs New Matching")
print("=" * 60)

ENTITY_NAMES = ["deployment pipeline", "john smith", "kafka cluster", "start menu", "smart home", "particle physics"]

for query in ["What is the deployment status?", "art gallery", "started the project"]:
    tokens = extract_tokens(query)

    # Old behavior: substring match
    old_matches = []
    for name in ENTITY_NAMES:
        old_tokens = [t.lower() for t in query.split() if len(t) >= ENTITY_TOKEN_MIN_LENGTH]
        if any(t in name for t in old_tokens):
            old_matches.append(name)

    # New behavior: word-boundary match (simulated — real matching uses PG tsvector)
    new_matches = []
    for name in ENTITY_NAMES:
        name_words = set(name.lower().split())
        if any(t.rstrip("?.,!") in name_words for t in tokens):
            new_matches.append(name)

    print(f"\n  Query: \"{query}\"")
    print(f"  OLD (substring): {old_matches or '(none)'}")
    print(f"  NEW (word boundary + stopwords): {new_matches or '(none)'}")
    removed = set(old_matches) - set(new_matches)
    if removed:
        print(f"  ✓ Eliminated false positives: {removed}")

print("\n" + "=" * 60)
result = "ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED"
print(result)
print("=" * 60)
