"""09/02 M-38 — the handler that un-wedges a report must not wedge it.

``start_crystallization`` reserves a report row as ``running`` and then
publishes the work. If that publish fails, nothing will ever execute the run,
so the row would block every later trigger for the tenant until the staleness
cutoff expired. The ``except`` block exists precisely to mark it terminal.

That block omitted ``duration_ms``. Storage's PATCH handler reads it with a
bare subscript — ``duration_ms=body["duration_ms"]`` — while its neighbours
(``summary``, ``hygiene``, ``health``) use ``.get`` with defaults. So the
recovery write raised KeyError inside the recovery itself, the inner
best-effort guard swallowed it, and the report stayed ``running`` forever:
the exact outcome the block was written to prevent, reached through the code
that prevents it.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def _publish_failure_block() -> str:
    """The ``except`` arm of ``start_crystallization``'s publish guard.

    Sliced from the publish call to the ``raise`` that ends the arm, so a later
    ``update_report`` elsewhere in the module cannot be mistaken for this one —
    there are three in the file and the other two were always correct.
    """
    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs.start_crystallization)
    start = src.index("except BaseException:")
    return src[start : src.index("raise", start)]


def test_the_recovery_write_sends_duration_ms():
    """Without it the PATCH raises KeyError and the row stays 'running'."""
    assert '"duration_ms"' in _publish_failure_block()


def test_storage_still_requires_it_rather_than_defaulting():
    """Pins WHY the caller must send it. If storage is ever made lenient this
    test fails and this fix can be reconsidered — but until then a missing key
    is a hard error, not a tolerated omission."""
    from core_storage_api.routers import reports

    src = inspect.getsource(reports.update_report)
    assert 'duration_ms=body["duration_ms"]' in src, (
        "storage no longer requires duration_ms — re-evaluate M-38's fix"
    )


def test_the_other_two_report_writes_are_untouched():
    """The run's own completion paths always sent a real elapsed time. This fix
    must not have flattened either to a constant."""
    from core_api.services import crystallizer_service as cs

    src = inspect.getsource(cs)
    assert '"duration_ms": elapsed_ms' in src
    assert '"duration_ms": int((time.monotonic() - t0) * 1000)' in src


def test_the_publish_failure_marks_the_report_terminal():
    """The surrounding contract: a failed publish must leave a report someone
    can inspect, not a row that blocks the tenant until the staleness cutoff."""
    block = _publish_failure_block()
    assert '"status": "failed"' in block
    assert '"completed_at"' in block
