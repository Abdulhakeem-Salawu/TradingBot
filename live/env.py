"""Minimal .env loader so cron jobs need no extra packages."""

from __future__ import annotations

import os
from pathlib import Path


def parse_line(line: str) -> tuple[str, str] | None:
    """KEY=VALUE, with optional quotes and an optional trailing ' # comment'."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    key, value = line.split("=", 1)
    value = value.strip()
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        value = value[1:end] if end > 0 else value[1:]
    else:
        hash_at = value.find(" #")
        if hash_at >= 0:
            value = value[:hash_at]
        value = value.strip()
    return key.strip(), value


def load_env(path: str = ".env") -> None:
    """Read KEY=VALUE lines into the environment. Never overrides a variable already set."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        kv = parse_line(line)
        if kv:
            os.environ.setdefault(*kv)
