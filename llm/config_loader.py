"""Model config loading — YAML parsing, ${VAR} expansion, ModelConfig construction.

Extracted from ModelRouter._load_configs/_expand_env: pure config-file
parsing has no coupling to model-routing runtime state (rate limiter,
active model, ollama endpoint), so it's testable and reusable without a
live ModelRouter instance. ModelRouter._load_configs still owns wiring the
parsed configs into self.configs/self.config_by_name/self.rate_limiter etc.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import structlog
import yaml

from .config import ModelConfig

logger = structlog.get_logger()


def expand_env(value: Optional[str]) -> Optional[str]:
    """Expand ${VAR} and ${VAR:-default} references using os.environ.

    Syntax:
      ${VAR}          — replaced by env value; left as-is if unset
      ${VAR:-default} — replaced by env value; falls back to *default* if unset

    Using ${VAR:-default} in config files means the system works with no
    .env file — the explicit default is used and no URL stays unexpanded.
    """
    if not value or "${" not in value:
        return value

    def _replacer(m: re.Match) -> str:
        spec = m.group(1)
        if ":-" in spec:
            var, default = spec.split(":-", 1)
            return os.environ.get(var.strip(), default)
        return os.environ.get(spec, m.group(0))  # leave placeholder if unset

    return re.sub(r"\$\{([^}]+)\}", _replacer, value)


def load_config_file(path: str) -> Optional[dict]:
    """Read and YAML-parse *path*. Returns None (and logs) if the file is missing."""
    config_file = Path(path)
    if not config_file.exists():
        logger.error(
            "config_not_found",
            path=str(config_file.resolve()),
            hint="Check that config/models.yaml exists at the project root",
        )
        return None

    with open(config_file) as f:
        return yaml.safe_load(f)


def build_model_config(entry: dict) -> ModelConfig:
    """Expand ${VAR} references in *entry* and construct a ModelConfig.

    May raise if *entry* doesn't match ModelConfig's constructor.
    """
    expanded = {
        k: (expand_env(v) if isinstance(v, str) else v)
        for k, v in entry.items()
    }
    config = ModelConfig(**expanded)
    if config.api_key_env:
        config.api_key = os.environ.get(config.api_key_env)
    return config
