"""HTTP API endpoints for the chat web UI.

The request handlers here are the thin web layer over the chat engine in
``chat-webui.py``. All shared application state and helper functions live in the
entrypoint module and are injected here at startup via :func:`set_app_state`,
so nothing has to be duplicated.

The names in ``APP_STATE_NAMES`` are declared as module globals below and
replaced by the real objects when the entrypoint calls ``set_app_state``.
"""
import base64
import http.server
import json
import mimetypes
import os
import re
import time
import traceback
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests

from server.config import (
    COMFYUI_OUTPUT,
    FORCE_GPU_LANE,
    IMG_PATH,
    SELF_CHAT_MODE,
    UPLOADS_DIR,
)

# ---------------------------------------------------------------------------
# Shared application state — injected by chat-webui.py via set_app_state().
# ---------------------------------------------------------------------------

ACTIVE_WINDOW_SECONDS = None
MAX_INPUT_TOKENS = None
MAX_QUEUE_SIZE = None
SHARES_FILE = None
_active_tokens = None
_agent_tokens = None
_agent_users = None
_data_lock = None
_db_fetch = None
_effective_contexts = None
_effective_contexts_lock = None
_image_url_rel = None
_load_extra_prompts = None
_location_events = None
_queue_conds = None
_queue_locks = None
_task_queues = None
_tokens_lock = None
context_token_report = None
create_share = None
get_current_user = None
get_share = None
get_user_context_path = None
get_user_password = None
handle_theme_tool = None
list_shares = None
load_shares = None
model_status_snapshot = None
read_user_context = None
revoke_share = None
save_sessions = None
save_shares = None
sessions = None
sessions_meta = None
set_client_location = None
shares = None
task_create = None
task_delete = None
task_list = None
task_update = None
tasks = None
write_user_context = None

APP_STATE_NAMES = [
    "ACTIVE_WINDOW_SECONDS",
    "MAX_INPUT_TOKENS",
    "MAX_QUEUE_SIZE",
    "SHARES_FILE",
    "_active_tokens",
    "_agent_tokens",
    "_agent_users",
    "_data_lock",
    "_db_fetch",
    "_effective_contexts",
    "_effective_contexts_lock",
    "_image_url_rel",
    "_load_extra_prompts",
    "_location_events",
    "_queue_conds",
    "_queue_locks",
    "_task_queues",
    "_tokens_lock",
    "context_token_report",
    "create_share",
    "get_current_user",
    "get_share",
    "get_user_context_path",
    "get_user_password",
    "handle_theme_tool",
    "list_shares",
    "load_shares",
    "model_status_snapshot",
    "read_user_context",
    "revoke_share",
    "save_sessions",
    "save_shares",
    "sessions",
    "sessions_meta",
    "set_client_location",
    "shares",
    "task_create",
    "task_delete",
    "task_list",
    "task_update",
    "tasks",
    "write_user_context",
]


def set_app_state(state):
    """Inject the application state shared with the entrypoint module."""
    globals().update(state)


def read_index_html():
    p = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "dist",
        "index.html",
    )
    try:
        with open(p) as f:
            return f.read()
    except:
        return "<html><body><h1>index.html missing</h1></body></html>"


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Auth-Token")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/user-context":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            context = read_user_context(user)
            self.send_json(
                {
                    "context": context,
                    "username": user,
                    "context_file": get_user_context_path(user),
                }
            )
        elif self.path == "/api/check-auth":
            user = get_current_user(self.headers)
            if user:
                self.send_json({"authenticated": True, "username": user})
            else:
                self.send_json({"authenticated": False})
        elif self.path == "/api/active-users":
            with _tokens_lock:
                now = time.time()
                active_users = set()
                for token, entry in _active_tokens.items():
                    if token in _agent_tokens or entry["user"] in _agent_users:
                        continue
                    if now - entry["last_seen"] <= ACTIVE_WINDOW_SECONDS:
                        active_users.add(entry["user"])
                active = sorted(active_users)
            self.send_json({"users": active})
        elif self.path == "/api/model-status":
            snap = model_status_snapshot()
            ms, tps, oh, gtemp, ram_evac = (
                snap["model"],
                snap["predicted_per_second"],
                snap["overheated"],
                snap["gpu_temp"],
                snap["ram_evacuating"],
            )
            try:
                user = get_current_user(self.headers)
                reminder_count = len(_db_fetch("SELECT id FROM tasks WHERE user_id=? AND reminder_at IS NOT NULL AND reminder_at <= ? AND reminded=0 AND status NOT IN ('completed','cancelled')", (user, datetime.now().isoformat()))) if user else 0
            except Exception:
                reminder_count = 0
            self.send_json(
                {
                    "model": ms,
                    "predicted_per_second": tps,
                    "overheated": oh,
                    "gpu_temp": gtemp,
                    "ram_evacuating": ram_evac,
                    "max_context": MAX_INPUT_TOKENS,
                    "reminder_count": reminder_count,
                }
            )
        elif self.path == "/api/shares":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            self.send_json({"shares": list_shares(user)})
        elif self.path.startswith("/api/public/share/"):
            token = os.path.basename(self.path)
            rec = get_share(token)
            if not rec:
                self.send_error(404)
                return
            self.send_json(
                {
                    "message": rec.get("message", {}),
                    "created": rec.get("created"),
                    "shared_by": rec.get("owner", ""),
                }
            )
        elif self.path.startswith("/output/"):
            rel = urlparse(self.path).path
            rel = rel[len("/output/"):] if rel.startswith("/output/") else rel
            fpath = os.path.abspath(os.path.join(COMFYUI_OUTPUT, rel))
            if fpath.startswith(os.path.abspath(COMFYUI_OUTPUT)) and os.path.exists(
                fpath
            ):
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self._safe_write(f.read())
                return
            self.send_error(404)
        elif self.path.startswith("/uploads/"):
            filename = os.path.basename(urlparse(self.path).path)
            fpath = os.path.abspath(os.path.join(UPLOADS_DIR, filename))
            if fpath.startswith(os.path.abspath(UPLOADS_DIR)) and os.path.exists(fpath):
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", "inline")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self._safe_write(f.read())
                return
            self.send_error(404)
        elif self.path.startswith("/api/status/"):
            task_id = os.path.basename(self.path)
            with _data_lock:
                status = tasks.get(
                    task_id, {"status": "unknown", "message": "Not found"}
                )
            self.send_json(status)
        elif self.path == "/api/sessions":
            user = get_current_user(self.headers)
            if not user:
                self.send_json([], status=401)
                return
            with _data_lock:
                sorted_items = sorted(
                    sessions_meta.items(),
                    key=lambda x: x[1].get("updated", 0),
                    reverse=True,
                )
                result = [
                    {
                        "session_id": sid,
                        "name": meta.get("name", "Chat"),
                        "created": meta.get("created", 0),
                        "updated": meta.get("updated", 0),
                        **context_token_report(sid, sessions.get(sid, [])),
                    }
                    for sid, meta in sorted_items
                    if meta.get("user_id", "") == user
                ]
            self.send_json(result)
        elif self.path.startswith("/api/sessions/") and self.path.endswith("/messages"):
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            sid = self.path.split("/")[3]
            with _data_lock:
                meta = sessions_meta.get(sid)
                if not meta or meta.get("user_id", "") != user:
                    self.send_error(404)
                    return
                msgs = sessions.get(sid)
            if msgs is not None:
                self.send_json(
                    {
                        "messages": msgs,
                        **context_token_report(sid, msgs),
                    }
                )
            else:
                self.send_error(404)
        elif self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._safe_write(read_index_html().encode())
        else:
            DIST_DIR = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist"
            )
            fpath = os.path.abspath(os.path.join(DIST_DIR, self.path.lstrip("/")))
            if fpath.startswith(os.path.abspath(DIST_DIR)) and os.path.isfile(fpath):
                ctype, _ = mimetypes.guess_type(fpath)
                self.send_response(200)
                self.send_header("Content-Type", ctype or "application/octet-stream")
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self._safe_write(f.read())
            elif self.path.startswith("/api/") or "." in os.path.basename(self.path):
                if self.path == "/api/tasks":
                    user = get_current_user(self.headers)
                    if not user:
                        self.send_json({"error": "Unauthorized"}, status=401)
                        return
                    user_tasks = task_list(user)
                    self.send_json({"tasks": user_tasks})
                elif self.path.startswith("/api/themes"):
                    user = get_current_user(self.headers)
                    if not user:
                        self.send_json({"error": "Unauthorized"}, status=401)
                        return
                    qs = parse_qs(urlparse(self.path).query)
                    scope = qs.get("scope", [""])[0] or None
                    result = handle_theme_tool(
                        user,
                        {
                            "operation": "list",
                            "scope": scope,
                            "global": qs.get("global", ["0"])[0] in ("1", "true", "True", "yes"),
                            "status": qs.get("status", [""])[0] or None,
                            "limit": qs.get("limit", ["50"])[0] or 50,
                        },
                    )
                    self.send_json(json.loads(result))
                else:
                    self.send_error(404)
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self._safe_write(read_index_html().encode())

    def do_DELETE(self):
        if self.path.startswith("/api/shares/"):
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            token = self.path.split("/")[3]
            if revoke_share(token, user):
                self.send_json({"status": "revoked"})
            else:
                self.send_json({"error": "Share not found or not yours"}, status=404)
        elif self.path.startswith("/api/sessions/"):
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            sid = self.path.split("/")[3]
            with _data_lock:
                meta = sessions_meta.get(sid)
                if not meta or meta.get("user_id", "") != user:
                    self.send_error(404)
                    return
                msgs = list(sessions.get(sid, []))
            # Cancel all queued/in-flight tasks for this session so they stop processing
            for mode in ("gpu", "cpu"):
                with _queue_locks[mode]:
                    q = _task_queues[mode]
                    q[:] = [item for item in q if item.get("session_id") != sid]
            with _data_lock:
                for tid, t in tasks.items():
                    if t.get("session_id") == sid and t.get("status") not in ("done", "error"):
                        tasks[tid] = {
                            "status": "cancelled",
                            "error": "Session was deleted",
                            "session_id": sid,
                        }
            for msg in msgs:
                if msg.get("role") == "assistant":
                    url = msg.get("_image_url", "") or ""
                    if url:
                        fname = os.path.join(IMG_PATH, _image_url_rel(url))
                        fpath = fname
                        if os.path.exists(fpath):
                            print(f"[delete] Removed output image: {fpath}")
                            os.remove(fpath)
                raw = msg.get("content", "")
                texts = []
                if isinstance(raw, str):
                    texts.append(raw)
                elif isinstance(raw, list):
                    for part in raw:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(part.get("text", ""))
                for text in texts:
                    for part in text.split("[FILE:"):
                        idx = part.find("/uploads/")
                        if idx != -1:
                            url_part = part[idx:].split("]")[0]
                            fname = os.path.basename(url_part)
                            fpath = os.path.join(UPLOADS_DIR, fname)
                            if os.path.exists(fpath):
                                print(f"[delete] Removed uploaded file: {fpath}")
                                os.remove(fpath)
            # Remove image uploads stored as image_url content parts (the
            # /uploads/ URLs written by _save_upload_image).
            for msg in msgs:
                raw = msg.get("content", "")
                if not isinstance(raw, list):
                    continue
                for part in raw:
                    if not isinstance(part, dict) or part.get("type") != "image_url":
                        continue
                    url = part.get("image_url", {}).get("url", "")
                    if not url.startswith("/uploads/"):
                        continue
                    fname = os.path.basename(url.split("?", 1)[0])
                    fpath = os.path.join(UPLOADS_DIR, fname)
                    if os.path.exists(fpath):
                        print(f"[delete] Removed uploaded image: {fpath}")
                        os.remove(fpath)

            with _data_lock:
                exists = sid in sessions
                if exists:
                    sessions.pop(sid, None)
                    sessions_meta.pop(sid, None)
            if exists:
                with _effective_contexts_lock:
                    _effective_contexts.pop(sid, None)
            if exists:
                save_sessions()
                self.send_json({"status": "deleted"})
            else:
                self.send_error(404)
        elif self.path.startswith("/api/tasks/"):
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            tid = self.path.split("/")[3]
            if task_delete(tid, user):
                self.send_json({"status": "deleted"})
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_PUT(self):
        if self.path.startswith("/api/sessions/"):
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            sid = self.path.split("/")[3]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            with _data_lock:
                meta = sessions_meta.get(sid)
                if meta and meta.get("user_id", "") == user:
                    meta["name"] = body.get("name", meta["name"])
                    meta["updated"] = time.time()
            if meta:
                save_sessions()
                self.send_json({"status": "updated"})
            else:
                self.send_error(404)
        elif self.path.startswith("/api/tasks/"):
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            tid = self.path.split("/")[3]
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            t = task_update(tid, user, **{k: v for k, v in body.items() if k in ("title","description","status","priority","due_date","reminder_at")})
            if t:
                self.send_json({"task": t})
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/shares":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            try:
                token, url = create_share(
                    user, body.get("session_id", ""), body.get("msg_index")
                )
            except ValueError as e:
                self.send_json({"error": str(e)}, status=400)
                return
            self.send_json({"token": token, "url": url})
        elif self.path == "/api/register-agent":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
            tokens = body.get("tokens", []) or []
            usernames = body.get("usernames", []) or []
            print("Agent registered")
            with _tokens_lock:
                for t in tokens:
                    _agent_tokens.add(t)
                for u in usernames:
                    _agent_users.add(u)
            self.send_json({"ok": True})
        elif self.path == "/api/login":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            username = body.get("username", "").strip()
            password = body.get("password", "").strip()
            if get_user_password(username) == password:
                token = str(uuid.uuid4())
                with _tokens_lock:
                    _active_tokens[token] = {"user": username, "last_seen": time.time()}
                self.send_json(
                    {
                        "token": token,
                        "username": username,
                        "context_file": get_user_context_path(username),
                    }
                )
            else:
                self.send_json({"error": "Invalid credentials"}, status=401)
        elif self.path == "/api/leaving":
            # Fired via navigator.sendBeacon on pagehide — beacons can't set
            # custom headers, so the token travels in the body instead of
            # X-Auth-Token. This does NOT invalidate the token (unlike
            # /api/logout) — it just marks the heartbeat stale so
            # _human_priority_active() sees this user as gone right away,
            # instead of waiting out the full ACTIVE_WINDOW_SECONDS timeout.
            # The timeout stays in place as a fallback for crashes/force-quits
            # where pagehide never fires.
            token = self.headers.get("X-Auth-Token", "")
            if not token:
                length = int(self.headers.get("Content-Length", 0))
                if length:
                    try:
                        body = json.loads(self.rfile.read(length))
                        token = body.get("token", "")
                    except Exception:
                        token = ""
            with _tokens_lock:
                entry = _active_tokens.get(token)
                if entry:
                    entry["last_seen"] = 0
            self.send_json({"ok": True})
        elif self.path == "/api/logout":
            token = self.headers.get("X-Auth-Token", "")
            with _tokens_lock:
                _active_tokens.pop(token, None)
            self.send_json({"ok": True})
        elif self.path == "/api/user-context":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            action = body.get("action", "read")
            if action == "write":
                content = body.get("context", "")
                write_user_context(user, content)
                self.send_json({"status": "ok", "username": user})
            elif action == "overwrite":
                content = body.get("context", "")
                path = get_user_context_path(user)
                if path:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as f:
                        f.write(content)
                self.send_json({"status": "ok", "username": user})
            else:
                context = read_user_context(user)
                self.send_json(
                    {
                        "context": context,
                        "username": user,
                        "context_file": get_user_context_path(user),
                    }
                )
        elif self.path == "/api/chat":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            task_id = str(uuid.uuid4())
            sid = body.get("session_id", "default")
            with _data_lock:
                meta = sessions_meta.get(sid)
                if not meta or meta.get("user_id", "") != user:
                    self.send_json({"error": "Session not found"}, status=404)
                    return

            entry = {
                "task_id": task_id,
                "session_id": sid,
                "message": body.get("message", ""),
                "image": body.get("image"),
                "audio": body.get("audio"),
                "user": user,
                "client_timestamp": body.get("client_timestamp"),
            }
            # Route to the GPU lane (interactive UI users) or the lane chosen
            # by SELF_CHAT_MODE — cpu (self-chat agents on the RAM-backed CPU
            # server) or gpu (agents sharing the interactive GPU server) — so
            # the two never wait behind each other. Agent users may override
            # the lane per request (self-chat.py --gpu sends mode="gpu"); the
            # override is ignored for interactive users, who always use GPU.
            mode = body.get("mode")
            if mode not in ("gpu", "cpu") or user not in _agent_users:
                mode = SELF_CHAT_MODE if user in _agent_users else "gpu"
            if FORCE_GPU_LANE:
                # Test-time override: never admit anything to the CPU lane.
                mode = "gpu"
            entry["mode"] = mode
            with _queue_locks[mode]:
                if len(_task_queues[mode]) >= MAX_QUEUE_SIZE:
                    self.send_json({"error": "Server busy"}, status=503)
                    return
                _task_queues[mode].append(entry)
                _queue_conds[mode].notify()
            with _data_lock:
                tasks[task_id] = {
                    "status": "queued",
                    "message": "Waiting in line...",
                    "session_id": sid,
                    "mode": mode,
                }
            self.send_json({"task_id": task_id})
        elif self.path == "/api/extract-file":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            name = body.get("name", "")
            data_b64 = body.get("data", "")
            ext = os.path.splitext(name)[1].lower()
            safe_name = str(uuid.uuid4()) + ext
            filepath = os.path.join(UPLOADS_DIR, safe_name)
            raw = base64.b64decode(data_b64)
            with open(filepath, "wb") as f:
                f.write(raw)
            file_url = f"/uploads/{safe_name}"
            self.send_json({"url": file_url, "name": name})
        elif self.path == "/api/tts":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            raw_text = body.get("text", "")
            if not raw_text:
                self.send_json({"error": "No text provided"}, status=400)
                return
            try:
                import re
                text = raw_text
                voice = body.get("voice", "")

                # Detect language tag from LLM prefix: [bn], [hi], [te], [kn], [en]
                m = re.match(r"^\s*\[(bn|hi|te|kn|en)\]\s*", text)
                if m:
                    tag = m.group(1)
                    text = text[m.end():]
                elif not voice:
                    bn = len(re.findall(r"[\u0980-\u09FF]", text))
                    hi = len(re.findall(r"[\u0900-\u097F]", text))
                    te = len(re.findall(r"[\u0C00-\u0C7F]", text))
                    kn = len(re.findall(r"[\u0C80-\u0CFF]", text))
                    scores = {"bn": bn, "hi": hi, "te": te, "kn": kn}
                    tag = max(scores, key=scores.get)
                    if scores[tag] == 0:
                        tag = "en"
                else:
                    tag = "en"

                # Determine TTS backend
                PIPER_VOICES = {
                    "bn": "/home/palash/.piper_voices/bn_BD-google-medium.onnx",
                    "hi": "/home/palash/.piper_voices/hi_IN-priyamvada-medium.onnx",
                    "te": "/home/palash/.piper_voices/te_IN-padmavathi-medium.onnx",
                    "en": "/home/palash/.piper_voices/en_US-amy-medium.onnx",
                }
                EDGE_VOICES = {
                    "bn": "bn-IN-TanishaaNeural",
                    "hi": "hi-IN-SwaraNeural",
                    "te": "te-IN-ShrutiNeural",
                    "kn": "kn-IN-GaganNeural",
                    "en": "en-US-AriaNeural",
                }

                if tag in PIPER_VOICES:
                    import piper, io, struct, wave
                    onnx_path = PIPER_VOICES[tag]
                    cfg_path = onnx_path + ".json"
                    if not hasattr(self, "_piper_voices"):
                        self._piper_voices = {}
                    if tag not in self._piper_voices:
                        print(f"[tts] Loading Piper voice '{tag}' ...")
                        self._piper_voices[tag] = piper.PiperVoice.load(
                            onnx_path, config_path=cfg_path
                        )
                    pv = self._piper_voices[tag]
                    print(f"[tts] Piper {tag}: synthesizing {len(text)} chars")
                    wav_io = io.BytesIO()
                    with wave.open(wav_io, "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(22050)
                        for chunk in pv.synthesize(text):
                            int16 = (chunk.audio_float_array * 32767).clip(-32768, 32767).astype("<i2")
                            wf.writeframes(int16.tobytes())
                    audio_b64 = base64.b64encode(wav_io.getvalue()).decode()
                    self.send_json({"audio": audio_b64, "type": "audio/wav"})
                else:
                    import asyncio, edge_tts
                    edge_voice = voice or EDGE_VOICES.get(tag, "en-US-AriaNeural")
                    print(f"[tts] edge-tts {tag} ({edge_voice}): {len(text)} chars")
                    communicate = edge_tts.Communicate(text, edge_voice)
                    mp3_data = bytearray()
                    async def _gen():
                        async for chunk in communicate.stream():
                            if chunk["type"] == "audio":
                                mp3_data.extend(chunk["data"])
                    asyncio.run(_gen())
                    audio_b64 = base64.b64encode(bytes(mp3_data)).decode()
                    self.send_json({"audio": audio_b64, "type": "audio/mpeg"})
            except Exception as e:
                print(f"[tts] Error: {e}")
                traceback.print_exc()
                self.send_json({"error": str(e)}, status=500)
        elif self.path == "/api/sessions":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            extra = {}
            context_tokens = {}
            if length:
                try:
                    ext_body = json.loads(self.rfile.read(length))
                    extra = _load_extra_prompts(ext_body.get("system_prompts") or [])
                    context_tokens = ext_body.get("context_tokens") or {}
                except Exception:
                    extra = []
            sid = str(uuid.uuid4())
            now = time.time()
            with _data_lock:
                sessions[sid] = []
                sessions_meta[sid] = {
                    "name": "New Chat",
                    "created": now,
                    "updated": now,
                    "user_id": user,
                    "system_prompts": extra,
                    "context_tokens": context_tokens,
                }
            save_sessions()
            self.send_json({"session_id": sid})
        elif self.path == "/api/location":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            task_id = body.get("task_id")
            if body.get("denied"):
                set_client_location("")
                ev = _location_events.get(task_id) if task_id else None
                if ev:
                    ev.set()
                self.send_json({"ok": True})
                return
            lat = body.get("latitude")
            lng = body.get("longitude")
            if lat is not None and lng is not None:
                try:
                    geo = requests.get(
                        "https://nominatim.openstreetmap.org/reverse",
                        params={"format": "json", "lat": lat, "lon": lng},
                        headers={"User-Agent": "LocalAI/1.0"},
                        timeout=5,
                    ).json()
                    display = geo.get("display_name", "")
                    set_client_location(display)
                except Exception:
                    set_client_location(f"{lat:.4f}, {lng:.4f}")
            ev = _location_events.get(task_id) if task_id else None
            if ev:
                ev.set()
            self.send_json({"ok": True})
        elif self.path == "/api/tasks":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            t = task_create(user, body.get("title", "Untitled"), body.get("description", ""), body.get("priority", "medium"), body.get("due_date"), body.get("session_id"), body.get("reminder_at"))
            self.send_json({"task": t})
        elif self.path == "/api/themes":
            user = get_current_user(self.headers)
            if not user:
                self.send_json({"error": "Unauthorized"}, status=401)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            result = handle_theme_tool(user, body)
            self.send_json(json.loads(result))
        else:
            self.send_error(404)

    def _safe_write(self, data):
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._safe_write(json.dumps(data).encode())

    def log_message(self, format, *args):
        pass

async def handle_chat(user_message):
    # Run both LLM calls in parallel to prevent waiting
    user_response, bot_response = await asyncio.gather(
        llm_call_user(user_message),
        llm_call_bot(user_message)
    )
    return user_response, bot_response