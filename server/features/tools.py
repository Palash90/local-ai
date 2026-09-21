"""LLM tool implementations: web search, page fetching, image tools dispatch."""

import ipaddress
import json
import os
import re
import socket
import threading
from datetime import datetime
from urllib.parse import urlparse

from server.mcp_client import mcp_manager, dispatch_mcp_tool
from server.features.state import M
from server.features.websearch import fetch_page, web_search
from server.features.websearch import relevance as _relevance
from server.features.websearch import vector_store as page_cache
from server.features.pensieve import memory_read as _pensieve_read

# Private names remain available to older focused checks and integrations.
_screen_cached_payload = _relevance._screen_cached_payload


# ── Headed-browser navigation gate ──────────────────────────────────────────
# browser__browser_navigate drives a local Chromium, so its SSRF surface is
# the same as fetch_page's: refuse loopback/LAN/metadata targets server-side
# (prompt text alone can't be trusted). Also refused while an image render
# owns the machine — Chromium on top of ComfyUI is the one shape that could
# make text-phase fetching trip a RAM evacuation.

def _browser_navigate_gate(tool_name, args):
    """Return an error string refusing the navigation, or "" to allow it."""
    if tool_name != "browser__browser_navigate":
        return ""
    try:
        image_active = bool(M._image_active)
    except Exception:
        image_active = False
    if image_active:
        return (
            "Browser navigation skipped: an image render owns the machine "
            "right now. Fall back to the direct fetch_page result or search "
            "snippets instead."
        )
    url = ""
    try:
        if isinstance(args, dict):
            url = (args.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Refused to open URL (only http/https allowed): {url}"
        host = (parsed.hostname or "").lower()
        if (
            host in ("localhost", "metadata.google.internal")
            or host.endswith(".local")
            or host.endswith(".internal")
            or host.endswith(".lan")
        ):
            return f"Refused to open private/internal address: {url}"
        ip = socket.gethostbyname(parsed.hostname or "")
        addr = ipaddress.ip_address(ip)
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or getattr(addr, "is_reserved", False)
        ):
            return f"Refused to open private/internal address: {url}"
    except Exception as e:
        return f"Refused to open URL ({e}): {url}"
    return ""


# ── Headed-browser read persistence ─────────────────────────────────────────
# Browser-opened pages must persist exactly like direct fetch_page results:
# a fetch_page-shaped _search_details entry (so citation verification treats
# them as grounded) plus a page_cache write on cache miss (so repeats,
# chunked re-reads and the semantic layer reuse them without relaunching
# Chromium). Browser fills misses only — a good direct entry is never
# overwritten here. TTL policy is identical to direct fetches
# (page_put's default). Best-effort: never raises.

_BROWSER_TEXT_CAP = 24000  # mirrors fetch_page's default max_chars


def _browser_extract_evaluate_text(result):
    """Pull the innerText out of a browser__browser_evaluate result."""
    text = result if isinstance(result, str) else ""
    if not text or "### Result" not in text:
        return ""
    body = text.split("### Result", 1)[1]
    body = body.split("### Ran Playwright code", 1)[0]
    body = body.strip()
    # The server JSON-quotes the evaluated string.
    if len(body) >= 2 and body[0] == '"' and body[-1] == '"':
        try:
            body = json.loads(body)
        except Exception:
            body = body[1:-1]
    body = body.replace("\\n", "\n").strip()
    return body


def _browser_note_result(task_id, tool_name, args, result):
    """Record a headed-browser read; see the block comment above."""
    try:
        if not isinstance(args, dict):
            return
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t is None:
                return
            if tool_name == "browser__browser_navigate":
                url = (args.get("url") or "").strip()
                if not url or not isinstance(result, str) or "Page URL:" not in result:
                    return
                title = ""
                m = re.search(r"^- Page Title:\s*(.+)$", result, re.MULTILINE)
                if m:
                    title = m.group(1).strip()
                t["_browser_url"] = url
                t["_browser_title"] = title
                return
            if tool_name != "browser__browser_evaluate":
                return
            url = (t.get("_browser_url") or "").strip()
            if not url:
                return
            title = t.get("_browser_title") or ""
            text = _browser_extract_evaluate_text(result)
            if not text:
                return
            text = text[:_BROWSER_TEXT_CAP]
            t.setdefault("_search_details", []).append(
                {
                    "tool": "fetch_page",
                    "url": url,
                    "title": title,
                    "content": text,
                    "error": "",
                    "via": "browser",
                    "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            canon = url.split("#", 1)[0] or url
            try:
                if page_cache.page_get(canon) is None:
                    page_cache.page_put(
                        canon, url, title, text, doc_type="web",
                    )
                else:
                    print(f"[browser-persist] cache hit for {canon} — details only")
            except Exception as e:
                print(f"[browser-persist] cache write failed: {e}")
            print(f"[browser-persist] recorded {len(text)} chars from {canon}")
    except Exception as e:
        print(f"[browser-persist] skipped: {e}")


def _tool_worker(task_id, sid, tc, image_b64, round_num, tool_index):
    tool_name = tc["function"]["name"]
    try:
        M._dispatch_tool(task_id, sid, tc, image_b64, round_num, tool_index)
    except Exception as e:
        print(f"[tool_worker] Tool '{tool_name}' crashed for task {task_id}: {e}")
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc.get("id", ""),
            result=json.dumps({"error": f"Tool {tool_name} failed: {e}"}),
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )


def _dispatch_tool(task_id, sid, tc, image_b64, round_num, tool_index):
    tool_name = tc["function"]["name"]
    try:
        args = json.loads(tc["function"]["arguments"])
    except Exception:
        args = {}

    with M._data_lock:
        tu = list(M.tasks.get(task_id, {}).get("_tools_used", []))
    has_generated_image = "generate_image" in tu

    if tool_name == "get_user_location":
        if M._client_location:
            result = M._client_location
        else:
            ev = threading.Event()
            M._location_events[task_id] = ev
            M.set_status(task_id, "location_needed")
            ev.wait(timeout=60)
            M._location_events.pop(task_id, None)
            result = (
                M._client_location
                if M._client_location
                else "User denied location access"
            )
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
        return

    if tool_name == "read_file":
        file_url = args.get("file_url", "")
        filename = os.path.basename(urlparse(file_url).path)
        fpath = os.path.abspath(os.path.join(M.UPLOADS_DIR, filename))
        if fpath.startswith(os.path.abspath(M.UPLOADS_DIR)) and os.path.exists(fpath):
            text = M.read_file_text(fpath)
            if text:
                markdown_match = re.search(r"\[Markdown saved: (/[^]]+\.md)\]", text)
                if markdown_match:
                    artifact_url = markdown_match.group(1)
                    artifact = {
                        "type": "markdown",
                        "name": os.path.basename(artifact_url),
                        "mime_type": "text/markdown",
                        "url": artifact_url,
                    }
                    with M._data_lock:
                        task = M.tasks.get(task_id)
                        if task:
                            task.setdefault("_artifacts", []).append(artifact)
                result = (
                    f"Content of {file_url}:\n\n{text}\n\n"
                    "This content came from PDF extraction/OCR. Preserve the original "
                    "Unicode text, and use the page headings when quoting or formatting it."
                )
            else:
                result = f"Could not extract text from {file_url}. The file may contain only images."
        else:
            result = f"File not found: {file_url}"
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
        return

    if tool_name == "read_image":
        url = args.get("url", "")
        fpath = M.resolve_image_path(url)
        if fpath is None:
            result = json.dumps({"ok": False, "error": f"Image not found: {url}"})
        else:
            result = json.dumps({"ok": True, "image_url": url})
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
        return

    if tool_name == "web_search":
        M.set_status(task_id, f"Searching web for: {args.get('query')}...")
        with M._data_lock:
            client_ts = M.tasks.get(task_id, {}).get("_client_timestamp")
        try:
            result = M.web_search(
                args["query"],
                current_time=args.get("current_time"),
                current_location=args.get("current_location"),
                force_refresh=bool(args.get("force_refresh", False)),
            )
        except Exception as e:
            print(f"[web_search] Unhandled exception for task {task_id}: {e}")
            result = json.dumps(
                {"results": [], "query": args.get("query"), "error": str(e)}
            )
        print(f"[web_search] RAW result for task {task_id}: {result[:300]}...")  # DEBUG
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                t.setdefault("_tools_used", []).append(tool_name)
                try:
                    search_payload = json.loads(result)
                    t.setdefault("_search_details", []).append(search_payload)
                    # An empty low-confidence search cannot provide evidence.
                    # Prevent a research model from issuing dozens of variant
                    # searches and appending the same failure until context is
                    # exhausted; the next LLM round must answer unsupported.
                    if search_payload.get("low_confidence") and not search_payload.get("results"):
                        t["no_tools"] = True
                        t["_search_exhausted"] = True
                except Exception:
                    pass
        llm_result = (
            f"Web search results for query '{args.get('query')}'. "
            f"Analyze these search results thoroughly and provide a clear, accurate response based on the findings:\n\n{result}"
        )
        print(
            f"[web_search] LLM-bound result (with analysis instruction) for task {task_id}: {llm_result[:400]}..."
        )  # DEBUG
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=llm_result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )

    elif tool_name == "fetch_page":
        M.set_status(task_id, f"Fetching page: {args.get('url', '')}...")
        try:
            result = M.fetch_page(args.get("url", ""), chunk=args.get("chunk", 1))
        except Exception as e:
            print(f"[fetch_page] Unhandled exception for task {task_id}: {e}")
            result = json.dumps({"url": args.get("url", ""), "error": str(e)})
        print(f"[fetch_page] Result for task {task_id}: {result[:300]}...")  # DEBUG
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                t.setdefault("_tools_used", []).append(tool_name)
                try:
                    res = json.loads(result)
                    t.setdefault("_search_details", []).append(
                        {
                            "tool": "fetch_page",
                            "url": res.get("url", args.get("url", "")),
                            "title": res.get("title", ""),
                            "content": res.get("content", ""),
                            "error": res.get("error", ""),
                            "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        }
                    )
                except Exception:
                    pass
        llm_result = (
            f"Page content fetched from URL '{args.get('url')}'. "
            f"Use this content to answer the user's question accurately. "
            f"If the content is insufficient or was truncated, you may fetch another page or fall back to the search results:\n\n{result}"
        )
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=llm_result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )

    elif tool_name == "generate_music":
        M.set_status(task_id, "Composing music...")
        with M._data_lock:
            t_user = M.tasks.get(task_id, {}).get("_user", "")
        try:
            # Strict pre-render validation: an LLM-written score with parse
            # errors is refused BEFORE FluidSynth (no partial player, no
            # render cost) — the errors return as the tool result so the
            # next round fixes the tokens directly. Creativity stays at the
            # model's sampling; only the grammar is enforced mechanically.
            result = M.render_music_score(
                args.get("score", ""), tempo=int(args.get("tempo") or 120),
                title=args.get("title", "music"), user=t_user,
                strict=True,
            )
        except Exception as e:
            print(f"[generate_music] Unhandled exception for task {task_id}: {e}")
            result = json.dumps({"ok": False, "error": str(e)})
        try:
            res = json.loads(result)
        except Exception:
            res = {"ok": False, "error": "bad render result"}
        with M._data_lock:
            te = M.tasks.get(task_id)
            if te:
                te["music_errors"] = [str(x) for x in (res.get("errors") or [])][:20]
        if res.get("ok"):
            music_url = res.get("music_url", "")
            rel = music_url[len("/music/"):] if music_url.startswith("/music/") else None
            with M._data_lock:
                t = M.tasks.get(task_id)
                if t:
                    t.setdefault("_tools_used", []).append(tool_name)
                    t["music_file"] = rel
                    t["music_score"] = res.get("score", args.get("score", ""))
                    t["music_url"] = music_url
                    t["music_stream_url"] = res.get("music_stream_url")
                    t["music_levels"] = res.get("levels", [])
                    t["music_duration"] = res.get("duration_s")
                    # Plural channel: every render appends so multi-track
                    # tasks keep all audio (singular keys stay last-wins for
                    # critic/sessions/tests compatibility).
                    t.setdefault("music_files", []).append(
                        {
                            "rel": rel,
                            "url": music_url,
                            "stream_url": res.get("music_stream_url"),
                            "score": res.get("score", args.get("score", "")),
                            "levels": res.get("levels", []),
                            "duration_s": res.get("duration_s"),
                        }
                    )
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )

    elif tool_name == "generate_music_arranged":
        from server.config import MUSIC_ARRANGER_PARAMS
        M.set_status(task_id, "Composing music...")
        with M._data_lock:
            t_user = M.tasks.get(task_id, {}).get("_user", "")
        try:
            if not MUSIC_ARRANGER_PARAMS:
                raise ValueError(
                    "The arranged pipeline is disabled on this server "
                    "(MUSIC_ARRANGER_PARAMS=0); write a score with "
                    "generate_music instead.")
            from server.features.music.random_arrange import random_score
            _seed = args.get("seed")
            score_text, tempo, info = random_score(
                seed=None if _seed in (None, "") else _seed,
                mood=args.get("mood"), genre=args.get("genre"),
                fusion=args.get("fusion"), tempo=args.get("tempo"),
                scale_name=args.get("scale"), lead=args.get("lead"),
                bars=args.get("bars"))
            result = M.render_music_score(
                score_text, tempo=tempo,
                title=args.get("title", "music"), user=t_user,
            )
        except Exception as e:
            print(f"[generate_music_arranged] Unhandled exception for task {task_id}: {e}")
            result = json.dumps({"ok": False, "error": str(e)})
        try:
            res = json.loads(result)
        except Exception:
            res = {"ok": False, "error": "bad render result"}
        with M._data_lock:
            te = M.tasks.get(task_id)
            if te:
                te["music_errors"] = [str(x) for x in (res.get("errors") or [])][:20]
        if res.get("ok"):
            music_url = res.get("music_url", "")
            rel = music_url[len("/music/"):] if music_url.startswith("/music/") else None
            with M._data_lock:
                t = M.tasks.get(task_id)
                if t:
                    t.setdefault("_tools_used", []).append(tool_name)
                    t["music_file"] = rel
                    t["music_score"] = res.get("score") or score_text
                    t["music_url"] = music_url
                    t["music_stream_url"] = res.get("music_stream_url")
                    t["music_levels"] = res.get("levels", [])
                    t["music_duration"] = res.get("duration_s")
                    # Plural channel (see generate_music above).
                    t.setdefault("music_files", []).append(
                        {
                            "rel": rel,
                            "url": music_url,
                            "stream_url": res.get("music_stream_url"),
                            "score": res.get("score") or score_text,
                            "levels": res.get("levels", []),
                            "duration_s": res.get("duration_s"),
                        }
                    )
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round_num=round_num,
        )

    elif tool_name == "edit_image":
        # Attempt cap (mirrors generate_image's 1-per-task limit): validation
        # failures used to loop forever, each burning a full GPU unload /
        # ComfyUI recycle / reload cycle. Counted at dispatch so failures
        # count too, not just successes.
        from server.features.images import MAX_EDIT_ATTEMPTS_PER_TASK
        if tu.count("edit_image") >= MAX_EDIT_ATTEMPTS_PER_TASK:
            result = json.dumps(
                {"error": "Edit attempt limit reached for this task "
                          f"({MAX_EDIT_ATTEMPTS_PER_TASK} tries). Do not retry — "
                          "report the last error to the user instead."}
            )
            M._event_post(
                "tool_ok",
                task_id,
                tc_id=tc["id"],
                result=result,
                sid=sid,
                round=round_num,
                tool_index=tool_index,
            )
        else:
            with M._data_lock:
                t = M.tasks.get(task_id)
                if t:
                    t.setdefault("_tools_used", []).append("edit_image")
            M._enqueue_image_job(task_id, sid, tool_name, args, tc, round_num, tool_index)
        return

    elif tool_name == "generate_image":
        if has_generated_image:
            result = json.dumps(
                {"error": "Image generation limit reached for this prompt."}
            )
            M._event_post(
                "tool_ok",
                task_id,
                tc_id=tc["id"],
                result=result,
                sid=sid,
                round=round_num,
                tool_index=tool_index,
            )
        else:
            M._enqueue_image_job(
                task_id, sid, tool_name, args, tc, round_num, tool_index
            )
        return
    elif tool_name == "update_user_context":
        content = args.get("content", "")
        user = ""
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                user = t.get("_user", "")
        if user:
            M.write_user_context(user, content)
            print(f"[context] Updated context for user '{user}' ({len(content)} chars)")
        result = json.dumps({"status": "ok", "saved": bool(user)})
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
    elif tool_name == "manage_tasks":
        user = ""
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                user = t.get("_user", "")
        if not user:
            result = json.dumps({"ok": False, "error": "User not found"})
        else:
            result = M.handle_task_tool(user, args)
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
    elif tool_name == "track_theme":
        user = ""
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                user = t.get("_user", "")
        if not user:
            result = json.dumps({"ok": False, "error": "User not found"})
        elif user not in M._agent_users:
            result = json.dumps(
                {
                    "ok": False,
                    "error": "track_theme is reserved for the self-chat agent pipeline",
                }
            )
        else:
            result = M.handle_theme_tool(user, args)
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
    elif tool_name == "tool_details":
        wanted = [n.strip() for n in str(args.get("name", "")).split(",") if n.strip()]
        known = {t["function"]["name"]: t for t in M.live_tools_detailed()}
        with M._data_lock:
            req_user = M.tasks.get(task_id, {}).get("_user", "")
        if req_user not in M._agent_users:
            known = {n: t for n, t in known.items() if n not in M.AGENT_ONLY_TOOLS}
        found = [known[n] for n in wanted if n in known]
        if found:
            result = json.dumps(found)
            # Warm the per-user docs cache so future sessions skip this round
            # (agents keep their own prompt pipelines; humans/MCP only).
            if req_user and req_user not in M._agent_users:
                try:
                    from server.features import tool_docs
                    tool_docs.warm(
                        req_user, [e["function"]["name"] for e in found]
                    )
                except Exception as e:
                    print(f"[tool_docs] warm failed: {e}")
        else:
            result = json.dumps(
                {
                    "error": "Unknown tool(s)",
                    "requested": wanted,
                    "available": sorted(known),
                }
            )
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )

    elif tool_name == "memory_read":
        try:
            result = _pensieve_read(
                sid=sid,
                memory_ids=args.get("memory_ids"),
                query=args.get("query", ""),
                limit=args.get("limit", 5),
            )
        except Exception as e:
            print(f"[memory_read] Unhandled exception for task {task_id}: {e}")
            result = json.dumps(
                {"error": f"memory_read failed: {e}", "memory_ids": args.get("memory_ids")}
            )
        print(f"[memory_read] Result for task {task_id}: {result[:300]}...")  # DEBUG
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )

    elif mcp_manager.is_mcp_tool(tool_name):
        gate_err = _browser_navigate_gate(tool_name, args)
        if gate_err:
            print(f"[browser-gate] refused {tool_name}: {gate_err}")
            result = json.dumps({"error": gate_err})
        else:
            try:
                result = dispatch_mcp_tool(tool_name, args)
                _browser_note_result(task_id, tool_name, args, result)
            except Exception as e:
                print(f"[MCP] Tool '{tool_name}' failed: {e}")
                result = json.dumps({"error": f"MCP tool {tool_name} failed: {e}"})

        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
    else:
        result = json.dumps({"error": f"Unknown tool: {tool_name}"})
        M._event_post(
            "tool_ok",
            task_id,
            tc_id=tc["id"],
            result=result,
            sid=sid,
            round=round_num,
            tool_index=tool_index,
        )
