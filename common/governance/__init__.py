"""Deterministic PII / PCI / secret detection for the governance gate.

The ingestion-boundary governance gate (eToro) screens every memory for
sensitive data before persistence using these deterministic patterns combined
with the LLM's free-form PII signal. This package owns the pattern library +
the ``scan`` / ``mask`` primitives; the pipeline steps and worker remediation
consume them.

Public API:
    PIICategory, Severity, Finding   — result types
    scan(text, enabled_categories)   — find sensitive spans (validator-gated)
    mask(text, findings)             — redact spans, keep the rest
    HIGH_RISK_CATEGORIES             — categories that drive fail-closed behavior
"""

from common.governance.pii_patterns import (
    HIGH_RISK_CATEGORIES,
    Finding,
    PIICategory,
    Severity,
    mask,
    scan,
)

__all__ = [
    "HIGH_RISK_CATEGORIES",
    "Finding",
    "PIICategory",
    "Severity",
    "mask",
    "scan",
]
