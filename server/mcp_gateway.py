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
from mcp.server.fastmcp import FastMCP, Image
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

API_BASE = os.environ.get("CHAT_API_BASE", "http://127.0.0.1:3001")

# The LLM safety judge is the primary defence for non-English and paraphrased
# harmful prompts, so when it is configured its outages must fail CLOSED
# (block) rather than open. Default is fail-closed; set GUARD_FAIL_CLOSED=0 to
# revert to fail-open (degrade to the pattern layer) if you prefer availability
# over strict safety when the judge model is down.
JUDGE_FAIL_CLOSED = os.environ.get("GUARD_FAIL_CLOSED", "1").lower() not in (
    "0", "false", "no",
)
MCP_ALLOWED_USERS = [
    u.strip() for u in os.environ.get("MCP_ALLOWED_USERS", "").split(",") if u.strip()
]
MCP_USER = os.environ.get("MCP_USER", "")
MCP_USER_PASSWORD = os.environ.get("MCP_USER_PASSWORD", "")
# Paste these into Claude's "Add custom connector" -> Advanced settings.
# MCP_OAUTH_CLIENT_SECRET doubles as the bearer token: /oauth/token hands it
# back as the access_token, and the middleware below validates against it
# directly — no separate inbound-token secret to keep in sync.
MCP_OAUTH_CLIENT_ID = os.environ.get("MCP_OAUTH_CLIENT_ID", "")
MCP_OAUTH_CLIENT_SECRET = os.environ.get("MCP_OAUTH_CLIENT_SECRET", "")
TOKEN_REFRESH_MARGIN = 60

mcp = FastMCP("chat-webui-api", stateless_http=True)


class EnforcementAuthMiddleware:
    """Inbound gate: callers must present a valid Authentik access token.

    The bearer JWT is verified against Authentik's JWKS on every request —
    no shared static secret exists anymore, so tokens are revocable and
    every call carries a real identity. Optionally restricted to specific
    usernames via MCP_ALLOWED_USERS (empty = any realm user).
    """

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
                # Token issued by /oauth/token IS the client secret — a
                # client that already proved it knows the secret at the
                # token endpoint is trusted to present it again here.
                identity = {"username": MCP_USER}
            else:
                try:
                    # JWKS fetch does synchronous network I/O — keep it off
                    # the event loop so a slow/hung IdP can't freeze every
                    # concurrent request.
                    identity = await asyncio.to_thread(
                        identity_from_bearer, auth_header
                    )
                except RuntimeError:
                    # Transient JWKS failure — treat as unauthenticated rather
                    # than crashing the request handler.
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

        await self.app(scope, receive, send)


_token_lock = threading.Lock()
_token_cache = {"value": "", "exp": 0}
_token_refresh_lock = asyncio.Lock()


def _decode_exp(token):
    """Expiry timestamp of a JWT access token, or 0 if unreadable."""
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(data.get("exp", 0))
    except Exception:
        return 0


def _token_usable():
    """True when the cached token is still fresh enough to bother with."""
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
    """Bearer headers holding a fresh Authentik access token for MCP_USER.

    The password-grant refresh runs in a worker thread and never holds a
    lock across the network call: one slow/hung IdP must not stall other
    tool calls (a threading.Lock held over I/O here used to wedge the whole
    gateway whenever the upstream was unreachable).
    """
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


# Sessions we've already auto-renamed (once a response is received) so we
# don't clobber a name the user set manually, and don't rename on every poll.
_auto_renamed = set()


def _is_default_name(name: str, system_prompt: str) -> bool:
    """True when a session name is still an unhelpful placeholder.

    Covers the literal defaults ("New Chat", "") as well as a name that is
    just the session's own system prompt (e.g. "You are an intelligent..."),
    which is what MCP-created sessions tend to show.
    """
    name = (name or "").strip()
    if name in ("", "New Chat"):
        return True
    sp = (system_prompt or "").strip()
    if sp and (name == sp or name.startswith(sp[:40])):
        return True
    return False


def _extract_text(content) -> str:
    """Pull plain text out of a message content field (str or parts list)."""
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
    """Give a still-unnamed MCP session a meaningful title from its first
    exchange. Called once the first response for the session is received."""
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
                # Skip the synthesized audio placeholder.
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
    """Fetch binary image data from the API, returning (Image|None, error|None).

    Unlike _call, this handles raw bytes: the response body is wrapped in a
    FastMCP Image so clients receive a real image content block instead of
    text.
    """
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


# --- PROFILE ---


@mcp.tool()
async def get_user_context() -> str:
    """Read the current user's persistent profile / memory context.

    Returns a JSON object:
      {
        "context": "<free-text profile notes saved across ALL conversations>",
        "username": "<owner of this gateway identity>",
        "context_file": "<server-side path of the context file>"
      }

    The context holds durable facts about the user (preferences, personal
    details, ongoing projects) that survive across sessions. Use it to
    personalize answers BEFORE asking the user to repeat known information.
    This is memory, NOT conversation history — use get_session_messages for
    the transcript of a specific conversation.

    No parameters. Read-only; it never modifies anything.
    """
    return await _call("GET", "/api/user-context")


# --- CHAT ---


@mcp.tool()
async def list_sessions() -> str:
    """List all chat sessions of the user, most recently active first.

    Returns a JSON array (possibly empty), one entry per session:
      [
        {
          "session_id": "<uuid — pass this to other chat tools>",
          "name": "<sidebar title, e.g. 'New Chat'>",
          "created": <unix epoch seconds>,
          "updated": <unix epoch seconds>,
          ...context-token accounting fields...
        },
        ...
      ]

    Use this FIRST whenever a task references "the conversation/chat" without
    an explicit id: pick the relevant session (usually the one with the most
    recent "updated"), then call get_session_messages on its session_id.
    Sessions listed here are owned exclusively by this gateway's user.
    """
    return await _call("GET", "/api/sessions")


@mcp.tool()
async def create_session(system_prompt: str = "", system_prompts: list = None) -> str:
    """Create a brand-new empty chat session and return its id.

    Args:
      system_prompt: optional single system prompt string applied to the new
        conversation (persona/instructions). Omit for the default behavior.
      system_prompts: optional list of additional named system-prompt entries
        supported by the backend (advanced; rarely needed).

    Returns JSON: {"session_id": "<uuid>"}.

    The new session starts with no messages and the name "New Chat". To send
    the first message into it, pass this session_id to send_chat_message.
    Creating a session does NOT automatically switch any UI state — it is a
    pure data operation.

    GUARDRAIL: system prompts are screened by the input guardrail; a prompt
    that tries to disable safety rules is declined and no session is created.
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
    for sp in sp_texts:
        if sp and await asyncio.to_thread(
            llm_classify_harmful, sp, None, 20, JUDGE_FAIL_CLOSED
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
    """Read the full message transcript of one chat session.

    Args:
      session_id: uuid of the session (from list_sessions or create_session).

    Returns JSON:
      {
        "messages": [ {"role": "user"|"assistant"|..., "content": "...",
                       ...attachment/image fields when present...}, ... ],
        ...context-token accounting fields...
      }

    Messages are ordered oldest → newest, exactly as rendered in the web UI.
    Errors: HTTP-404 wrapped as {"error": "Upstream 404", ...} when the id is
    unknown or belongs to another user. Use after get_message_status reports
    "done" to read the assistant's reply (it is the last message).
    """
    res = await _call("GET", f"/api/sessions/{session_id}/messages")
    raw = res.text if hasattr(res, "text") else res
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    blocked = False
    judged = 0
    strict_judged = 0
    if isinstance(data, dict):
        for msg in data.get("messages", []):
            if msg.get("role") != "assistant":
                continue
            if msg.get("_guardrail_blocked"):
                blocked = True
                continue
            content = msg.get("content", "")
            scan = content if isinstance(content, str) else json.dumps(content)
            if not scan:
                continue
            harmful = is_harmful_content(scan)
            if not harmful and len(scan) > 50 and judged < 5:
                harmful = await asyncio.to_thread(
                    llm_classify_harmful_output, scan, None, 20, False
                )
                judged += 1
            if harmful:
                print(f"[guardrail] redacted harmful assistant message in session {session_id}")
                msg["content"] = HARMFUL_DECLINE
                msg["_guardrail_blocked"] = True
                blocked = True
                continue
            # ── Strict output judge (final end-of-pipe gate) ────────────
            # After the existing harmful-content checks, run the strictest
            # possible judge: pattern match for ALL prohibited categories
            # (sexual, violent, gore, death, suicide, murder, graphic,
            # illegal) plus prompt/input/system-prompt leaking.  If any
            # pattern matches, immediately decline — no LLM judge needed.
            # If patterns miss, still call the LLM judge (fail-closed) as
            # the final arbiter.  This catches edge cases the existing
            # output judge misses (e.g. subtle sexual content, prompt
            # leaking, or content that is harmful but not how-to related).
            if strict_judged < 3:
                if is_strict_output_blocked(scan):
                    print(f"[guardrail][strict] pattern-blocked assistant message in session {session_id}")
                    msg["content"] = HARMFUL_DECLINE
                    msg["_guardrail_blocked"] = True
                    blocked = True
                    continue
                strict_harmful = await asyncio.to_thread(
                    mcp_output_judge, scan, None, 90, True
                )
                strict_judged += 1
                if strict_harmful:
                    print(f"[guardrail][strict] judge-blocked assistant message in session {session_id}")
                    msg["content"] = HARMFUL_DECLINE
                    msg["_guardrail_blocked"] = True
                    blocked = True
                    continue
    if blocked:
        return json.dumps(data)
    return raw


@mcp.tool()
async def rename_session(session_id: str, name: str) -> str:
    """Rename a chat session (the title shown in the UI sidebar).

    Args:
      session_id: uuid of an existing session.
      name: the new human-readable title (short is best).

    Returns JSON: {"status": "updated"}.

    Purely cosmetic metadata — renaming never alters the messages. Useful
    after the first exchange so the conversation can be recognized later in
    list_sessions.
    """
    return await _call("PUT", f"/api/sessions/{session_id}", json={"name": name})


@mcp.tool()
async def send_chat_message(
    session_id: str,
    message: str,
    research: bool = False,
    cpu: bool = False,
    no_tools: bool = False,
) -> str:
    """Submit a user message into a chat session for ASYNC processing.

    This is the equivalent of typing into the web chat box and pressing send.
    It ENQUEUES the message and returns immediately — the model has NOT yet
    answered when this call returns.

    SPEED EXPECTATION — generation is SLOW by design:
      simple replies ......... ~40–90 seconds
      tool flows (images,
      web search, files) ..... often 2–3 minutes
      research mode .......... frequently 3–7 minutes
    Treat long stretches of "working" as NORMAL. Never declare failure,
    retry, or re-send just because the answer hasn't appeared yet — check
    get_message_status instead.

    POLLING STRATEGY:
    - First 60s: Poll every 20-30 seconds.
    - 1 minute to 2 mins: Poll every 30-40 seconds.
    - After 2 mins (Image Gen / Research): Poll every 45-60 seconds.
    Do NOT poll rapidly (under 15s).

    Args:
      session_id: target conversation (from list_sessions/create_session).
      message: the user's message text.
      research: enable the deep web-research pipeline (slower, cited reports).
      cpu: force the request onto the CPU model lane instead of GPU.
      no_tools: disable the model's built-in tool calling for this turn.

    Returns JSON: {"task_id": "<uuid>", "wait_hint": "<polling guidance>"}.

    GUARDRAIL: messages matching known injection patterns ("ignore previous
    instructions", "jailbreak", ...) are declined locally with
    {"declined": true} — no task is created. Everything else is forwarded
    wrapped in the server-side safety frame; the model may answer borderline
    content itself with "Request declined." — treat that as a normal reply.

    REQUIRED follow-up: poll get_message_status(task_id) until the status is
    a terminal value ("done", "error" or "cancelled"), then fetch the reply
    with get_session_messages(session_id). Never assume the answer exists
    right after this call.
    """
    print(f"[MCP MESSAGE REQUEST]: {message}")
    if is_jailbreak_attempt(message):
        print(f"[JAILBREAK DETECTED FOR]: {message}")
        return json.dumps({
            "declined": True,
            "response": GUARDRAIL_DECLINE,
            "detail": "message blocked by MCP input guardrail",
        })
    if is_harmful_request(message):
        print(f"[HARMFUL INTENT DETECTED FOR]: {message}")
        return json.dumps({
            "declined": True,
            "response": HARMFUL_DECLINE,
            "detail": "message blocked by MCP harmful-content guardrail",
        })
    # LLM safety judge: pre-call the inference engine for a single HARMFUL/SAFE
    # verdict so non-English (e.g. French/Spanish) and paraphrased requests are
    # also caught before any real generation happens. Runs on the raw message.
    if await asyncio.to_thread(llm_classify_harmful, message, None, 20, JUDGE_FAIL_CLOSED):
        print(f"[LLM VERIFIED HARMFUL INTENT DETECTED FOR]: {message}")
        return json.dumps({
            "declined": True,
            "response": HARMFUL_DECLINE,
            "detail": "message blocked by MCP LLM safety judge",
        })
    payload = {
        "session_id": session_id,
        "message": wrap_user_message(message),
        "research": research,
        "cpu": cpu,
        "no_tools": no_tools,
    }
    res = await _call("POST", "/api/chat", json=payload)

    # Extract string content safely if _call returns a Response object
    raw_text = res.text if hasattr(res, "text") else res

    try:
        obj = json.loads(raw_text)
    except (ValueError, TypeError):
        return raw_text

    if isinstance(obj, dict) and "error" not in obj:
        task_id = obj.get("task_id", "")

        if research:
            init_delay = 60
            mode_desc = "Deep research mode enabled (est. 3-7 mins)"
        elif not no_tools:
            init_delay = 30
            mode_desc = "Tool execution pipeline active (est. 2-3 mins)"
        else:
            init_delay = 20
            mode_desc = "Standard text generation (est. 40-90s)"

        obj["wait_hint"] = (
            f"{mode_desc}. Do NOT poll immediately. "
            f"Sleep at least {init_delay} seconds BEFORE calling get_message_status(task_id='{task_id}') for the first time. "
            f"Never poll faster than every 15-20 seconds."
        )
        return json.dumps(obj)

    return raw_text


@mcp.tool()
async def get_message_status(task_id: str) -> str:
    """Check the processing state of one submitted chat message.

    Args:
      task_id: the uuid returned by send_chat_message.

    Returns JSON describing the task. "status" progresses:
      "queued"    → accepted, waiting for a worker
      "working"   → the LLM is generating ("message" may hold progress text)
      "done"      → finished; the reply IS persisted in the session —
                    retrieve it with get_session_messages
      "error"     → failed; details in the payload
      "cancelled" → cancelled by the user/server
    An unknown task_id yields {"status": "unknown"}.

    PATIENCE: generation on a local LLM routinely takes 40–90 seconds and
    up to 3–7 minutes for heavy tasks (research mode, image pipelines).

    POLLING STRATEGY:
    - First 60s: Poll every 20-30 seconds.
    - 1 minute to 2 mins: Poll every 30-40 seconds.
    - After 2 mins (Image Gen / Research): Poll every 45-60 seconds.
    Do NOT poll rapidly (under 15s).

    This tool NEVER returns the reply text itself — always read the final answer
    via get_session_messages.
    """
    res = await _call("GET", f"/api/status/{task_id}")
    raw_text = res.text if hasattr(res, "text") else res

    try:
        obj = json.loads(raw_text)
    except (ValueError, TypeError):
        return raw_text

    if isinstance(obj, dict):
        status = obj.get("status")
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
                # Give the session a meaningful title from its first exchange
                # so MCP-created sessions stop showing the system prompt.
                await _maybe_auto_rename(sid)
        return json.dumps(obj)

    return raw_text


# ─────────────────────────────────────────────────────────────────────────────
# Bulk processing — batch chat prompts.
#
# start_chat_batch enqueues N prompts and returns a batch_id immediately. The
# batch is persisted to SQLite (server/batches_db.py) with a status flag —
# PENDING → WORKING → COMPLETED|ERROR — and a SINGLE background worker drains
# the queue one batch at a time (no per-request asyncio task; concurrent
# batches simply queue up). ERROR means every item failed; partial failures
# still close as COMPLETED with failed_indexes listing the dead items. Within a batch, items run ONE AT A TIME too: the GPU lane
# has a single LLM worker and its queue caps at 5, so firing in parallel would
# just 503. Items finish in waves; get_batch_status reports progress plus
# new_indexes (terminal replies never fetched yet), and every successful
# get_batch_results fetch marks its items collected so later polls only show
# genuinely new work. The flow is bidirectional: submit_batch_results lets
# the client attach its own per-item outcomes (grades, notes, anything) back
# onto the batch in ONE call, riding out again via get_batch_results.
# Batches survive gateway restarts: WORKING rows found at boot are re-queued.
# ─────────────────────────────────────────────────────────────────────────────

BATCH_MAX_ITEMS = 50            # hard cap per batch
BATCH_POLL_INTERVAL = 15        # seconds between /api/status polls per item
BATCH_ITEM_TIMEOUT = 2400       # seconds before one item is abandoned
BATCH_CHAT_SUBMIT_RETRIES = 3   # retries when the lane queue answers 503
BATCH_KEEP = 50                 # most recent batches kept in the DB
BATCH_WORKER_IDLE_SLEEP = 2     # seconds between DB polls when queue is empty


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

            task_id = ""
            for attempt in range(BATCH_CHAT_SUBMIT_RETRIES):
                res = await _call(
                    "POST",
                    "/api/chat",
                    json={
                        "session_id": sid,
                        "message": wrap_user_message(it["prompt"]),
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
            # ── Output guardrail ──────────────────────────────────────────────
            # The model may have complied with a harmful request that dodged the
            # input filters; scan its reply (pattern + LLM judge) and redact
            # before the reply is ever persisted or returned to the client.
            if reply:
                harmful = is_harmful_content(reply) or await asyncio.to_thread(
                    llm_classify_harmful_output, reply, None, 20, False
                )
                if harmful:
                    print(
                        f"[guardrail] redacted harmful batch reply "
                        f"(batch {batch_id}, item {it['index']})"
                    )
                    reply = HARMFUL_DECLINE
                    item_update(
                        batch_id, it["index"], status="done",
                        reply=reply, guardrail_blocked=1,
                    )
                    continue
                # ── Strict output judge (final end-of-pipe gate) ─────────
                # After the existing harmful-content checks, run the strictest
                # possible judge covering ALL prohibited categories plus
                # prompt/input/system-prompt leaking.  Fail-closed: if the
                # judge is down, the reply is blocked.
                if is_strict_output_blocked(reply) or await asyncio.to_thread(
                    mcp_output_judge, reply, None, 90, True
                ):
                    print(
                        f"[guardrail][strict] blocked batch reply "
                        f"(batch {batch_id}, item {it['index']})"
                    )
                    reply = HARMFUL_DECLINE
                    item_update(
                        batch_id, it["index"], status="done",
                        reply=reply, guardrail_blocked=1,
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
    """Single queue drainer: claims PENDING batches from SQLite one at a
    time and runs them to completion. Replaces the old spawn-a-task-per-
    request model; concurrent submissions simply wait their turn."""
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
            # Never let one bad batch wedge the queue: fail its open items
            # so the batch can be closed out and the worker moves on.
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
    """Submit MANY chat prompts at once and get a batch_id back immediately.

    The batch is persisted to a SQLite queue and picked up by a single
    background worker that processes batches (and the items inside them)
    SEQUENTIALLY through the normal chat pipeline (tools, research mode and
    sampling all behave exactly like send_chat_message), so a batch of N
    messages takes roughly N × single-turn time. If other batches are
    already PENDING, this one waits its turn — check "status" /
    "queue_position" via get_batch_status. This tool never blocks on
    generation.

    Args:
      prompts: list of message strings to process, e.g.
        ["Summarise X", "Draft a poem about Y"]. Max 50 items.
      shared_session: false → every prompt gets its own fresh session
        (independent answers, no cross-contamination — recommended).
        true → all prompts run inside ONE new session and see each other's
        context, like a conversation.
      system_prompt: optional persona/instructions applied to created sessions.
      research / cpu / no_tools: same meaning as send_chat_message, applied to
        every item.
      session_ids: optional list, SAME length and order as prompts.
        prompts[i] then runs INSIDE the existing conversation session_ids[i]
        instead of a fresh session — this is how a "round 2" batch continues
        many round-1 chats in one call. Get each prior item's session_id
        from get_batch_results on the base batch. Incompatible with
        shared_session and system_prompt; an unknown id surfaces later as
        that item's per-item error (indexes stay stable).

    Returns JSON:
      {"batch_id": "<id>", "total": <n>, "status": "PENDING",
       "queue_position": <pending batches ahead>,
       "guardrail_blocked_indexes": [..], "note": "..."}.

    GUARDRAIL: prompts matching known injection patterns are rejected
    upfront — they appear in guardrail_blocked_indexes and as error items
    (indexes stay stable); if EVERY prompt is blocked, the whole batch is
    declined and no batch is created.

    TIMING — items run SEQUENTIALLY, so budget generously:
      standard text batch ......... ~2 min PER ITEM
      tools/images batch .......... ~3-5 min PER ITEM
      research batch .............. ~5-7 min PER ITEM
     WORKFLOW — repeat per polling wave until percent_complete == 100:
      1. Sleep ~30 minutes, then call get_batch_status(batch_id).
      2. Read its new_indexes (terminal replies you have NOT fetched yet).
      3. Fetch them: get_batch_results(batch_id, new_indexes) — they get
         marked collected and won't reappear in later waves.
      4. Optionally send your per-item outcomes back IN ONE CALL:
         submit_batch_results(batch_id, [{"index": i, "result": ...}, ...]).
      5. Re-poll every ~30 minutes; newly finished items show up in
         new_indexes automatically.
    Never poll faster than every 30 minutes; process waves incrementally —
    do not wait for the whole batch to finish.
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
    if is_jailbreak_attempt(system_prompt or ""):
        return json.dumps({
            "declined": True,
            "response": GUARDRAIL_DECLINE,
            "detail": "system_prompt blocked by MCP input guardrail",
        })
    blocked = [i for i, p in enumerate(prompts)
               if is_jailbreak_attempt(p) or is_harmful_request(p)]
    # LLM safety judge on the prompts the cheap patterns didn't already catch.
    for i, p in enumerate(prompts):
        if i in blocked:
            continue
        if p and await asyncio.to_thread(
            llm_classify_harmful, p, None, 20, JUDGE_FAIL_CLOSED
        ):
            blocked.append(i)
    blocked = sorted(set(blocked))
    kept = [p for i, p in enumerate(prompts) if i not in blocked]
    if not kept:
        return json.dumps({
            "declined": True,
            "response": GUARDRAIL_DECLINE,
            "detail": "every prompt was blocked by the MCP input guardrail",
            "blocked_indexes": blocked,
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
    for i in blocked:
        item_update(
            batch_id,
            i,
            status="error",
            error=f"{GUARDRAIL_DECLINE} (blocked by MCP input guardrail)",
        )

    ahead = queue_position(batch_id)

    per_item_min = 7 if (research or not no_tools) else 2
    est_total = len(kept) * per_item_min
    return json.dumps({
        "batch_id": batch_id,
        "total": len(prompts),
        "status": BATCH_PENDING,
        "queue_position": ahead,
        "guardrail_blocked_indexes": blocked,
        "mode": (
            "shared_session" if shared_session
            else ("bound_sessions" if session_ids else "per_item_sessions")
        ),
        "est_total_minutes": est_total,
        "note": (
            f"{len(kept)} of {len(prompts)} prompts queued SEQUENTIALLY "
            f"(~{per_item_min} min per item, ≈{est_total} min total"
            + (f"; {len(blocked)} prompt(s) blocked by guardrail" if blocked else "")
            + (f"; behind {ahead} earlier batch(es)" if ahead else "")
            + "). Do NOT poll immediately: "
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
    """LIGHTWEIGHT progress check for a bulk-chat batch — no reply text here.

    Args:
      batch_id: the id returned by start_chat_batch.

    Poll this every ~30 MINUTES while the batch runs (items take minutes
    each and are processed sequentially; faster polling wastes calls).
    Items finish in WAVES, so each poll may reveal new work.

    Returns JSON:
      {
        "batch_id": ..., "status": "PENDING"|"WORKING"|"COMPLETED"|"ERROR",
        "total": n,
        "progress": "15 out of 20 items completed",
        "percent_complete": 75,
        "done": x, "error": y, "running": r, "queued": q,
        "completed_indexes": [0, 1, 2, ...],   ← terminal (done or error)
        "failed_indexes": [7],
        "new_indexes": [3, 4, 7],   ← terminal AND never fetched yet
        "results_submitted": 5,     ← items carrying your submitted result
        "queue_position": 0,        ← PENDING only: batches ahead of this one
        "note": "..."
      }
    ERROR means EVERY item failed; if some succeeded the batch is COMPLETED
    and its failures show up in failed_indexes. The batch is finished only
    at percent_complete == 100 (status COMPLETED or ERROR). Each polling wave: fetch whatever appears in new_indexes via
    get_batch_results — fetched items are marked collected and drop OUT of
    new_indexes on later polls, so you never re-process old replies. What
    you do with each reply (grading, summarising, forwarding) is up to you;
    optionally push your per-item outcome back with submit_batch_results.
    Unknown ids yield {"error": "unknown batch_id"}.
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
    """Collect assistant replies produced by a bulk-chat batch — per wave.

    Args:
      batch_id: the id returned by start_chat_batch.
      indexes: list of item indexes to fetch. Omit to fetch EVERY item
        finished so far in one shot.
      new_only: true → skip items already fetched before (every successful
        fetch marks the reply 'collected'). Handy with no indexes to grab
        "everything new since my last poll" — replies never show up twice.

    Each fetched item records a collected_at timestamp; get_batch_status
    uses it to compute new_indexes for subsequent waves. What you do with
    the replies is up to you (grade, summarise, forward...); optionally
    attach your own per-item outcome via submit_batch_results.

    Returns a JSON array:
      [{"index": 0, "status": "done", "prompt": "<original>",
        "reply": "<full assistant answer>", "session_id": "<uuid>",
        "collected_at": <unix ts>,
        "submitted_result": <your attached result, if any>},
       {"index": 7, "status": "error", "prompt": "...",
        "error": "<why it failed>"},
       {"index": 99, "error": "unknown index"}]

    Items still running/queued are simply absent unless requested explicitly,
    in which case they appear with status "running"/"queued" and no reply
    and are NOT marked collected. Fetch incrementally — never wait for 100%.
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
            out.append(it)  # unknown-index placeholder — pass through as-is
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
            # Defensive redaction: in case a reply slipped the worker's output
            # scan (e.g. stored by an older build), never hand harmful content
            # back to the client.
            if reply and it.get("guardrail_blocked") != 1:
                if is_harmful_content(reply):
                    reply = HARMFUL_DECLINE
                elif is_strict_output_blocked(reply):
                    reply = HARMFUL_DECLINE
                else:
                    strict_blocked = await asyncio.to_thread(
                        mcp_output_judge, reply, None, 90, True
                    )
                    if strict_blocked:
                        reply = HARMFUL_DECLINE
            entry["reply"] = reply
            entry["session_id"] = it["session_id"]
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
    """Push YOUR OWN per-item outcomes back onto a batch — in ONE call.

    The counterpart to get_batch_results: after collecting a batch's
    assistant replies, send YOUR results for them back IN A BATCH instead
    of one request per item. A "result" is whatever your pipeline produces
    per reply — a grade/score, a summary, an annotation, a follow-up flag,
    any JSON payload. Each submission is stored on its batch item and
    travels back out through get_batch_results, so the full prompt → reply
    → outcome round trip lives on the same batch_id.

    Args:
      batch_id: the id returned by start_chat_batch.
      results: list of objects, one per item:
        [{"index": 0, "result": "grade: 4/5 — concise, accurate"},
         {"index": 7, "result": {"score": 2, "reason": "hallucinated"}}]
        - "index": must match an item index of this batch.
        - "result": any JSON-serialisable payload — string, number or
          object. Extra keys inside an entry are ignored.

    Returns JSON:
      {"batch_id": ..., "accepted": [0, 7],
       "rejected": [{"index": 99, "error": "unknown index"}],
       "results_submitted": 2,
       "note": "..."}
    Re-submitting an index OVERWRITES that item's previous result. Items
    need not be finished (or even running) to accept a submission — grading
    may legitimately happen out of order.

    WAVES: batches finish incrementally, so this is normally called once per
    polling wave, not once per batch:
      1. get_batch_status → new_indexes (terminal, not yet fetched)
      2. get_batch_results(batch_id, new_indexes) → replies
      3. process them → ONE submit_batch_results call with all outcomes
      4. repeat ~30 min later; fetched indexes vanish from new_indexes.
    Unknown batch ids yield {"error": "unknown batch_id"}; entries without a
    usable "index" or missing "result" are rejected individually and
    reported back.
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
    """Fetch and display an image referenced in a chat conversation.

    Chat messages reference images as site-relative URLs, e.g.:
      - "/uploads/<name>.jpg"  — files uploaded by the user
      - "/output/<file>.png"   — images generated by ComfyUI in-chat

    These markers appear in message content (often as "[IMAGE: /output/...]")
    or in attachment fields of messages returned by get_session_messages.
    Such paths are NOT resolvable by the client itself — they only exist on
    this server. Pass the path/identifier to this tool to obtain the actual
    image.

    Args:
      image_id: the image reference exactly as it appears in the message,
        e.g. "/uploads/photo.jpg", "/output/art_00001_.png", or a bare id
        accepted by the backend's resolver. Do not prepend a hostname.

    Returns:
      A rendered image content block the client can display directly, so the
      conversation can "see" what was generated or uploaded. On failure
      (unknown/expired file, upstream error), returns a JSON error object —
      check for that before treating the result as an image.

    Typical use: after get_session_messages shows an assistant reply that
    generated an image, call this with the referenced path so you can view
    and describe it to the user. Images are served read-only through the
    authenticated gateway; there is deliberately no way to upload or modify
    images via MCP.
    """
    img, err = await _call_image(f"/api/image/{image_id.lstrip('/')}")
    return err if err else img


import secrets, hashlib
from starlette.responses import RedirectResponse

_auth_codes = {}
_auth_codes_lock = threading.Lock()
AUTH_CODE_TTL = 300


async def oauth_metadata(request):
    """RFC 8414 authorization-server metadata. Claude's connector discovers
    this from the base URL to find /authorize and /oauth/token."""
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
    """Authorization-code leg. Single-tenant server: reaching this endpoint
    at all already implies it's the owner (no separate login screen) — we
    just check client_id and mint a short-lived code tied to the PKCE
    challenge, then bounce back to Claude's redirect_uri."""
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
        return True  # PKCE not used by this client
    if method == "plain":
        return verifier == challenge
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    return calc.rstrip(b"=").decode() == challenge


async def oauth_token(request):
    """Token endpoint: supports the authorization_code grant Claude actually
    uses (code + PKCE verifier from /authorize), and client_credentials as a
    fallback for other callers. Either way the issued access token is
    MCP_OAUTH_CLIENT_SECRET, which the middleware below validates."""
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
    # Confidential clients that pass a secret must get it right; public
    # clients (PKCE-only, no secret — Claude's default) skip this check.
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
    """RFC 9728 protected-resource metadata. Claude checks this (both the
    bare path and the /mcp-suffixed variant) to confirm which authorization
    server backs this resource before it will call /mcp at all."""
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
    _worker_task = asyncio.create_task(_batch_worker())
    try:
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
    finally:
        if _worker_task:
            _worker_task.cancel()
            try:
                await _worker_task
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
