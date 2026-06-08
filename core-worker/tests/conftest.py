"""Shared fixtures for core-worker tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Repo root on sys.path so ``common.*`` resolves the same way it does
# in production (the Dockerfile sets PYTHONPATH=core-worker/src:/app).
_repo_root = str(Path(__file__).resolve().parents[2])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# In-process bus by default — every test runs without a real Pub/Sub.
os.environ.setdefault("EVENT_BUS_BACKEND", "inprocess")
