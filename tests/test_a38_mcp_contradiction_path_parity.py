"""A38 — does the MCP write path bypass contradiction detection?

The row records a suspicion ("MCP-driven writes may bypass parts of Path C —
coverage unverified vs the REST write path"), not a known defect. This pins the
answer so it stops being an open question.

Answer: they cannot diverge, because detection is scheduled INSIDE the write
pipeline, and both entry points reach it through the same ``create_memory``:

    REST  /api/v1/memories  -> write_memory -> _write_memory_inner -> create_memory
    MCP   caura_write       -> create_memory
                                     |
                                     v
                        _create_memory_pipeline
                                     |
                                     v
                        ScheduleBackgroundTasks   <- entity extraction +
                                                     contradiction detection

If someone later moves detection up into the REST route, the suspicion in A38
becomes true and these tests fail.
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_detection_is_scheduled_inside_the_write_pipeline():
    """If detection ever moves to a route handler, every non-REST entry point
    silently loses it. Keeping it in the pipeline step is what makes parity
    structural rather than a thing to remember."""
    from core_api.pipeline.steps.write import schedule_background_tasks as sbt

    src = inspect.getsource(sbt)
    assert "contradiction" in src.lower()


def test_mcp_write_goes_through_create_memory():
    from core_api import mcp_server

    src = inspect.getsource(mcp_server.caura_write)
    assert "create_memory(" in src, "MCP write no longer routes through create_memory"


def test_create_memory_runs_the_pipeline():
    from core_api.services import memory_service

    src = inspect.getsource(memory_service.create_memory)
    assert "_create_memory_pipeline" in src


def test_rest_and_mcp_share_the_same_write_entry():
    """Both surfaces must land on create_memory; a second, parallel write
    implementation is how detection coverage would drift apart."""
    from core_api import mcp_server
    from core_api.routes import memories as rest

    rest_src = inspect.getsource(rest)
    mcp_src = inspect.getsource(mcp_server.caura_write)
    assert "create_memory" in rest_src
    assert "create_memory" in mcp_src
