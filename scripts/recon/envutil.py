"""Shared helpers for phase 0 recon scripts.

Loads .env without extra dependencies and scrubs secret values from
every string that is printed or written to reports.
"""

from __future__ import annotations

import os
from pathlib import Path

_SECRET_VALUES: list[str] = []


def load_env(path: Path | None = None) -> None:
    env_path = path or Path(__file__).resolve().parents[2] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def register_secrets() -> None:
    """Collect env values whose name looks like a secret, for scrub()."""
    for name, value in os.environ.items():
        if len(value) >= 8 and ("KEY" in name or "TOKEN" in name or "SECRET" in name):
            if value not in _SECRET_VALUES:
                _SECRET_VALUES.append(value)


def scrub(text: str) -> str:
    for secret in _SECRET_VALUES:
        text = text.replace(secret, "***")
    return text


def has(key: str) -> bool:
    return bool(os.environ.get(key, "").strip())
