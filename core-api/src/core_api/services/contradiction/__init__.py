"""Contradiction engine package (A55).

A single seam over contradiction detection. Today the detection logic lives in
``core_api.services.contradiction_detector``; this package introduces the
``ContradictionEngine`` facade + a flag-gated ``run_contradiction_detection``
router so trigger sites have one entry point and the legacy arch can be retired
later. See benchmark/a55/subtasks/10-contradiction-engine-decision.md.
"""

from core_api.services.contradiction.dispatch import run_contradiction_detection
from core_api.services.contradiction.engine import ContradictionEngine, Trigger

__all__ = ["ContradictionEngine", "Trigger", "run_contradiction_detection"]
