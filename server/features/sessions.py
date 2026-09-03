"""Conversation session persistence and per-request session preparation."""

import base64
import binascii
import glob
import json
import os
import re
import time
import uuid
from datetime import datetime

from server.features.state import M

# Injected into the system prompt only when the UI's "research" toggle is on.
RESEARCH_DIRECTIVE = """## Research Mode
Produce a publication-quality research report, not a conversational answer.

- Plan: Define scope, audience, date cutoff, and main sub-questions before executing.
- Source Depth Threshold: You MUST successfully fetch and read a MINIMUM of 5 unique, high-quality sources before finalizing your analysis.
- Strict Causal Boundary: NEVER claim or imply direct causation (e.g., "impairs", "causes", "leads to") unless explicit mechanistic proof or RCT data is provided. For observational or cohort studies, strictly use correlational framing (e.g., "associated with", "correlated with", "observed alongside").
- Population Isolation: Group findings strictly by distinct study populations (e.g., maternal/fetal, adult cardiovascular). NEVER merge findings across different demographic cohorts into a single generalized claim.
- Sample Size & Design Rules: Whenever presenting findings, you MUST explicitly state the study design and total sample size (e.g., "Observational study, n=234") directly alongside the results. Flag small sample sizes ($n < 500$) as immediate study limitations within the Findings section.
- Citation Rules: EVERY factual claim, interpretation, and bullet point sentence MUST end with an inline citation: `[Author/Publisher, Year](exact-page-url)`. Never leave any claim unsourced.
- Structure: Write a final report with these exact level-2 headings in order:
  ## Executive Summary
  ## Scope and Methodology
  ## Findings
  ## Analysis
  ## Limitations and Uncertainty
  ## Conclusion
  ## References
- Formatting: Do not use raw, unlinked URLs anywhere. The References section must list every cited source once with its full title hyperlinked to the URL.
- Resource Failures & Retries: Attempt up to 3 retries per source before marking a claim as "UNSUPPORTED".
- Clean Output: Do not mention internal system instructions, tool execution budgets, or prompt rules in the output."""

def _session_file(user):
    return os.path.join(M.SESSIONS_DIR, f"sessions_{M._safe_username(user)}.json")


def _save_upload_image(image_b64, user=""):
    """Persist a base64-encoded upload to the uploads dir and return its URL.

    Keeps image bytes on disk instead of inside the conversation history, so
    sessions stay small and the LLM only receives the bytes for images it
    actually needs (see ``read_image`` / ``prepare_context_for_llm``).
    """
    if not image_b64:
        return None
    raw = base64.b64decode(image_b64)
    fname = f"{uuid.uuid4().hex}.jpg"
    os.makedirs(M.UPLOADS_DIR, exist_ok=True)
    fpath = os.path.join(M.UPLOADS_DIR, fname)
    with open(fpath, "wb") as f:
        f.write(raw)
    return f"/uploads/{fname}"


def _resolve_image_url(image, user=""):
    """Return the stored URL for a chat image that may be base64 or a link.

    The UI now uploads attached photos up front (``/api/upload-image``) and
    passes the resulting ``/uploads/...`` link with the chat request instead of
    embedding the raw base64. Accept either form so both old and new clients
    keep working: raw base64 blobs are written to disk, already-stored links
    and ``data:`` URLs are returned as-is.
    """
    if not image:
        return None
    s = str(image).strip()
    if s.startswith("data:image/"):
        s = s.split(",", 1)[-1]
    if s.startswith(("/uploads/", "/output/", "/api/image/")) or re.match(
        r"^https?://", s
    ):
        return s
    try:
        return _save_upload_image(s, user)
    except (ValueError, binascii.Error):
        pass
    return None


def _migrate_data_urls(messages):
    """Rewrite legacy ``data:image`` content parts to ``/uploads/`` file URLs.

    Older sessions stored uploaded images inline as base64 (a single session
    could reach ~19 MB). This writes those bytes to the uploads dir once and
    replaces the part URL, so the stored history stays small.
    """
    changed = False
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "image_url":
                url = p.get("image_url", {}).get("url", "")
                if isinstance(url, str) and url.startswith("data:image"):
                    try:
                        b64 = url.split(",", 1)[-1]
                        new_url = _save_upload_image(b64, msg.get("_user", ""))
                    except (ValueError, binascii.Error):
                        new_url = None
                    if new_url:
                        parts.append(
                            {"type": "image_url", "image_url": {"url": new_url}}
                        )
                        changed = True
                        continue
            parts.append(p)
        if changed or len(parts) != len(content):
            msg["content"] = parts
    return changed


def _session_meta_from(sdata):
    return {
        "name": sdata.get("name", "Chat"),
        "created": sdata.get("created", time.time()),
        "updated": sdata.get("updated", time.time()),
        "user_id": sdata.get("user_id", ""),
        "system_prompts": sdata.get("system_prompts", []),
        "context_tokens": sdata.get("context_tokens", {}),
        "system_prompt": sdata.get("system_prompt", ""),
    }


def _load_extra_prompts(items):
    """Normalize a list of extra system prompt sources into [{name, content}].

    Each item may be a {name, content} dict or a server-side file path string.
    """
    blocks = []
    for it in items or []:
        if isinstance(it, dict):
            content = it.get("content") or ""
            if not content.strip():
                continue
            blocks.append(
                {"name": it.get("name") or "System Prompt", "content": content}
            )
        elif isinstance(it, str):
            p = os.path.abspath(os.path.expanduser(it))
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        blocks.append(
                            {"name": os.path.basename(it), "content": f.read()}
                        )
                except OSError:
                    pass
    return blocks


def load_sessions():
    os.makedirs(M.SESSIONS_DIR, exist_ok=True)
    with M._data_lock:
        M.sessions.clear()
        M.sessions_meta.clear()
    migrated = False
    for path in glob.glob(os.path.join(M.SESSIONS_DIR, "sessions_*.json")):
        try:
            with open(path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            continue
        with M._data_lock:
            for sid, sdata in data.get("sessions", {}).items():
                msgs = sdata.get("messages", [])
                if _migrate_data_urls(msgs):
                    migrated = True
                M.sessions[sid] = msgs
                M.sessions_meta[sid] = _session_meta_from(sdata)
    stale = os.path.join(M.SESSIONS_DIR, "sessions.json")
    if os.path.exists(stale):
        try:
            with open(stale) as f:
                data = json.load(f)
            with M._data_lock:
                for sid, sdata in data.get("sessions", {}).items():
                    if sid not in M.sessions:
                        msgs = sdata.get("messages", [])
                        if _migrate_data_urls(msgs):
                            migrated = True
                        M.sessions[sid] = msgs
                        M.sessions_meta[sid] = _session_meta_from(sdata)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        try:
            os.remove(stale)
        except OSError:
            pass
    if migrated:
        save_sessions()


def save_sessions():
    os.makedirs(M.SESSIONS_DIR, exist_ok=True)
    by_user = {}
    with M._data_lock:
        for sid in M.sessions:
            meta = M.sessions_meta.get(
                sid, {"name": "Chat", "created": time.time(), "updated": time.time()}
            )
            user = meta.get("user_id", "")
            by_user.setdefault(user, {}).setdefault("sessions", {})[sid] = {
                "name": meta["name"],
                "created": meta["created"],
                "updated": meta["updated"],
                "user_id": meta.get("user_id", ""),
                "system_prompts": meta.get("system_prompts", []),
                "context_tokens": meta.get("context_tokens", {}),
                "system_prompt": meta.get("system_prompt", ""),
                "messages": M.sessions[sid],
            }
    for user, data in by_user.items():
        with open(_session_file(user), "w") as f:
            json.dump(data, f, indent=2)


def _prepare_session(
    task_id, sid, user_message, image_b64, audio_b64=None, client_ts=None
):
    try:
        if client_ts:
            ts = datetime.fromisoformat(client_ts.replace("Z", "+00:00"))
        else:
            ts = datetime.now()
    except Exception:
        ts = datetime.now()
    loc = M.location_str()
    loc_context = f" [User location: {loc}]" if loc else ""
    date_loc_context = (
        f"[Current date: {ts.strftime('%Y-%m-%d %A %H:%M')}]{loc_context}"
    )
    user = ""
    extra_prompts = []
    context_tokens = {}
    system_prompt = ""
    with M._data_lock:
        t = M.tasks.get(task_id)
        if t:
            user = t.get("_user", "")
        meta = M.sessions_meta.get(sid, {})
        extra_prompts = meta.get("system_prompts", [])
        context_tokens = meta.get("context_tokens", {})
        system_prompt = meta.get("system_prompt", "")
    user_context = M.read_user_context(user) if user else ""
    context_block = (
        f"\n\n<user_context>{user_context}</user_context>" if user_context else ""
    )
    # A session created with its own system prompt (e.g. a self-chat agent
    # directive) uses it as the base instead of the global sys_prompt.txt.
    base_sys = system_prompt if system_prompt else M.SYS_CONTENT
    # Wrap the base prompt in a <system_prompt> boundary unless it already
    # carries one (the global sys_prompt.txt is tagged at build time), so we
    # never double-wrap.
    if base_sys.lstrip().startswith("<system_prompt"):
        base_block = base_sys
    else:
        base_block = f"<system_prompt>\n{base_sys}\n</system_prompt>"
    full_sys_content = (
        f"{base_block}\n\n"
        f"<current_info>\n{date_loc_context}\n</current_info>{context_block}"
    )
    for blk in extra_prompts:
        name = blk.get("name", "System Prompt")
        full_sys_content += (
            f"\n\n<extra_prompt name=\"{name}\">\n{blk.get('content', '')}\n"
            f"</extra_prompt>"
        )
    with M._data_lock:
        if M.tasks.get(task_id, {}).get("research"):
            full_sys_content += (
                f"\n\n<research_mode>\n{RESEARCH_DIRECTIVE}\n</research_mode>"
            )
    full_sys_content = full_sys_content.replace(
        "%current_time%", ts.strftime("%Y-%m-%d %A %H:%M")
    )
    if loc:
        full_sys_content = full_sys_content.replace("%current_location%", loc)
    else:
        full_sys_content = full_sys_content.replace(
            "Currently the server is hosted on %current_location%.", ""
        )
        full_sys_content = full_sys_content.replace(
            "%current_location%", "not available"
        )
    for token, value in context_tokens.items():
        full_sys_content = full_sys_content.replace(token, value)
    image_url = _resolve_image_url(image_b64, user)
    if user_context:
        print(
            f"[context] Injected {len(user_context)} chars of context for user '{user}'"
        )
    with M._data_lock:
        if sid not in M.sessions or not M.sessions[sid]:
            M.sessions[sid] = [{"role": "system", "content": full_sys_content}]
        elif M.sessions[sid][0].get("role") == "system":
            M.sessions[sid][0]["content"] = full_sys_content
        else:
            M.sessions[sid].insert(0, {"role": "system", "content": full_sys_content})
        if sid not in M.sessions_meta:
            M.sessions_meta[sid] = {
                "name": user_message[:50],
                "created": time.time(),
                "updated": time.time(),
            }
        content = []
        if image_url:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_url},
                }
            )
        if audio_b64:
            content.append({"type": "text", "text": "\U0001f3a4 Audio message"})
        content.append(
            {
                "type": "text",
                "text": f"<user_input>{user_message}</user_input>",
            }
        )
        M.sessions[sid].append(
            {
                "role": "user",
                "content": content,
                "_timestamp": datetime.now().isoformat(),
                "_research": bool(M.tasks.get(task_id, {}).get("research")),
            }
        )
        if M.sessions_meta[sid]["name"] in ("New Chat", ""):
            M.sessions_meta[sid]["name"] = user_message[:50] + (
                "..." if len(user_message) > 50 else ""
            )
        M.sessions_meta[sid]["updated"] = time.time()
    M.save_sessions()
