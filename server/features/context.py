"""Token estimation, context trimming and context compaction."""

import base64
import json
import os
import re

import requests

from server.features.state import M


def strip_html(text):
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _text_tokens(s):
    if not s:
        return 0
    # Multilingual/non-ASCII characters eat up far more tokens (~2 chars per token vs ~4 for English)
    non_ascii = sum(1 for ch in s if ord(ch) > 0x7F)
    divisor = 2.0 if non_ascii > len(s) * 0.15 else 4.0
    return int(len(s) / divisor)


def estimate_tokens(messages, include_tools=True):
    total = M.TOOLS_TOKEN_COST if include_tools else 0

    for msg in messages:
        total += M.PER_MESSAGE_OVERHEAD
        content = msg.get("content", "")

        # Standard string content
        if isinstance(content, str):
            total += _text_tokens(content)

        # Multi-modal array content (text + images + audio)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    total += _text_tokens(part.get("text", ""))
                elif ptype in ("image_url", "input_image", "image"):
                    total += M.IMAGE_TOKEN_COST
                elif ptype in ("audio_url", "input_audio", "audio"):
                    total += M.AUDIO_TOKEN_COST

        # Tool call tokens
        for tc in msg.get("tool_calls") or []:
            total += _text_tokens(json.dumps(tc))

    # Return MUST be outside the for-loop!
    return max(1, total)


def trim_messages_for_context(messages):
    trimmed = list(messages)
    sys_msg = None
    if trimmed and trimmed[0].get("role") == "system":
        sys_msg = trimmed.pop(0)
    while estimate_tokens(trimmed) > M.MAX_INPUT_TOKENS and len(trimmed) > 1:
        trimmed.pop(0)
    if sys_msg:
        trimmed.insert(0, sys_msg)
    return trimmed


def _summarize_with_llm(text, mode="gpu"):
    payload = {
        "model": M.server_model_id(mode),
        "messages": [
            {
                "role": "system",
                "content": "You summarize conversations concisely, preserving key facts, decisions, user preferences, and unresolved questions.",
            },
            {"role": "user", "content": text},
        ],
        "max_tokens": 1024,
        "temperature": 0.3,
        "stream": False,
    }
    try:
        M.mark_slot_kv_dirty(mode)
        r = requests.post(M.server_url(mode), json=payload, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[compact] LLM summarization failed: {e}")
        return None


def compact_messages_copy(messages, keep_messages=6, mode="gpu"):
    """Return a compacted COPY of the message list (summary + recent messages)
    WITHOUT modifying the stored session. Old messages are summarized, not deleted."""
    msgs = list(messages)
    sys_msg = None
    if msgs and msgs[0].get("role") == "system":
        sys_msg = msgs.pop(0)
    if len(msgs) <= keep_messages + 1:
        return ([sys_msg] + msgs) if sys_msg else msgs
    to_compact = msgs[:-keep_messages] if keep_messages > 0 else msgs
    recent = msgs[-keep_messages:] if keep_messages > 0 else []
    compact_text = ""
    for m in to_compact:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    if p.get("type") == "text":
                        parts.append(p.get("text", ""))
            content = " ".join(parts)
        if not content:
            continue
        compact_text += f"[{role}]: {content}\n\n"
    if not compact_text.strip():
        return ([sys_msg] + msgs) if sys_msg else msgs
    summary = M._summarize_with_llm(
        f"Compress the following conversation into a short paragraph, keeping all important details:\n\n{compact_text}",
        mode,
    )
    if summary is None:
        return ([sys_msg] + msgs) if sys_msg else msgs
    new_msgs = []
    if sys_msg:
        new_msgs.append(sys_msg)
    new_msgs.append({"role": "system", "content": f"[Compressed context]: {summary}"})
    new_msgs.extend(recent)
    return new_msgs


def sanitize_content_for_llm(messages):
    """Return a COPY of ``messages`` with content parts the LLM backend cannot
    process (e.g. ``audio_url``) removed, so a stale multimodal message can't
    make llama-server reject the whole request (HTTP 400 "unsupported
    content[].type"). The stored session is left untouched.
    """
    sanitized = []
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            sanitized.append(msg)
            continue
        parts = []
        dropped_audio = False
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("audio_url", "input_audio", "audio"):
                dropped_audio = True
                continue
            parts.append(p)
        if dropped_audio:
            parts.append(
                {
                    "type": "text",
                    "text": "[Voice message omitted — audio input is not supported by this model]",
                }
            )
        sanitized.append({**msg, "content": parts})
    return sanitized


def resolve_image_path(url):
    """Resolve a ``/uploads/`` or ``/output/`` URL to a local file path, or
    ``None`` if the URL is unknown or the file is missing."""
    if not url:
        return None
    fpath = None
    if url.startswith("/uploads/"):
        fname = os.path.basename(url.split("?", 1)[0])
        fpath = os.path.join(M.UPLOADS_DIR, fname)
    elif url.startswith("/output/"):
        rel = url[len("/output/"):].split("?", 1)[0]
        fpath = os.path.join(M.IMG_PATH, rel)
    if not fpath or not os.path.isfile(fpath):
        return None
    return fpath


def _image_to_data_url(url):
    """Resolve an image URL the server can serve to a ``data:`` URL.

    Understands ``/uploads/`` (user uploads) and ``/output/`` (generated
    images) so those bytes can be embedded in the LLM request. ``data:`` URLs
    pass through unchanged; anything unknown returns ``None``.
    """
    if not url:
        return None
    if url.startswith("data:"):
        return url
    fpath = resolve_image_path(url)
    if not fpath:
        return None
    ext = os.path.splitext(fpath)[1].lower()
    mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(ext, "image/jpeg")
    try:
        with open(fpath, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def _reference_historical_images(messages):
    """Return a COPY of ``messages`` where ``image_url`` parts are kept lean.

    The image attached to the most recent user message stays visible (its bytes
    are embedded via ``_image_to_data_url``), preserving the current turn's
    vision. Every older image becomes a compact text marker carrying the file
    URL — the model can call ``read_image`` to actually view one, so history
    never drags megabytes of base64 through the context window.
    """
    last_user_idx = -1
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            last_user_idx = i

    out = []
    for i, m in enumerate(messages):
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "image_url":
                url = p.get("image_url", {}).get("url", "")
                if i == last_user_idx:
                    data_url = _image_to_data_url(url)
                    if data_url:
                        parts.append({"type": "image_url", "image_url": {"url": data_url}})
                        continue
                    parts.append({"type": "text", "text": f"[IMAGE: {url}]"})
                else:
                    parts.append(
                        {
                            "type": "text",
                            "text": f"[IMAGE: {url} — use the read_image tool to view this image]",
                        }
                    )
            else:
                parts.append(p)
        out.append({**m, "content": parts})
    return out


def _latest_read_image_url(messages):
    """Return the image URL of the most recent successful ``read_image`` tool
    result in ``messages`` (or ``None``)."""
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            continue
        if data.get("ok") is True and data.get("image_url"):
            return data["image_url"]
    return None


def prepare_context_for_llm(sid, messages, mode="gpu"):
    """Build the message list to send to the LLM. When the conversation nears the
    context limit, old messages are summarized into a compressed context block —
    but the stored session is left untouched, so no messages are deleted.
    Historical images are referenced by path (see ``read_image``) instead of
    being re-sent as base64 on every round."""
    messages = sanitize_content_for_llm(messages)
    messages = _reference_historical_images(messages)
    total = estimate_tokens(messages)
    if total <= M.AUTO_COMPACT_THRESHOLD:
        context = trim_messages_for_context(messages)
        with M._effective_contexts_lock:
            M._effective_contexts.pop(sid, None)
        return context
    # print(f"[context] Session {sid} estimate {total} tokens exceeds threshold {M.AUTO_COMPACT_THRESHOLD}; building compressed context for LLM")
    compacted = compact_messages_copy(messages, mode=mode)
    context = trim_messages_for_context(compacted)
    # print(f"[context] Compressed context built; estimate after: {estimate_tokens(context)}")
    with M._effective_contexts_lock:
        M._effective_contexts[sid] = context
    return context


def effective_token_estimate(sid, messages):
    """Report the token count the UI shows: the compressed context actually sent
    to the LLM once compression has kicked in, falling back to the full history."""
    with M._effective_contexts_lock:
        cached = M._effective_contexts.get(sid)
    if cached is not None:
        return estimate_tokens(cached)
    return estimate_tokens(messages)


def context_token_report(sid, messages):
    """Token report for the UI: effective count sent to the LLM, the raw stored
    count, and whether context compression is currently active."""
    effective = effective_token_estimate(sid, messages)
    raw = estimate_tokens(messages)
    return {
        "token_estimate": effective,
        "raw_token_estimate": raw,
        "context_compressed": raw > effective,
    }
