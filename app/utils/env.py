"""Minimal .env loader for local BotYo secrets."""

from __future__ import annotations

import os
from pathlib import Path

from app.utils.logging import ROOT_DIR


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load simple KEY=VALUE pairs from a local .env file."""

    env_path = Path(path) if path is not None else ROOT_DIR / ".env"
    if not env_path.is_absolute():
        env_path = ROOT_DIR / env_path
    if not env_path.exists():
        return {}

    loaded: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip().strip("'").strip('"')
        loaded[normalized_key] = normalized_value

        if override or normalized_key not in os.environ:
            os.environ[normalized_key] = normalized_value

    return loaded
