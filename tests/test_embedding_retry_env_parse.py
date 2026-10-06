"""oss-0909-l-02 — ``EMBEDDING_RETRY_*`` must not crash the module import.

``EMBEDDING_RETRY_ATTEMPTS`` / ``EMBEDDING_RETRY_DELAY_S`` were parsed with
bare ``int()`` / ``float()`` in ``common/embedding/constants.py``'s module
body, so a garbage value raised ``ValueError`` at import and crash-looped
every service importing ``common.embedding`` — with a traceback naming
neither variable. Same defect, and same fix, as ``OPENAI_REQUEST_TIMEOUT_SECONDS``
(#1441, ``tests/test_embedding_timeout_env_parse.py``).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

import common.embedding.constants as embedding_constants

pytestmark = pytest.mark.unit


def _exec_constants_fresh() -> ModuleType:
    """Run the constants module body again as a new, unregistered module."""
    path = Path(embedding_constants.__file__)
    spec = importlib.util.spec_from_file_location("_fresh_embedding_constants", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("var", "bad", "default"),
    [
        ("EMBEDDING_RETRY_ATTEMPTS", "2x", 2),
        ("EMBEDDING_RETRY_ATTEMPTS", "", 2),
        # 0 parsed fine pre-fix but made ``_run_with_retry`` call nothing.
        ("EMBEDDING_RETRY_ATTEMPTS", "0", 2),
        ("EMBEDDING_RETRY_DELAY_S", "1s", 1.0),
        ("EMBEDDING_RETRY_DELAY_S", "", 1.0),
    ],
)
def test_bad_value_falls_back_and_names_the_var(var, bad, default, capsys):
    with patch.dict(os.environ, {var: bad}):
        mod = _exec_constants_fresh()
    assert getattr(mod, var) == default
    assert var in capsys.readouterr().err


def test_valid_values_are_parsed():
    env = {"EMBEDDING_RETRY_ATTEMPTS": "3", "EMBEDDING_RETRY_DELAY_S": "0.25"}
    with patch.dict(os.environ, env):
        mod = _exec_constants_fresh()
    assert mod.EMBEDDING_RETRY_ATTEMPTS == 3
    assert mod.EMBEDDING_RETRY_DELAY_S == 0.25
