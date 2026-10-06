"""Strict configuration; obsolete direct-transport settings fail at startup."""

import os
import tomllib
from pathlib import Path

from .agent import AgentConfig

CONFIG_ENV_VAR = "CAURA_BUS_AGENT_CONFIG"
DEFAULT_CONFIG_PATH = Path("caura-bus.toml")


def load_config(path: Path | None = None) -> AgentConfig:
    path = path or Path(os.environ.get(CONFIG_ENV_VAR, str(DEFAULT_CONFIG_PATH)))
    return AgentConfig.model_validate(tomllib.loads(path.read_text()))


def require_api_key() -> str:
    key = os.environ.get("CAURA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("CAURA_API_KEY is required: use a Caura agent-scoped credential")
    return key
