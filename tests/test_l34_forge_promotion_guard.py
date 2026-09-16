"""09/02 L-34 — a transient promotion failure re-bought the whole tick.

`run_forge_cron_tick` has two halves:

1. **mining** — `run_forge_distill`, which spends LLM calls and writes
   candidates that are durable the moment they land;
2. **promotion** — `promote_pending_candidates`, which flips already-written
   rows `candidate → staged`.

Promotion was awaited unguarded, so any exception propagated out of the tick.
The bus REDELIVERS a failed tick, which re-runs the mining half — re-paying its
LLM cost for candidates that were already written.

A transient storage blip in the cheap, retryable half is not a reason to buy
the expensive, already-completed half twice. Held candidates are designed to
surface on a later tick anyway, so deferring them costs a cron interval.

Reported the way the interview sweep reports its jobs half (`jobs_sweep_ok` /
`jobs_sweep_error`): keep the successful tick, but say which of the two
happened — otherwise a promotion raising on every tick is indistinguishable
from one with nothing to promote.
"""

import ast
import inspect

import pytest

from core_api.services.forge import cron_handler

pytestmark = pytest.mark.unit


def _tick_src() -> str:
    return inspect.getsource(cron_handler.run_forge_cron_tick)


def _tick_code() -> str:
    """The tick with comments and docstrings removed.

    ``ast.unparse`` drops comments outright; docstrings are blanked explicitly.
    Needed because the comments explaining this fix NAME the very constructs
    being asserted about — ``str(exc)`` appears only in a comment saying why it
    is not used, so a raw-source search finds the prose and asserts the
    opposite of the truth. That is exactly what it did when first written.
    """
    tree = ast.parse(_tick_src().lstrip())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value.value = ""
    return ast.unparse(tree)


def _promotion_call_is_guarded() -> bool:
    """True when the ``promote_pending_candidates`` call sits inside a Try.

    AST rather than substring: the comment explaining this fix names the very
    symbols involved, so a text search would match the prose.
    """
    tree = ast.parse(_tick_src().lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "promote_pending_candidates"
            ):
                return True
    return False


def test_the_promotion_half_is_guarded():
    """The fix. Unguarded, a blip here re-runs the LLM-spending half."""
    assert _promotion_call_is_guarded()


def test_the_mining_half_is_not_guarded():
    """Deliberate asymmetry. Mining failures SHOULD fail the tick: nothing was
    written worth keeping, and the docstring's PermanentOpError contract
    depends on them propagating."""
    tree = ast.parse(_tick_src().lstrip())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "run_forge_distill"
            ):
                raise AssertionError("run_forge_distill must not be swallowed")


def test_the_outcome_is_reported_in_stats():
    """Without this, a promotion that raises every tick returns the same zeros
    as one with nothing to promote, and the mining counters still look
    healthy — so the tick reads fine."""
    src = _tick_src()
    assert '"promotion_ok"' in src


def test_the_stats_dict_stays_int_typed():
    """The interview sweep carries a companion ``jobs_sweep_error`` STRING, but
    its summary is an untyped HTTP response body. This one is
    ``dict[str, int]`` and feeds ``lifecycle_audit``, which does
    ``int(stats.get(...))`` — widening it to admit a string just moves the type
    error to that caller, which is exactly what mypy caught in CI. ``bool`` is
    an ``int`` subtype, so the alertable signal fits without widening, and the
    exception type goes in the log line instead."""
    import inspect

    sig = inspect.signature(cron_handler.run_forge_cron_tick)
    assert str(sig.return_annotation).replace(" ", "") == "dict[str,int]"


def test_only_the_exception_type_is_surfaced():
    """The full exception is already in the log record's traceback, and some
    ``__str__`` implementations carry hostnames, URLs or request fragments —
    the same reason the interview sweep records only the type."""
    code = _tick_code()
    assert "type(exc).__name__" in code
    assert "str(exc)" not in code


def test_the_failure_is_logged_with_the_candidate_count():
    """An operator needs to know the mining work survived — that is the whole
    point of not re-running it."""
    code = _tick_code()
    assert "logger.exception" in code
    assert "candidates_written" in code


def test_stats_keep_one_shape_whether_promotion_ran_or_not():
    """The log line and downstream readers index these keys directly, so the
    failure path zeroes the counters rather than omitting them."""
    code = _tick_code()
    assert "PromoterRunResult(" in code
    assert "promoted=0" in code
    assert "scanned=0" in code
    assert "held=0" in code


def test_the_zeroed_result_matches_the_real_type():
    """A hand-rolled stand-in that drifts from ``PromoterRunResult`` would
    raise inside the stats dict, turning a handled failure back into a tick
    failure — reintroducing the bug through the fix."""
    from core_api.services.skill_promoter import PromoterRunResult

    empty = PromoterRunResult(
        tenant_id="t", fleet_id=None, scanned=0, promoted=0, held=0
    )
    for attr in ("promoted", "auto_approved", "scanned", "held"):
        assert getattr(empty, attr) == 0
