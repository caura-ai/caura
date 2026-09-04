"""Wire shape for "this write failed and a retry cannot clear it", shared by both services.

Why this module exists. core-api's bulk route answers a storage 5xx with "504,
retry with the same ``X-Bulk-Attempt-Id``", and ``app.upstream_http_error_handler``
answers every *unhandled* upstream 5xx with "503, retry". Both are right for the
failure they were written for — a lost response, a restart mid-commit, a
transient upstream — and both are wrong for a failure that reproduces byte-for-
byte on every attempt. A client that complies with either retries forever. In
prod that ran at ~680 requests/hour for 29 hours against a batch that could
never compile, and nothing on the wire distinguished it from noise.

So storage has to be able to SAY "permanent", and the word has to survive the
trip. Retryable stays the default: the costs are asymmetric. A wrong "retry" is
a no-op by construction, because ``ix_memories_attempt_unique`` resolves an
already-committed row to ``duplicate_attempt`` with its canonical id — while a
wrong "do not retry" strands committed rows with nothing to recover them. Hence
:func:`is_permanent` tests for the literal ``False`` rather than falsiness:
an absent key, a null, a non-JSON body and a proxy's HTML error page must all
read as "not marked".

Pure data: no framework imports, so both services and their tests can use it —
the same reason ``common/duplicate_memory.py`` is shaped this way, and for the
same failure it was built to prevent. The marker key and the code strings used
to be literals written once in the storage route and again in core-api's
reader: two separately-deployed services required to agree byte-for-byte, with
no symbol to grep and no import to follow.
"""

from __future__ import annotations

# core-api's error code for a refused write that must not be retried. Named for
# the ANSWER, not the cause: a caller's next move is the same whatever made the
# failure permanent, and the specific cause travels in ``error`` below.
PERMANENT_WRITE_FAILURE_CODE = "PERMANENT_WRITE_FAILURE"

# The key storage sets to ``False``. Deliberately not ``permanent: true`` — the
# question a caller asks is "may I retry?", and answering the question actually
# asked is what keeps a reader from inverting it by accident.
RETRYABLE_KEY = "retryable"

# Causes. One per way a write can fail permanently; today there is one.
# ``bulk_row_shape``: the rows of a bulk batch disagreed on which columns they
# set, so the multi-values INSERT has no single column list to compile.
CAUSE_BULK_ROW_SHAPE = "bulk_row_shape"


class PermanentWriteFailure(Exception):
    """Base for a write refusal that a retry cannot clear.

    Both services raise a subclass of this — storage when it detects the fault,
    core-api when it reads the marker back off the wire — so the "carries
    structured fields beside the message" behaviour is defined once.

    The single definition is not only tidiness. ``scripts/tenant_scope_gate.py``
    resolves functions in the modules it scans by BARE NAME and refuses to run
    when one is defined twice, so a second hand-written ``__init__`` in
    ``postgres_service.py`` or ``storage_client.py`` takes the whole tenancy
    invariant offline — with an error about ambiguous names that says nothing
    about tenancy. Inheriting costs nothing and cannot trip it.

    ``fields`` is the structured half. ``common/duplicate_memory.py`` records
    why it exists: the alternative already cost us a uuid serialised into
    English, shipped across two service boundaries, and recovered at the far
    end with a regular expression.
    """

    def __init__(self, message: str, fields: dict | None = None) -> None:
        super().__init__(message)
        self.fields: dict = fields or {}


def permanent_detail(*, cause: str, message: str, **fields: object) -> dict:
    """Storage's ``detail`` for a failure a retry cannot clear.

    ``message`` is prose for a human; ``cause`` is the stable string a program
    branches on. Extra *fields* ride alongside rather than being formatted into
    the message — ``common/duplicate_memory.py``'s docstring records what the
    alternative cost us, a uuid serialised into English and recovered at the far
    end with a regular expression.
    """
    return {"error": cause, RETRYABLE_KEY: False, "message": message, **fields}


def is_permanent(detail: object) -> bool:
    """Did storage explicitly mark this failure as one a retry cannot clear?

    Takes the parsed ``detail`` — anything at all, because on an error path it
    may be a string from FastAPI's default handler, a list, or absent entirely.
    Only a mapping carrying ``retryable`` set to exactly ``False`` counts; see
    the module docstring for why the default must stay retryable.
    """
    return isinstance(detail, dict) and detail.get(RETRYABLE_KEY) is False
