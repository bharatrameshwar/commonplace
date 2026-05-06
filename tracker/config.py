"""Config loader. Reads `config.yaml` from the repo root, falling back to
`config.example.yaml` so the app boots even when the user hasn't customised
anything yet.

User-personal data lives in `config.yaml` only — that file is gitignored.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
USER_CONFIG_PATH = REPO_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "config.example.yaml"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # PyYAML
    except ImportError as e:
        raise RuntimeError(
            "PyYAML is required. Install with `pip install pyyaml`."
        ) from e
    with path.open("r") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping at the top level")
    return data


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    """Return the merged config dict.

    Loads `config.yaml` if present, otherwise `config.example.yaml`.
    Cached for the process lifetime — restart the daemon to pick up changes.
    """
    if USER_CONFIG_PATH.exists():
        return _load_yaml(USER_CONFIG_PATH)
    if EXAMPLE_CONFIG_PATH.exists():
        logger.info("config.yaml not found, falling back to config.example.yaml")
        return _load_yaml(EXAMPLE_CONFIG_PATH)
    return {}


def user_profile() -> dict[str, Any]:
    return get_config().get("user_profile") or {}


def self_aliases() -> set[str]:
    """Lowercase set of name forms that should be filtered as 'self'."""
    aliases = user_profile().get("self_aliases") or []
    return {a.strip().lower() for a in aliases if a.strip()}


def person_stopwords() -> set[str]:
    """Lowercase set of words that should never be detected as people."""
    words = user_profile().get("person_stopwords") or []
    return {w.strip().lower() for w in words if w.strip()}


def user_name() -> str:
    return (user_profile().get("name") or "").strip()


def user_role() -> str:
    return (user_profile().get("role") or "").strip()


def user_organization() -> str:
    return (user_profile().get("organization") or "").strip()


def ai_settings() -> dict[str, Any]:
    cfg = get_config().get("ai") or {}
    return {
        "ollama_url": os.environ.get("OLLAMA_URL", cfg.get("ollama_url", "http://localhost:11434")),
        "model": os.environ.get("OLLAMA_MODEL", cfg.get("model", "qwen3:8b")),
    }
