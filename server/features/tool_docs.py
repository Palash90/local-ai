"""Per-user warm cache of full tool documentation (the tool_details payloads).

The wire schemas are slim so every request stays cheap; the model fetches a
tool's full docs once via the ``tool_details`` meta-tool. This cache persists
what a user has already fetched so later sessions get the docs injected as a
trailing reference block instead of paying an extra LLM round. Entries are
keyed by a content hash of the live TOOLS_DETAILED record, so editing a doc
in ``config.py`` self-invalidates and the model re-fetches the fresh text.

Injected docs live OUT of the stored session and after the whole history
(see llm._append_turn_context), so the prompt prefix — stable system block
plus past messages — stays byte-identical turn over turn for KV reuse.
"""

import hashlib
import json
import os
import re
import threading

from server.config import TOOL_DOCS_CACHE_DIR, live_tools_detailed

# TEMPORARY (music DSL calibration): keep tool_details always-fresh for
# generate_music — no warm preload, so edits to prompts/music_dsl.txt take
# effect in every new session immediately. Delete the entry (and re-warm
# happens naturally) once the DSL doc is considered final.
WARM_DISABLED = {"generate_music"}

_LOCK = threading.Lock()

# Tools whose full docs are worth caching, and the keyword gate that justifies
# paying their tokens this round. A miss only costs what the old flow cost: one
# tool_details round. Image tools share one gate (~200 tok each).
_IMAGE_RX = (
    r"\b(image|images|picture|pictures|photo|photos|photograph|photoshop|"
    r"draw|drawing|illustration|illustrations|poster|posters|logo|logos|"
    r"wallpaper|wallpapers|artwork|sketch|portrait|portraits|render)\b"
)
WARM_TRIGGERS = {
    "generate_music": (
        r"\b(music|musical|song|songs|melody|melodies|tune|tunes|jingle|"
        r"compose|composition|instrumental|beat|beats|rhythm|lyrics|"
        r"orchestra|orchestral|soundtrack|lullaby|anthem)\b"
    ),
    "generate_image": _IMAGE_RX,
    "edit_image": _IMAGE_RX,
}


def _detail(name):
    for t in live_tools_detailed():
        if t.get("function", {}).get("name") == name:
            return json.dumps(t, sort_keys=True)
    return None


def _safe_user(user):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (user or "")) or "_anon"


def _file(user, cache_dir=None):
    d = cache_dir or TOOL_DOCS_CACHE_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, _safe_user(user) + ".json")


MAX_ENTRIES = 6


def warm(user, names, cache_dir=None):
    """Persist the current full docs for ``names`` under ``user``."""
    rec = {}
    for name in names:
        if name in WARM_DISABLED:
            continue
        payload = _detail(name)
        if payload is None:
            continue
        rec[name] = {
            "hash": hashlib.sha256(payload.encode()).hexdigest()[:16],
            "payload": payload,
        }
    if not rec:
        return []
    path = _file(user, cache_dir)
    with _LOCK:
        stored = {}
        try:
            with open(path) as f:
                stored = json.load(f)
            if not isinstance(stored, dict):
                stored = {}
        except (OSError, ValueError):
            stored = {}
        # Newly warmed entries win; trim to the most recent MAX_ENTRIES.
        stored = {k: v for k, v in stored.items() if k not in rec}
        stored.update(rec)
        if len(stored) > MAX_ENTRIES:
            for k in list(stored)[: len(stored) - MAX_ENTRIES]:
                stored.pop(k)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(stored, f)
        os.replace(tmp, path)
    return sorted(rec)


def fresh(user, cache_dir=None):
    """{tool: payload_json} whose cached hash matches the live TOOLS_DETAILED."""
    path = _file(user, cache_dir)
    with _LOCK:
        try:
            with open(path) as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return {}
        out, changed = {}, False
        for name, entry in list(stored.items()):
            if name in WARM_DISABLED:
                continue
            live = _detail(name) if isinstance(entry, dict) else None
            h = entry.get("hash") if isinstance(entry, dict) else None
            if live and h == hashlib.sha256(live.encode()).hexdigest()[:16]:
                out[name] = live
            else:
                stored.pop(name, None)
                changed = True
        if changed:
            try:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(stored, f)
                os.replace(tmp, path)
            except OSError:
                pass
        return out


def docs_in_history(messages):
    """Tool names already delivered to the model this session via a
    tool_details result (a JSON array of TOOLS_DETAILED entries) in the
    *sent* messages — compacted-away results drop out, so docs return when
    history compression evicted them."""
    found = set()
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "tool":
            continue
        c = m.get("content")
        if not isinstance(c, str) or "function" not in c[:2000]:
            continue
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if not isinstance(data, list):
            continue
        for e in data:
            try:
                found.add(e["function"]["name"])
            except (KeyError, TypeError):
                pass
    return found


_MUSIC_REQUEST_RE = re.compile(
    r"\b(?:generat\w*|creat\w*|mak\w*|compos\w*|produc\w*|writ\w*|play\w*|"
    r"record\w*|render\w*|give\w*)\b[^.?!\n]{0,48}"
    r"\b(music|audio|soundtrack|songs?|melod(?:y|ies)|tunes?|jingles?|"
    r"beats?|instrumentals?|compositions?|symphon(?:y|ies)|lullabies?|"
    r"anthems?|soundscapes?|piece|pieces|track|recording)\b",
    re.IGNORECASE,
)


def music_directive(user_text, delivered=False):
    """Per-request hard nudge when the CURRENT message asks for music.

    Small quantized models skip tool calls at creative temperatures even when
    the system prompt covers it; a directive riding in the round's context
    block is the deterministic lever. Returns "" when not applicable.
    """
    if delivered or not user_text:
        return ""
    if not _MUSIC_REQUEST_RE.search(user_text):
        return ""
    return (
        "[music-directive] The user has asked you to generate music this turn. "
        "You MUST call generate_music: if its documentation is not already in "
        "context, call tool_details(\"generate_music\") first, then compose a "
        "valid score per the DSL. Never describe or claim music without a "
        "successful generate_music result in this conversation."
    )


def docs_block(user, text, messages, cache_dir=None):
    """Trailing-context block with the warm docs relevant to ``text``, or "".

    Skips tools whose docs are already in the sent history and tools the
    current request text doesn't trigger.
    """
    if not user:
        return ""
    text = (text or "").lower()
    already = docs_in_history(messages)
    parts, names = [], []
    for tool, payload in fresh(user, cache_dir).items():
        rx = WARM_TRIGGERS.get(tool)
        if rx is None or tool in already:
            continue
        if not re.search(rx, text):
            continue
        names.append(tool)
        parts.append(
            f'<tool_reference name="{tool}">\n{payload}\n</tool_reference>'
        )
    if not parts:
        return ""
    joined = ", ".join(sorted(names))
    return (
        "<preloaded_tools>\n"
        f"Full documentation for {joined} is preloaded below. It is already "
        "loaded — do NOT call tool_details for these tools; write scores/"
        "prompts exactly per this documentation.\n"
        + "\n".join(parts)
        + "\n</preloaded_tools>"
    )
