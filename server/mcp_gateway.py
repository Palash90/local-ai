"""MCP gateway exposing a narrow chat-oriented surface of the local-ai API.

All tools act as the single configured identity (``MCP_USER``): the gateway
authenticates inbound clients via OAuth client_credentials
(``MCP_OAUTH_CLIENT_ID``/``MCP_OAUTH_CLIENT_SECRET``) and then talks to the
main API on loopback using a self-refreshing Authentik OIDC access token
obtained via a password grant.

Intended workflow for an MCP client (e.g. Claude):
1. ``list_sessions`` to discover existing conversations (or ``create_session``)
2. optionally ``get_user_context`` / ``get_session_messages`` for background
3. ``send_chat_message`` to submit a user message — this is ASYNCHRONOUS and
   returns a task id
4. poll ``get_message_status`` until the task reaches a terminal state
5. ``get_session_messages`` again to read the assistant's reply
"""

import os, sys, json, time, uuid, asyncio, threading, base64, httpx
from mcp.server.fastmcp import FastMCP, Image, Context
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, Mount
from contextlib import asynccontextmanager
import uvicorn

try:
    from server.auth import identity_from_bearer, oidc_password_grant
    from server.batches_db import (
        PENDING as BATCH_PENDING,
        WORKING as BATCH_WORKING,
        COMPLETED as BATCH_COMPLETED,
        ERROR as BATCH_ERROR,
        batch_get,
        batch_insert,
        claim_next_pending,
        fail_open_items,
        finish_batch,
        init_batches_db,
        item_update,
        pending_count,
        prune_batches,
        queue_position,
        requeue_stuck_batches,
    )
    from server.input_guard import (
        GUARDRAIL_DECLINE,
        HARMFUL_DECLINE,
        is_harmful_content,
        is_strict_output_blocked,
        llm_classify_harmful,
        llm_classify_harmful_output,
        is_harmful_request,
        is_jailbreak_attempt,
        mcp_output_judge,
        wrap_user_message,
    )
    from server.mcp_tasks_db import (
        mcp_task_insert,
        mcp_task_update,
        mcp_task_get,
        mcp_task_list,
    )
    from server.features.state import (
        _data_lock as _data_lock,
    )
    from server.config import SELF_CHAT_MODE
    from server.features.judge import resolve_judge_model
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from server.auth import identity_from_bearer, oidc_password_grant
    from server.batches_db import (
        PENDING as BATCH_PENDING,
        WORKING as BATCH_WORKING,
        COMPLETED as BATCH_COMPLETED,
        ERROR as BATCH_ERROR,
        batch_get,
        batch_insert,
        claim_next_pending,
        fail_open_items,
        finish_batch,
        init_batches_db,
        item_update,
        pending_count,
        prune_batches,
        queue_position,
        requeue_stuck_batches,
    )
    from server.input_guard import (
        GUARDRAIL_DECLINE,
        HARMFUL_DECLINE,
        is_harmful_content,
        is_strict_output_blocked,
        llm_classify_harmful,
        llm_classify_harmful_output,
        is_harmful_request,
        is_jailbreak_attempt,
        mcp_output_judge,
        wrap_user_message,
    )
    from server.mcp_tasks_db import (
        mcp_task_insert,
        mcp_task_update,
        mcp_task_get,
        mcp_task_list,
    )
    from server.features.state import (
        _data_lock as _data_lock,
    )
    from server.config import SELF_CHAT_MODE
    from server.features.judge import resolve_judge_model

API_BASE = os.environ.get("CHAT_API_BASE", "http://127.0.0.1:3001")

JUDGE_FAIL_CLOSED = os.environ.get("GUARD_FAIL_CLOSED", "1").lower() not in (
    "0", "false", "no",
)
MCP_ALLOWED_USERS = [
    u.strip() for u in os.environ.get("MCP_ALLOWED_USERS", "").split(",") if u.strip()
]
MCP_USER = os.environ.get("MCP_USER", "")
MCP_USER_PASSWORD = os.environ.get("MCP_USER_PASSWORD", "")
MCP_OAUTH_CLIENT_ID = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
MCP_OAUTH_CLIENT_SECRET = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "")
TOKEN_REFRESH_MARGIN = 60

mcp = FastMCP("chat-webui-api", stateless_http=True)


class EnforcementAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if scope.get("method") == "OPTIONS":
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            auth_header = headers.get(b"authorization", b"").decode("utf-8")
            presented = (
                auth_header[7:] if auth_header.lower().startswith("bearer ") else ""
            )

            identity = None
            if MCP_OAUTH_CLIENT_SECRET and presented == MCP_OAUTH_CLIENT_SECRET:
                identity = {"username": MCP_USER}
            else:
                try:
                    identity = await asyncio.to_thread(
                        identity_from_bearer, auth_header
                    )
                except RuntimeError:
                    identity = None
                if identity and MCP_ALLOWED_USERS:
                    identity = (
                        identity
                        if identity.get("username") in MCP_ALLOWED_USERS
                        else None
                    )
            if not identity:
                response = JSONResponse({"error": "Unauthorized"}, status_code=401)
                await response(scope, receive, send)
                return

            scope.setdefault("state", {})["identity"] = identity

        await self.app(scope, receive, send)


def _request_username(ctx: Context) -> str:
    """Username of the authenticated MCP caller, from the request scope."""
    try:
        req = ctx.request_context.request
        identity = req.scope.get("state", {}).get("identity") or {}
        return str(identity.get("username", ""))
    except Exception:
        return ""


_token_lock = threading.Lock()
_token_cache = {"value": "", "exp": 0}
_token_refresh_lock = asyncio.Lock()


def _decode_exp(token):
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(data.get("exp", 0))
    except Exception:
        return 0


def _token_usable():
    now = time.time()
    exp = _token_cache["exp"]
    return bool(
        _token_cache["value"]
        and (
            (exp and now < exp - TOKEN_REFRESH_MARGIN)
            or (not exp and now < TOKEN_REFRESH_MARGIN)
        )
    )


async def _auth_headers():
    if not MCP_USER or not MCP_USER_PASSWORD:
        raise RuntimeError(
            "MCP_USER / MCP_USER_PASSWORD not set — cannot authenticate to the API"
        )
    if not _token_usable():
        async with _token_refresh_lock:
            if not _token_usable():
                token = await asyncio.to_thread(
                    oidc_password_grant, MCP_USER, MCP_USER_PASSWORD
                )
                with _token_lock:
                    _token_cache["value"] = token
                    _token_cache["exp"] = _decode_exp(token)
    return {"Authorization": f"Bearer {_token_cache['value']}"}


async def _call(method: str, path: str, **kw) -> str:
    try:
        headers = await _auth_headers()
    except Exception as e:
        return json.dumps({"error": f"MCP upstream auth failed: {e}"})
    async with httpx.AsyncClient() as client:
        r = await client.request(
            method, f"{API_BASE}{path}", headers=headers, timeout=30.0, **kw
        )
        if r.status_code >= 400:
            return json.dumps({"error": f"Upstream {r.status_code}", "detail": r.text})
        return r.text


_auto_renamed = set()


def _is_default_name(name: str, system_prompt: str) -> bool:
    name = (name or "").strip()
    if name in ("", "New Chat"):
        return True
    sp = (system_prompt or "").strip()
    if sp and (name == sp or name.startswith(sp[:40])):
        return True
    return False


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    out.append(part.get("text", ""))
                elif "text" in part:
                    out.append(str(part.get("text", "")))
        return "\n".join(p for p in out if p)
    return ""


async def _maybe_auto_rename(session_id: str) -> None:
    if not session_id or session_id in _auto_renamed:
        return
    try:
        res = await _call("GET", "/api/sessions")
        raw = res.text if hasattr(res, "text") else res
        try:
            sessions = json.loads(raw)
        except (ValueError, TypeError):
            return
        if not isinstance(sessions, list):
            return
        sess = next((s for s in sessions if s.get("session_id") == session_id), None)
        if not sess:
            return
        name = sess.get("name", "")
        system_prompt = sess.get("system_prompt", "")
        if not _is_default_name(name, system_prompt):
            _auto_renamed.add(session_id)
            return

        res = await _call("GET", f"/api/sessions/{session_id}/messages")
        raw = res.text if hasattr(res, "text") else res
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        messages = data.get("messages", []) if isinstance(data, dict) else []
        title = ""
        for m in messages:
            if m.get("role") == "user":
                txt = _extract_text(m.get("content", "")).strip()
                if txt and txt != "\U0001F3A4 Audio message":
                    title = txt
                    break
        if not title:
            for m in messages:
                if m.get("role") == "assistant":
                    txt = _extract_text(m.get("content", "")).strip()
                    if txt:
                        title = txt
                        break
        if not title:
            return
        title = title.replace("\n", " ").strip()
        if len(title) > 60:
            title = title[:60].rstrip() + "..."
        if not title:
            return
        await _call("PUT", f"/api/sessions/{session_id}", json={"name": title})
        _auto_renamed.add(session_id)
    except Exception:
        pass


async def _call_image(path: str):
    try:
        headers = await _auth_headers()
    except Exception as e:
        return None, json.dumps({"error": f"MCP upstream auth failed: {e}"})
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{API_BASE}{path}", headers=headers, timeout=60.0)
        if r.status_code >= 400:
            return None, json.dumps(
                {"error": f"Upstream {r.status_code}", "detail": r.text}
            )
        content_type = r.headers.get("content-type", "image/png")
        fmt = content_type.split("/")[-1].split(";")[0]
        return Image(data=r.content, format=fmt), None


import subprocess

from server.config import (
    LLAMA_BASE_GUARDRAIL,
    LLAMA_SERVER_PATH,
    VERIFY_CONTEXT_SIZE,
    VERIFY_IDLE_TIMEOUT,
    VERIFY_MODEL,
    VERIFY_PORT,
)

_verify_proc = None
_verify_last_used = 0.0
_verify_lock = threading.Lock()


def _verify_server_url():
    return LLAMA_BASE_GUARDRAIL


def _ensure_verify_server():
    pass


def _touch_verify():
    global _verify_last_used
    with _verify_lock:
        _verify_last_used = time.time()


def _stop_verify_server():
    global _verify_proc
    with _verify_lock:
        if _verify_proc and _verify_proc.poll() is None:
            print("[verify] shutting down verification server", flush=True)
            _verify_proc.terminate()
            try:
                _verify_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _verify_proc.kill()
        _verify_proc = None


async def _verify_idle_watcher():
    while True:
        await asyncio.sleep(30)
        with _verify_lock:
            proc = _verify_proc
            last = _verify_last_used
        if proc and proc.poll() is None and last > 0:
            idle = time.time() - last
            if idle > VERIFY_IDLE_TIMEOUT:
                print(
                    f"[verify] idle {idle:.0f}s > {VERIFY_IDLE_TIMEOUT}s — "
                    "unloading verification model",
                    flush=True,
                )
                _stop_verify_server()


VERIFY_TIMEOUT = 90


async def _run_llm_verify(message: str, judge_system_prompt: str, model_id: str = None) -> tuple:
    from server.features.state import M
    from server.features.monitoring import ensure_guardrail_ready
    from server.features.judge import sanitize_judge_model
    from server.input_guard import _parse_verdict, _parse_strict_verdict
    import re as _re

    task_lane = "guardrail"
    model_id = sanitize_judge_model(
        (model_id or "").strip() or M.server_model_id(task_lane),
        M.server_base(task_lane),
    )
    text = (message or "").strip()
    if not text:
        print(f"[guardrail][L2] empty message, auto-passing")
        return True, ""

    print(f"[guardrail][L2] ensuring {task_lane} server is running (with model {model_id} loaded)")
    await asyncio.to_thread(ensure_guardrail_ready, model_id=model_id)
    print(f"[guardrail][L2] {task_lane} server ready")

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": judge_system_prompt},
            {"role": "user", "content": text[:4000]},
        ],
        "temperature": 0,
        "max_tokens": 2048,
        "stream": False,
    }

    async def _judge_call(max_tokens=2048):
        """POST the judge payload; return (content, reasoning, error_string).

        Reasoning-capable "it" models emit a long ``reasoning_content`` before the
        verdict in ``content``. If ``max_tokens`` is too small the reasoning eats
        the whole budget and ``content`` comes back empty (finish_reason length),
        which used to misread as 'judge unavailable'. We return both fields so the
        caller can fall back to judging on the reasoning text, plus a larger budget
        on retry.
        """
        payload["max_tokens"] = max_tokens
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    M.server_url(task_lane),
                    json=payload,
                    timeout=VERIFY_TIMEOUT,
                )
            if r.status_code != 200:
                return "", "", f"HTTP {r.status_code}"
            msg = r.json().get("choices", [{}])[0].get("message", {})
            content = msg.get("content", "") or ""
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            return content, reasoning, ""
        except Exception as e:
            return "", "", str(e)

    print(f"[guardrail][L2] calling {task_lane} LLM server at {M.server_url(task_lane)}")
    print(f"[guardrail][L2] input message: {text}")
    reply, reasoning, err = await _judge_call()
    if not reply and reasoning:
        # Reasoning-only reply (reasoning consumed the token budget before the
        # verdict was emitted). Judge on the reasoning text instead of failing.
        print(
            f"[guardrail][L2] empty content but reasoning present — "
            "judging on reasoning text"
        )
        reply = reasoning
    if not reply:
        # Availability failure (connection error / non-200 / empty reply): restart,
        # wait for the model, and retry with a larger budget before failing closed.
        print(
            f"[guardrail][L2] judge call unavailable ({err or 'empty'}) — "
            "restarting judge & retrying once",
            flush=True,
        )
        await asyncio.to_thread(ensure_guardrail_ready, model_id=model_id)
        reply, reasoning, err = await _judge_call(max_tokens=4096)
        if not reply and reasoning:
            reply = reasoning
        if not reply:
            print(
                f"[guardrail][L2] judge still unavailable after restart "
                f"({err or 'empty'}) — treating as HARMFUL (fail-closed)",
                flush=True,
            )
            return False, f"LLM judge unavailable after restart ({err or 'empty'})"

    print(f"[guardrail][L2] raw verdict: {reply.strip()}")
    words = set(w for w in _re.findall(r"[A-Za-z']+", reply.upper())[:10])
    blocked_tokens = {"BLOCKED", "UNSAFE", "HARMFUL", "REJECTED", "DENIED", "REFUSED"}
    if words & blocked_tokens and _parse_strict_verdict(reply):
        print(f"[guardrail][L2] BLOCKED: found blocked token + strict verdict")
        return False, f"LLM judge: {reply.strip()}"
    if _parse_verdict(reply):
        print(f"[guardrail][L2] BLOCKED: verdict triggered")
        return False, f"LLM judge: {reply.strip()}"
    print(f"[guardrail][L2] PASSED: message approved by LLM judge")
    return True, ""


def _requeue_pending_mcp_tasks():
    """Reload recovery: resets non-terminal MCP tasks in SQLite to 'queued' so
    the DB worker picks them up."""
    rows = mcp_task_list(limit=500)
    requeued_count = 0
    for row in rows:
        if row["status"] in ("queued", "working"):
            mcp_task_update(row["task_id"], status="queued")
            requeued_count += 1
    if requeued_count > 0:
        print(f"[mcp] reset {requeued_count} pending MCP task(s) to queued on reload", flush=True)

from server.config import (
    LLAMA_BASE_GUARDRAIL,
    LLAMA_SERVER_PATH,
    VERIFY_CONTEXT_SIZE,
    VERIFY_IDLE_TIMEOUT,
    VERIFY_MODEL,
    VERIFY_PORT,
)

@mcp.tool()
async def get_user_context() -> str:
    """Retrieve the persistent user profile and memory context for the authenticated MCP caller.

    Returns a JSON object containing user-level information that persists
    across sessions: preferences, prior interactions, and any stored memory
    entries. Use this to personalise subsequent requests without re-explaining
    context.

    Returns:
        JSON string with the user's profile and memory context.
    """
    return await _call("GET", "/api/user-context")


@mcp.tool()
async def list_sessions() -> str:
    """List all chat sessions owned by the authenticated user, ordered by most recently active first.

    Returns a JSON array of session objects, each containing session_id,
    name, creation timestamp, and last-updated timestamp. Use session_id
    values with send_chat_message, get_session_messages, and rename_session.

    Returns:
        JSON array of session summary objects.
    """
    return await _call("GET", "/api/sessions")


@mcp.tool()
async def create_session(
    system_prompt: str = "",
    system_prompts: list = None,
    ctx: Context = None,
) -> str:
    """Create a new empty chat session with an optional system prompt or list of system prompts.

    The session is created under the authenticated user's account and can
    receive messages via send_chat_message. System prompts are validated
    through L1 pattern matching and L2 LLM judge before the session is
    created; harmful or jailbreaking prompts are rejected.

    Args:
        system_prompt: A single system prompt string to set the session persona.
        system_prompts: A list of system prompt strings or dicts with a "prompt" key.
            When both are provided they are merged. Mutually exclusive with
            session_ids in batch mode.

    Returns:
        JSON with the new session_id on success, or declined=True if a
        guardrail rejected the system prompt.
    """
    sp_jail = (system_prompt and is_jailbreak_attempt(system_prompt)) or any(
        isinstance(sp, dict) and is_jailbreak_attempt(str(sp.get("prompt", "")))
        or isinstance(sp, str) and is_jailbreak_attempt(sp)
        for sp in (system_prompts or [])
    )
    sp_harm = (system_prompt and is_harmful_request(system_prompt)) or any(
        isinstance(sp, dict) and is_harmful_request(str(sp.get("prompt", "")))
        or isinstance(sp, str) and is_harmful_request(sp)
        for sp in (system_prompts or [])
    )
    if sp_jail or sp_harm:
        return json.dumps({
            "declined": True,
            "response": GUARDRAIL_DECLINE,
            "detail": "system_prompt blocked by MCP input guardrail",
        })
    sp_texts = [system_prompt] if system_prompt else []
    sp_texts += [
        str(sp.get("prompt", "")) if isinstance(sp, dict) else str(sp)
        for sp in (system_prompts or [])
    ]
    judge_model = resolve_judge_model(_request_username(ctx))
    for sp in sp_texts:
        if sp and await asyncio.to_thread(
            llm_classify_harmful, sp, None, 20, JUDGE_FAIL_CLOSED, judge_model
        ):
            return json.dumps({
                "declined": True,
                "response": GUARDRAIL_DECLINE,
                "detail": "system_prompt blocked by MCP LLM safety judge",
            })
    payload = {}
    if system_prompt:
        payload["system_prompt"] = system_prompt
    if system_prompts:
        payload["system_prompts"] = system_prompts
    return await _call("POST", "/api/sessions", json=payload)


@mcp.tool()
async def get_session_messages(session_id: str) -> str:
    """Retrieve the full message transcript of a chat session.

    Returns the complete conversation history including user messages,
    assistant replies, tool calls, and metadata. Use this to read the
    assistant's response after get_message_status reports status=done.

    Args:
        session_id: The session identifier returned by create_session or list_sessions.

    Returns:
        JSON object with a "messages" array containing the full transcript.
    """
    res = await _call("GET", f"/api/sessions/{session_id}/messages")
    raw = res.text if hasattr(res, "text") else res
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    return raw


@mcp.tool()
async def rename_session(session_id: str, name: str) -> str:
    """Rename a chat session. The name is displayed in the UI sidebar and returned in list_sessions.

    Args:
        session_id: The session to rename.
        name: New display name for the session.

    Returns:
        JSON confirmation with the updated session metadata.
    """
    return await _call("PUT", f"/api/sessions/{session_id}", json={"name": name})


@mcp.tool()
async def send_chat_message(
    session_id: str,
    message: str,
    research: bool = False,
    cpu: bool = False,
    no_tools: bool = False,
    ctx: Context = None,
) -> str:
    """Submit a user message for asynchronous processing through the guardrail-protected pipeline.

    Every message passes through three guardrail stages before content is
    generated:

      L1  Code-level pattern scan  — substring matching against known
          jailbreak and harmful-request pattern lists.  Blocks instantly
          with no LLM call.
      L2  LLM input classification — the guardrail lane LLM judge
          evaluates the text for harmful intent.  Fail-closed: if the
          judge is unreachable the message is blocked.
      L3  LLM output verification  — runs after generation against the
          full reply to catch any policy-violating content that slipped
          through.

    If any guardrail stage rejects the message, the response contains
    declined=True with the reason.  Otherwise a task_id is returned for
    polling with get_message_status.

    Args:
        session_id: Target session from create_session or list_sessions.
        message: The user message text to process.
        research: Enable deep-research mode (tool-heavy, est. 3-7 min).
        cpu: Force CPU lane processing (slower, preserves GPU for others).
        no_tools: Skip tool execution, text-only generation (est. 40-90s).

    Returns:
        JSON with task_id and wait_hint on success, or declined=True if a
        guardrail blocked the message.
    """
    print(f"[MCP] send_chat_message called for session {session_id}, msg_len={len(message)}")

    print(f"[guardrail][L1] checking jailbreak...")
    if is_jailbreak_attempt(message):
        reason = "jailbreak pattern detected"
        print(f"[guardrail][L1] REJECTED: {reason}")
        return json.dumps({
            "declined": True,
            "reason": reason,
            "detail": "message blocked by L1 guardrail",
        })
    print(f"[guardrail][L1] jailbreak check passed")

    print(f"[guardrail][L1] checking harmful request...")
    if is_harmful_request(message):
        reason = "harmful request pattern detected"
        print(f"[guardrail][L1] REJECTED: {reason}")
        return json.dumps({
            "declined": True,
            "reason": reason,
            "detail": "message blocked by L1 guardrail",
        })
    print(f"[guardrail][L1] harmful check passed")

    print(f"[guardrail][L2] running LLM verification on guardrail lane...")
    from server.input_guard import _judge_system
    judge_model = resolve_judge_model(_request_username(ctx))
    passed, reason = await _run_llm_verify(
        message, _judge_system(), model_id=judge_model
    )
    if not passed:
        print(f"[guardrail][L2] REJECTED: {reason}")
        return json.dumps({
            "declined": True,
            "reason": reason,
            "detail": "message blocked by L2 LLM verification",
        })
    print(f"[guardrail][L2] LLM verification passed")

    task_id = str(uuid.uuid4())
    print(f"[MCP] inserting task {task_id} into db for session {session_id}")
    mcp_task_insert(
        task_id, session_id, message, mode="gpu",
        research=research, cpu=cpu, no_tools=no_tools,
    )
    print(f"[MCP] task {task_id} inserted successfully")

    if research:
        init_delay = 60
        mode_desc = "Deep research mode enabled (est. 3-7 mins)"
    elif not no_tools:
        init_delay = 30
        mode_desc = "Tool execution pipeline active (est. 2-3 mins)"
    else:
        init_delay = 20
        mode_desc = "Standard text generation (est. 40-90s)"

    print(f"[MCP] returning task_id={task_id} to client")
    return json.dumps({
        "task_id": task_id,
        "wait_hint": (
            f"{mode_desc}. Do NOT poll immediately. "
            f"Sleep at least {init_delay} seconds BEFORE calling get_message_status(task_id='{task_id}') for the first time. "
            f"Never poll faster than every 15-20 seconds."
        ),
    })


@mcp.tool()
async def get_message_status(task_id: str) -> str:
    """Poll the processing state of a message submitted via send_chat_message.

    Returns the current status, the assistant's reply (when done), the
    guardrail verification level (L1/L2/L3 pass or fail), and a
    failure_reason if any stage blocked the request.

    Do NOT poll immediately after submitting.  Follow the wait_hint returned
    by send_chat_message (typically 20-60 seconds depending on mode), then
    poll no faster than every 15-20 seconds.

    Args:
        task_id: The task identifier returned by send_chat_message.

    Returns:
        JSON with fields: status (queued|working|done|error), reply,
        verification_level, failure_reason, elapsed_seconds, and
        next_action (a human-readable suggestion for what to do next).
    """
    from server.features.state import M
    row = mcp_task_get(task_id)
    if row is None:
        return json.dumps({"status": "unknown", "error": "task not found"})

    obj = {
        "status": row["status"],
        "task_id": row["task_id"],
        "session_id": row["session_id"],
        "reply": row["reply"],
        "verification_level": row["verification_level"],
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }

    with _data_lock:
        mem = M.tasks.get(task_id, {})
    mem_status = mem.get("status", "")
    if mem_status in ("working", "done", "error"):
        obj["status"] = mem_status
    if mem_status == "done":
        obj["reply"] = mem.get("response", obj.get("reply", ""))
    elif mem_status == "error":
        obj["failure_reason"] = mem.get("error", obj.get("failure_reason", ""))

    if row["updated_at"] and row["created_at"]:
        obj["elapsed_seconds"] = int(row["updated_at"] - row["created_at"])

    status = obj["status"]
    if status == "working":
        elapsed = obj.get("elapsed_seconds", 0)
        if elapsed > 120:
            recommended_sleep = "45-60"
        elif elapsed > 60:
            recommended_sleep = "30-40"
        else:
            recommended_sleep = "20-30"
        obj["next_action"] = (
            f"Still generating (elapsed: {elapsed}s). "
            f"Sleep {recommended_sleep} seconds before calling get_message_status again."
        )
    elif status == "done":
        obj["next_action"] = "Generation complete. Call get_session_messages to read the response."
        sid = obj.get("session_id")
        if sid:
            await _maybe_auto_rename(sid)

    return json.dumps(obj)


BATCH_MAX_ITEMS = 50
BATCH_POLL_INTERVAL = 15
BATCH_ITEM_TIMEOUT = 2400
BATCH_CHAT_SUBMIT_RETRIES = 3
BATCH_KEEP = 50
BATCH_WORKER_IDLE_SLEEP = 2


def _batch_summary(batch_id, b):
    items = b["items"]
    total = len(items)
    counts = {"done": 0, "error": 0, "running": 0, "queued": 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    finished = counts["done"] + counts["error"]
    return {
        "batch_id": batch_id,
        "status": b["status"],
        "total": total,
        "progress": f"{finished} out of {total} items completed",
        "percent_complete": round(finished * 100 / total) if total else 100,
        **counts,
        "results_submitted": sum(1 for it in items if "submitted" in it),
        "uncollected": sum(
            1 for it in items
            if it["status"] in ("done", "error") and "collected_at" not in it
        ),
        "created": b["created"],
    }


async def _batch_create_session(system_prompt):
    payload = {"system_prompt": system_prompt} if system_prompt else {}
    res = await _call("POST", "/api/sessions", json=payload)
    raw = res.text if hasattr(res, "text") else str(res)
    try:
        return json.loads(raw).get("session_id", "")
    except (ValueError, TypeError, AttributeError):
        return ""


async def _run_batch(batch_id):
    b = batch_get(batch_id)
    if not b:
        return
    # Batches are owned by the single configured MCP identity, so every item's
    # L2/L3 judge resolves to MCP_USER's configured judge model.
    batch_judge_model = resolve_judge_model(MCP_USER)
    shared_sid = b["session_id"]
    for it in b["items"]:
        if it["status"] in ("done", "error"):
            continue
        item_update(batch_id, it["index"], status="running")
        try:
            sid = it["session_id"] or shared_sid or await _batch_create_session(b["system_prompt"])
            if not sid:
                item_update(
                    batch_id, it["index"], status="error",
                    error="could not create session",
                )
                continue
            item_update(batch_id, it["index"], session_id=sid)

            prompt = it["prompt"]

            if is_jailbreak_attempt(prompt):
                reason = "jailbreak pattern detected"
                print(
                    f"[guardrail][L1] blocked batch item {it['index']} "
                    f"(batch {batch_id}): {reason}"
                )
                item_update(
                    batch_id, it["index"], status="done",
                    verification_level="LEVEL 1 VERIFICATION FAILED",
                    failure_reason=reason,
                )
                continue
            if is_harmful_request(prompt):
                reason = "harmful request pattern detected"
                print(
                    f"[guardrail][L1] blocked batch item {it['index']} "
                    f"(batch {batch_id}): {reason}"
                )
                item_update(
                    batch_id, it["index"], status="done",
                    verification_level="LEVEL 1 VERIFICATION FAILED",
                    failure_reason=reason,
                )
                continue

            from server.input_guard import _judge_system
            passed, reason = await _run_llm_verify(
                prompt, _judge_system(), model_id=batch_judge_model
            )
            if not passed:
                print(
                    f"[guardrail][L2] blocked batch item {it['index']} "
                    f"(batch {batch_id}): {reason}"
                )
                item_update(
                    batch_id, it["index"], status="done",
                    verification_level="LEVEL 2 LLM VERIFICATION FAILED",
                    failure_reason=reason,
                )
                continue

            task_id = ""
            for attempt in range(BATCH_CHAT_SUBMIT_RETRIES):
                res = await _call(
                    "POST",
                    "/api/chat",
                    json={
                        "session_id": sid,
                        "message": wrap_user_message(prompt),
                        "research": b["research"],
                        "cpu": b["cpu"],
                        "no_tools": b["no_tools"],
                    },
                )
                raw = res.text if hasattr(res, "text") else str(res)
                try:
                    obj = json.loads(raw)
                except (ValueError, TypeError):
                    obj = {}
                if isinstance(obj, dict) and obj.get("task_id"):
                    task_id = obj["task_id"]
                    break
                busy = "503" in raw or "Busy" in raw
                if not busy or attempt == BATCH_CHAT_SUBMIT_RETRIES - 1:
                    raise RuntimeError(raw[:300])
                await asyncio.sleep(20)
            item_update(batch_id, it["index"], task_id=task_id)

            deadline = time.time() + BATCH_ITEM_TIMEOUT
            final_status = ""
            while time.time() < deadline:
                await asyncio.sleep(BATCH_POLL_INTERVAL)
                res = await _call("GET", f"/api/status/{task_id}")
                raw = res.text if hasattr(res, "text") else str(res)
                try:
                    st = json.loads(raw).get("status", "")
                except (ValueError, TypeError, AttributeError):
                    continue
                if st in ("done", "error", "cancelled"):
                    final_status = st
                    break
            if not final_status:
                raise RuntimeError(f"timed out after {BATCH_ITEM_TIMEOUT}s")

            if final_status != "done":
                item_update(
                    batch_id, it["index"], status="error",
                    error=f"task ended as '{final_status}'",
                )
                continue

            res = await _call("GET", f"/api/sessions/{sid}/messages")
            raw = res.text if hasattr(res, "text") else str(res)
            reply = ""
            try:
                msgs = json.loads(raw).get("messages", [])
                for m in reversed(msgs):
                    if m.get("role") == "assistant":
                        c = m.get("content")
                        reply = c if isinstance(c, str) else json.dumps(c)
                        break
            except (ValueError, TypeError, AttributeError):
                pass

            if reply:
                from server.input_guard import _strict_judge_system
                passed, reason = await _run_llm_verify(
                    reply, _strict_judge_system(), model_id=batch_judge_model
                )
                if not passed:
                    print(
                        f"[guardrail][L3] blocked batch item {it['index']} "
                        f"(batch {batch_id}): {reason}"
                    )
                    item_update(
                        batch_id, it["index"], status="done",
                        reply=HARMFUL_DECLINE,
                        verification_level="LEVEL 3 POST PROCESSING LLM VERIFICATION FAILED",
                        failure_reason=reason,
                        guardrail_blocked=1,
                    )
                    continue

            item_update(
                batch_id, it["index"], status="done", reply=reply,
            )
        except Exception as e:
            item_update(
                batch_id, it["index"], status="error", error=str(e)[:300]
            )


async def _batch_worker():
    print("[batches] worker started — draining SQLite queue", flush=True)
    while True:
        batch_id = ""
        try:
            batch_id = claim_next_pending()
        except Exception as e:
            print(f"[batches] claim failed: {e}", flush=True)
        if not batch_id:
            await asyncio.sleep(BATCH_WORKER_IDLE_SLEEP)
            continue
        ahead = pending_count()
        print(
            f"[batches] WORKING on {batch_id} ({ahead} still PENDING)", flush=True
        )
        try:
            await _run_batch(batch_id)
        except Exception as e:
            print(f"[batches] {batch_id} crashed: {e}", flush=True)
            try:
                fail_open_items(batch_id, f"worker crash: {e}")
            except Exception:
                pass
        finally:
            try:
                finish_batch(batch_id)
                print(f"[batches] COMPLETED {batch_id}", flush=True)
            except Exception:
                pass


@mcp.tool()
async def start_chat_batch(
    prompts: list,
    shared_session: bool = False,
    system_prompt: str = "",
    research: bool = False,
    cpu: bool = False,
    no_tools: bool = False,
    session_ids: list = None,
) -> str:
    """Submit a batch of chat prompts for sequential processing and receive a batch_id immediately.

    Items are processed one at a time through the same L1/L2/L3 guardrail
    pipeline as send_chat_message.  Processing is sequential to respect
    server capacity; expect roughly 2-7 minutes per item depending on mode.

    The batch is queued behind any earlier batches.  Do NOT poll immediately:
    sleep for the estimated total time, then call get_batch_status to check
    progress and get_batch_results to collect replies.

    Args:
        prompts: List of user message strings to process (max 50).
        shared_session: If True, all prompts share one session with
            system_prompt as its persona.  Mutually exclusive with
            session_ids.
        system_prompt: Persona for the shared session (used only when
            shared_session=True).
        research: Enable deep-research mode for every item.
        cpu: Force CPU lane processing for every item.
        no_tools: Disable tool execution for every item.
        session_ids: A list of pre-existing session_ids aligned 1:1 with
            prompts.  Each prompt is processed in its own bound session.
            Mutually exclusive with shared_session and system_prompt.

    Returns:
        JSON with batch_id, total item count, queue position, estimated
        total time, and polling instructions.
    """
    if not isinstance(prompts, list) or not prompts:
        return json.dumps({"error": "prompts must be a non-empty list"})
    prompts = [p for p in prompts if isinstance(p, str) and p.strip()]
    if not prompts:
        return json.dumps({"error": "prompts contains no usable strings"})
    if len(prompts) > BATCH_MAX_ITEMS:
        return json.dumps({
            "error": f"too many prompts ({len(prompts)}); max {BATCH_MAX_ITEMS}"
        })
    if session_ids is not None:
        if shared_session:
            return json.dumps({"error": "session_ids cannot be combined with shared_session"})
        if system_prompt:
            return json.dumps({"error": "session_ids cannot be combined with system_prompt (bound sessions keep their original persona)"})
        if not isinstance(session_ids, list) or len(session_ids) != len(prompts):
            return json.dumps({
                "error": f"session_ids must be a list aligned with prompts ({len(prompts)} entries, same order)"
            })
        bad = [i for i, s in enumerate(session_ids) if not isinstance(s, str) or not s.strip()]
        if bad:
            return json.dumps({
                "error": "session_ids contains empty/non-string entries",
                "bad_indexes": bad,
            })

    session_id = ""
    if shared_session:
        session_id = await _batch_create_session(system_prompt)
        if not session_id:
            return json.dumps({"error": "could not create shared session"})

    batch_id = uuid.uuid4().hex[:12]
    batch_insert(
        batch_id, system_prompt, session_id, research, cpu, no_tools, prompts,
        item_session_ids=session_ids,
    )
    prune_batches(keep=BATCH_KEEP)

    ahead = queue_position(batch_id)

    per_item_min = 7 if (research or not no_tools) else 2
    est_total = len(prompts) * per_item_min
    return json.dumps({
        "batch_id": batch_id,
        "total": len(prompts),
        "status": BATCH_PENDING,
        "queue_position": ahead,
        "mode": (
            "shared_session" if shared_session
            else ("bound_sessions" if session_ids else "per_item_sessions")
        ),
        "est_total_minutes": est_total,
        "note": (
            f"{len(prompts)} prompts queued SEQUENTIALLY "
            f"(~{per_item_min} min per item, ≈{est_total} min total"
            + (f"; behind {ahead} earlier batch(es)" if ahead else "")
            + "). Guard rails (LEVEL 1 pattern + LEVEL 2 LLM verification) "
            "run AFTER dequeue in the worker. "
            f"Do NOT poll immediately: "
            f"sleep ~30 minutes, then call get_batch_status('{batch_id}') — "
            "it returns new_indexes (ids only, no text). Fetch those replies "
            f"with get_batch_results('{batch_id}', [ids]) — they are marked "
            f"collected and won't reappear. Optionally push outcomes back via "
            f"one submit_batch_results('{batch_id}', results) call. Re-poll "
            "every ~30 minutes; each wave reveals only new items."
        ),
    })


@mcp.tool()
async def get_batch_status(batch_id: str) -> str:
    """Check progress of a batch started with start_chat_batch without retrieving reply text.

    Returns counts of completed, failed, and new (not yet collected) items,
    along with any guardrail verification failures.  Use new_indexes with
    get_batch_results to fetch the actual replies.

    Args:
        batch_id: The batch identifier returned by start_chat_batch.

    Returns:
        JSON with total, completed, failed, new_indexes, queue_position
        (if still pending), and per-item verification failure details.
    """
    b = batch_get(batch_id)
    if not b:
        return json.dumps({"error": "unknown batch_id"})
    out = _batch_summary(batch_id, b)
    out["completed_indexes"] = [
        it["index"] for it in b["items"] if it["status"] == "done"
    ]
    out["failed_indexes"] = [
        it["index"] for it in b["items"] if it["status"] == "error"
    ]
    out["new_indexes"] = [
        it["index"] for it in b["items"]
        if it["status"] in ("done", "error") and "collected_at" not in it
    ]

    vfailed = [
        {
            "index": it["index"],
            "level": it.get("verification_level", ""),
            "reason": it.get("failure_reason", ""),
        }
        for it in b["items"]
        if it.get("verification_level")
    ]
    if vfailed:
        out["verification_failed"] = vfailed
    out["results_submitted"] = sum(1 for it in b["items"] if "submitted" in it)
    if b["status"] == BATCH_PENDING:
        out["queue_position"] = queue_position(batch_id)
    if b["status"] == BATCH_ERROR:
        out["note"] = (
            "EVERY item in this batch failed — see failed_indexes and the "
            f"per-item 'error' text via get_batch_results('{batch_id}', "
            "failed_indexes). Nothing was collected; do not re-poll."
        )
    else:
        out["note"] = (
            "Status only — no reply text. For each NEW wave: "
            f"get_batch_results('{batch_id}', new_indexes) fetches those replies "
            "(and marks them collected). Attach your own outcomes — grades, "
            "notes, anything — with one submit_batch_results("
            f"'{batch_id}', results) call."
        )
    return json.dumps(out)


@mcp.tool()
async def get_batch_results(
    batch_id: str, indexes: list = None, new_only: bool = False
) -> str:
    """Collect assistant replies and metadata for items in a batch.

    Each call marks retrieved items as collected so they are not returned
    again on subsequent calls.  Use get_batch_status first to obtain
    new_indexes, then pass them here to fetch only the latest wave.

    Args:
        batch_id: The batch identifier from start_chat_batch.
        indexes: Optional list of specific item indexes to retrieve.
            If omitted, all completed/failed items are returned.
        new_only: If True, return only items not yet collected in a
            previous call.

    Returns:
        JSON array of item objects with index, status, prompt, reply
        (for done items), error (for failed items), session_id, and any
        guardrail verification_level / failure_reason fields.
    """
    b = batch_get(batch_id)
    if not b:
        return json.dumps({"error": "unknown batch_id"})
    if indexes is None:
        selected = [it for it in b["items"] if it["status"] in ("done", "error")]
    else:
        by_idx = {it["index"]: it for it in b["items"]}
        selected = []
        for i in indexes if isinstance(indexes, list) else []:
            try:
                selected.append(by_idx[int(i)])
            except (KeyError, TypeError, ValueError):
                selected.append({"index": i, "error": "unknown index"})
    if new_only:
        selected = [it for it in selected if "collected_at" not in it]
    now = int(time.time())
    out = []
    for it in selected:
        if "status" not in it:
            out.append(it)
            continue
        if it["status"] in ("done", "error") and "collected_at" not in it:
            it["collected_at"] = now
            item_update(batch_id, it["index"], collected_at=now)
        entry = {
            "index": it["index"],
            "status": it["status"],
            "prompt": it["prompt"],
        }
        if it["status"] == "done":
            reply = it.get("reply", "")
            entry["reply"] = reply
            entry["session_id"] = it["session_id"]
            if it.get("verification_level"):
                entry["verification_level"] = it["verification_level"]
            if it.get("failure_reason"):
                entry["failure_reason"] = it["failure_reason"]
        elif it["status"] == "error":
            entry["error"] = it["error"]
        else:
            entry["note"] = "not finished yet — poll get_batch_status"
        if "submitted" in it:
            entry["submitted_result"] = it["submitted"]["result"]
            entry["submitted_at"] = it["submitted"]["submitted_at"]
        out.append(entry)
    return json.dumps(out)


@mcp.tool()
async def submit_batch_results(batch_id: str, results: list) -> str:
    """Attach your own per-item outcomes (grades, notes, evaluations) to a batch in a single call.

    Use this to push external evaluation results back onto batch items after
    collecting replies with get_batch_results.  Each result object must
    contain an "index" field matching an item index in the batch, plus any
    key-value pairs you want to store (e.g. "grade", "note", "score").

    Args:
        batch_id: The batch identifier from start_chat_batch.
        results: List of dicts, each with "index" (int) and any additional
            evaluation fields to attach.

    Returns:
        JSON with accepted/rejected counts and per-rejection reasons.
    """
    b = batch_get(batch_id)
    if not b:
        return json.dumps({"error": "unknown batch_id"})
    if not isinstance(results, list) or not results:
        return json.dumps({"error": "results must be a non-empty list of objects"})
    by_idx = {it["index"]: it for it in b["items"]}
    accepted, rejected = [], []
    for entry in results:
        if not isinstance(entry, dict):
            rejected.append({
                "entry": str(entry)[:120],
                "error": "entry must be an object with 'index' and 'result'",
            })
            continue
        try:
            idx = int(entry.get("index"))
        except (TypeError, ValueError):
            rejected.append({"entry": str(entry)[:120], "error": "missing/invalid 'index'"})
            continue
        if idx not in by_idx:
            rejected.append({"index": idx, "error": "unknown index"})
            continue
        if "result" not in entry:
            rejected.append({"index": idx, "error": "missing 'result'"})
            continue
        item_update(
            batch_id,
            idx,
            submitted_result=json.dumps(entry["result"]),
            submitted_at=int(time.time()),
        )
        accepted.append(idx)
    pre_submitted = {
        it["index"] for it in b["items"] if "submitted" in it
    }
    total_submitted = len(pre_submitted | set(accepted))
    return json.dumps({
        "batch_id": batch_id,
        "accepted": accepted,
        "rejected": rejected,
        "results_submitted": total_submitted,
        "note": (
            f"Stored {len(accepted)} result(s) on batch '{batch_id}'. "
            f"Retrieve them via get_batch_results('{batch_id}') — submitted "
            "values appear as 'submitted_result' alongside each reply."
        ),
    })


@mcp.tool()
async def get_image(image_id: str):
    """Fetch an image that was generated or referenced in a chat conversation.

    The image_id comes from assistant replies that include image output
    (e.g. image generation tool results).  Returns the raw image bytes
    suitable for display.

    Args:
        image_id: The image identifier from a chat message's image field.

    Returns:
        Raw image data, or an error message if the image is not found.
    """
    img, err = await _call_image(f"/api/image/{image_id.lstrip('/')}")
    return err if err else img


import secrets, hashlib
from starlette.responses import RedirectResponse

_auth_codes = {}
_auth_codes_lock = threading.Lock()
AUTH_CODE_TTL = 300


async def oauth_metadata(request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "issuer": base,
            "authorization_endpoint": f"{base}/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "client_credentials"],
            "code_challenge_methods_supported": ["S256", "plain"],
            "token_endpoint_auth_methods_supported": [
                "client_secret_post",
                "client_secret_basic",
                "none",
            ],
        }
    )


async def oauth_authorize(request):
    p = request.query_params
    client_id = p.get("client_id", "")
    redirect_uri = p.get("redirect_uri", "")
    state = p.get("state", "")
    code_challenge = p.get("code_challenge", "")
    code_challenge_method = p.get("code_challenge_method", "S256")

    if p.get("response_type") != "code" or not redirect_uri:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if MCP_OAUTH_CLIENT_ID and client_id != MCP_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "unauthorized_client"}, status_code=401)

    code = secrets.token_urlsafe(32)
    with _auth_codes_lock:
        _auth_codes[code] = {
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "exp": time.time() + AUTH_CODE_TTL,
        }
    sep = "&" if "?" in redirect_uri else "?"
    location = f"{redirect_uri}{sep}code={code}"
    if state:
        location += f"&state={state}"
    return RedirectResponse(location, status_code=302)


def _pkce_ok(verifier, challenge, method):
    if not challenge:
        return True
    if method == "plain":
        return verifier == challenge
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return calc.rstrip(b"=").decode() == challenge


async def oauth_token(request):
    try:
        form = await request.form()
    except Exception:
        form = {}
    grant_type = form.get("grant_type", "")
    client_id = form.get("client_id", "")
    client_secret = form.get("client_secret", "")
    if not client_id:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            decoded = base64.b64decode(auth[6:]).decode("utf-8", "ignore")
            client_id, _, client_secret = decoded.partition(":")

    if MCP_OAUTH_CLIENT_ID and client_id != MCP_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "invalid_client"}, status_code=401)
    if client_secret and client_secret != MCP_OAUTH_CLIENT_SECRET:
        return JSONResponse({"error": "invalid_client"}, status_code=401)

    if grant_type == "authorization_code":
        code = form.get("code", "")
        with _auth_codes_lock:
            entry = _auth_codes.pop(code, None)
        if not entry or entry["exp"] < time.time():
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if form.get("redirect_uri", "") != entry["redirect_uri"]:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if not _pkce_ok(
            form.get("code_verifier", ""),
            entry["code_challenge"],
            entry["code_challenge_method"],
        ):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
    elif grant_type == "client_credentials":
        if not client_secret or client_secret != MCP_OAUTH_CLIENT_SECRET:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
    else:
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    return JSONResponse(
        {
            "access_token": MCP_OAUTH_CLIENT_SECRET,
            "token_type": "bearer",
            "expires_in": 31536000,
        }
    )


async def oauth_protected_resource(request):
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "resource": f"{base}/mcp",
            "authorization_servers": [base],
        }
    )


mcp_app = mcp.streamable_http_app()

_worker_task = None


@asynccontextmanager
async def lifespan(app):
    global _worker_task
    init_batches_db()
    requeued = requeue_stuck_batches()
    if requeued:
        print(
            f"[batches] requeued {requeued} WORKING batch(es) from previous run",
            flush=True,
        )
    _requeue_pending_mcp_tasks()
    _worker_task = asyncio.create_task(_batch_worker())
    _verify_watcher_task = asyncio.create_task(_verify_idle_watcher())
    try:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
    finally:
        _stop_verify_server()
        for task in (_verify_watcher_task, _worker_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/.well-known/oauth-authorization-server", oauth_metadata),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource),
        Route("/authorize", oauth_authorize),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Mount("/", app=EnforcementAuthMiddleware(mcp_app)),
    ],
)


def run():
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=8000)


if __name__ == "__main__":
    run()