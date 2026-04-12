"""Cross-platform external tool location probe.

Reads config/environment.yaml to know what tools to look for, then
searches PATH and platform-specific directories to find their binaries
or install locations. Results are cached to data/environment.json so
subsequent startups are instant (delete the cache to force a re-probe).

Each tool can be overridden via an environment variable declared in the
YAML registry — the env var always wins over auto-detection.

Usage
-----
    probe = EnvironmentProbe()
    chromium_dir = probe.get_tool_path("playwright_chromium")
    git_bin      = probe.get_tool_path("git")
    info         = probe.get_all()   # full dict for /environment endpoint
"""

import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import structlog
import yaml

logger = structlog.get_logger()

# Map Python's platform.system() → YAML key
_PLATFORM_MAP = {
    "Windows": "windows",
    "Darwin": "darwin",
    "Linux": "linux",
}


def _expand_path(raw: str) -> Path:
    """Expand {LOCALAPPDATA} / {APPDATA} / ~ in a path string."""
    expanded = raw
    for var in ("LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
        env_val = os.environ.get(var, "")
        expanded = expanded.replace(f"{{{var}}}", env_val)
    expanded = os.path.expanduser(expanded)
    return Path(expanded)


class EnvironmentProbe:
    """Probe and cache external tool paths across platforms."""

    def __init__(
        self,
        config_path: str = "config/environment.yaml",
        cache_path: str = "data/environment.json",
    ):
        from local_coding_agent import _PROJECT_ROOT

        self._config_path = _PROJECT_ROOT / config_path
        self._cache_path = _PROJECT_ROOT / cache_path
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._platform = _PLATFORM_MAP.get(platform.system(), "linux")
        self._config: Dict[str, Any] = {}
        self._resolved: Dict[str, Optional[str]] = {}

        self._load_config()
        self._load_or_probe()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_tool_path(self, name: str) -> Optional[str]:
        """Return the resolved path string for *name*, or None if not found."""
        return self._resolved.get(name)

    def get_all(self) -> Dict[str, Any]:
        """Return full status dict suitable for an API response."""
        tools_cfg = self._config.get("tools", {})
        result = {}
        for name, cfg in tools_cfg.items():
            path = self._resolved.get(name)
            result[name] = {
                "description": cfg.get("description", ""),
                "found": path is not None,
                "path": path,
                "env_override": cfg.get("env_override"),
                "required": cfg.get("required", True),
            }
        return result

    def reprobe(self) -> None:
        """Force a fresh probe (ignores cache) and save the result."""
        self._probe_all()
        self._save_cache()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        if not self._config_path.exists():
            logger.warning("environment_config_missing", path=str(self._config_path))
            return
        with open(self._config_path, encoding="utf-8") as fh:
            self._config = yaml.safe_load(fh) or {}

    def _load_or_probe(self) -> None:
        if self._cache_path.exists():
            try:
                data = json.loads(self._cache_path.read_text(encoding="utf-8"))
                # Validate cache platform matches current
                if data.get("platform") == self._platform:
                    self._resolved = data.get("tools", {})
                    logger.info("environment_cache_loaded", tools=len(self._resolved))
                    return
            except Exception as e:
                logger.warning("environment_cache_invalid", error=str(e))

        self._probe_all()
        self._save_cache()

    def _probe_all(self) -> None:
        tools_cfg = self._config.get("tools", {})
        self._resolved = {}
        for name, cfg in tools_cfg.items():
            self._resolved[name] = self._probe_one(name, cfg)
        logger.info(
            "environment_probed",
            platform=self._platform,
            found=[k for k, v in self._resolved.items() if v],
            missing=[k for k, v in self._resolved.items() if not v],
        )

    def _probe_one(self, name: str, cfg: dict) -> Optional[str]:
        # 1. Env-var override always wins
        env_key = cfg.get("env_override")
        if env_key:
            env_val = os.environ.get(env_key, "").strip()
            if env_val:
                logger.debug("tool_from_env", tool=name, path=env_val)
                return env_val

        is_directory = cfg.get("is_directory", False)
        binary = cfg.get("binary")
        search_paths = cfg.get("search_paths", {}).get(self._platform, [])

        # 2. Binary tools: try shutil.which first (respects PATH)
        if binary and not is_directory:
            found = shutil.which(binary)
            if found:
                logger.debug("tool_from_path", tool=name, path=found)
                return found

        # 3. Search platform-specific directories
        for raw_path in search_paths:
            candidate = _expand_path(raw_path)
            if is_directory:
                if candidate.exists() and candidate.is_dir():
                    logger.debug("tool_dir_found", tool=name, path=str(candidate))
                    return str(candidate)
            elif binary:
                # Try binary directly in this directory
                bin_path = candidate / binary
                if sys.platform == "win32":
                    # Also try .exe / .cmd variants
                    for ext in (".exe", ".cmd", ""):
                        test = candidate / (binary + ext)
                        if test.exists():
                            return str(test)
                elif bin_path.exists():
                    return str(bin_path)

        logger.debug("tool_not_found", tool=name)
        return None

    def _save_cache(self) -> None:
        try:
            data = {"platform": self._platform, "tools": self._resolved}
            self._cache_path.write_text(
                json.dumps(data, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("environment_cache_save_failed", error=str(e))


# Module-level singleton — created lazily on first import
_probe: Optional[EnvironmentProbe] = None


def get_environment_probe() -> EnvironmentProbe:
    """Return the module-level singleton probe, creating it if needed."""
    global _probe
    if _probe is None:
        _probe = EnvironmentProbe()
    return _probe
