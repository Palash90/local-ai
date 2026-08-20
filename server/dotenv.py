"""Minimal ``.env`` loader (stdlib only — no external dependency).

Reads a ``KEY=VALUE`` file from the repo root into ``os.environ`` so every
entrypoint (``chat-webui.py``, ``self-chat.py``, ``markdown_hosting.py``)
sees the same settings whether it is launched by hand, systemd or cron.

Only values that are NOT already exported by the real environment are applied,
so a systemd ``EnvironmentFile`` or the shell always wins over the ``.env``.
"""

import os
from pathlib import Path


def load_dotenv(path=None):
    """Load ``path`` (default: repo-root ``.env``) into ``os.environ``."""
    if path is None:
        path = Path(__file__).resolve().parent.parent / ".env"
    path = Path(path)
    if not path.is_file():
        return False
    loaded = False
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded = True
    return loaded


load_dotenv()