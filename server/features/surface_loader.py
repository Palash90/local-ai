"""Shared loader for ``SURFACE_ATTACKS_DIR`` pattern/prompt files.

Used by both the deterministic pattern layer (``server/input_guard.py``) and
the LLM judge (``server/features/judge.py``), so both stay in sync on file
location, caching and optional Fernet decryption.

When ``SURFACE_ATTACKS_KEY`` is set, ``<name>.enc`` files are decrypted in
memory; otherwise plaintext ``<name>`` files are read directly (dev
convenience). All files live under ``SURFACE_ATTACKS_DIR`` (default
``<repo>/prompts/surface_attacks``).
"""

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_fernet = None
_surface_dir = None
_patterns_cache: dict[str, list[str]] = {}
_prompts_cache: dict[str, str] = {}


def _ensure_fernet():
    """Initialise Fernet + surface dir once, on first use."""
    global _fernet, _surface_dir
    if _surface_dir is not None:
        return  # already initialised

    _surface_dir = Path(os.environ.get(
        "SURFACE_ATTACKS_DIR",
        Path(__file__).resolve().parent.parent.parent / "prompts" / "surface_attacks",
    ))

    key = os.environ.get("SURFACE_ATTACKS_KEY", "").strip()
    if key:
        try:
            from cryptography.fernet import Fernet
            _fernet = Fernet(key.encode() if isinstance(key, str) else key)
            log.info("[guardrail] Fernet decryption enabled for %s", _surface_dir)
        except Exception as exc:
            log.warning("[guardrail] bad SURFACE_ATTACKS_KEY, falling back to plaintext: %s", exc)
            _fernet = None
    else:
        log.info("[guardrail] SURFACE_ATTACKS_KEY not set — reading plaintext .txt from %s", _surface_dir)


def _load_raw(name: str) -> bytes:
    """Read a file from ``SURFACE_ATTACKS_DIR``.

    Tries ``<name>.enc`` first (decrypted in memory via Fernet), then falls
    back to ``<name>`` (plaintext).  Raises ``FileNotFoundError`` if neither
    exists.
    """
    _ensure_fernet()
    enc = _surface_dir / f"{name}.enc"
    plain = _surface_dir / name

    if _fernet and enc.exists():
        return _fernet.decrypt(enc.read_bytes())

    if plain.exists():
        return plain.read_bytes()

    raise FileNotFoundError(
        f"[guardrail] Neither {enc} nor {plain} found. "
        f"Set SURFACE_ATTACKS_DIR and (optionally) SURFACE_ATTACKS_KEY."
    )


def _get_patterns(name: str) -> list[str]:
    """Load (and cache) a pattern list — one pattern per line."""
    if name not in _patterns_cache:
        text = _load_raw(name).decode("utf-8")
        _patterns_cache[name] = [line.strip() for line in text.splitlines() if line.strip()]
    return _patterns_cache[name]


def _get_prompt(name: str) -> str:
    """Load (and cache) a prompt text file, stripping trailing whitespace."""
    if name not in _prompts_cache:
        _prompts_cache[name] = _load_raw(name).decode("utf-8").rstrip("\n")
    return _prompts_cache[name]