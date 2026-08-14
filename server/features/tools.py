"""LLM tool implementations: web search, page fetching, image tools dispatch."""

import concurrent.futures
import json
import os
import threading
from datetime import datetime
from urllib.parse import urlencode, urlparse

import requests

from server.features.state import M

# How many search results to hand back to the LLM, and how many of the top
# ones to enrich with the full page text (via fetch_page) so the LLM sees real
# content instead of only engine snippets.
WEB_SEARCH_RESULT_LIMIT = 10
WEB_SEARCH_ENRICH_TOP = 2
WEB_SEARCH_ENRICH_CHARS = 6000
WEB_SEARCH_ENRICH_TIMEOUT = 25


def web_search(query, current_time=None, current_location=None):
    ts = datetime.now()
    clean_query = query.strip()
    params = {"q": clean_query, "format": "json"}
    search_url = f"{M.SEARXNG_URL}?{urlencode(params)}"
    print("Performing web search", search_url)
    try:
        r = requests.get(M.SEARXNG_URL, params=params, timeout=10)
        r.raise_for_status()
        print("Web-search completed")
        data = r.json()
    except Exception as e:
        print(f"Web-search failed: {e}")
        return json.dumps({
            "results": [],
            "search_date": ts.strftime("%Y-%m-%d %A"),
            "query": query,
            "search_url": search_url,
            "error": str(e),
        })
    results = data.get("results", [])[:WEB_SEARCH_RESULT_LIMIT]
    formatted = []
    for x in results:
        formatted.append(
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "snippet": x.get("content", "") or x.get("snippet", ""),
            }
        )
    enriched = _enrich_top_results(formatted)
    return json.dumps(
        {
            "results": enriched,
            "search_date": ts.strftime("%Y-%m-%d %A"),
            "query": query,
            "search_url": search_url,
        }
    )


def _enrich_top_results(results):
    """Attach the full page text to the top results.

    The top ``WEB_SEARCH_ENRICH_TOP`` results are fetched concurrently (their
    body is stored as ``full_content``) so the LLM does not have to make a
    separate ``fetch_page`` call for every promising link. Fetch failures are
    recorded as ``fetch_error`` and never break the search response.
    """
    targets = results[:WEB_SEARCH_ENRICH_TOP]
    if not targets:
        return results

    def _one(entry):
        url = entry.get("url", "")
        if not url.lower().startswith(("http://", "https://")):
            return
        try:
            page = json.loads(
                M.fetch_page(url, max_chars=WEB_SEARCH_ENRICH_CHARS)
            )
            if page.get("content"):
                entry["full_content"] = page["content"]
                entry["page_title"] = page.get("title", "")
            elif page.get("error"):
                entry["fetch_error"] = page["error"]
        except Exception as e:
            entry["fetch_error"] = str(e)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(targets))
    futs = [executor.submit(_one, entry) for entry in targets]
    concurrent.futures.wait(futs, timeout=WEB_SEARCH_ENRICH_TIMEOUT)
    executor.shutdown(wait=False)
    return results


def fetch_page(url, max_chars=24000):
    import ipaddress
    import socket

    from bs4 import BeautifulSoup

    if not url:
        return json.dumps({"url": "", "error": "No URL provided."})
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return json.dumps({"url": url, "error": "Only http/https URLs are supported."})
        host = parsed.hostname or ""
        ip = socket.gethostbyname(host)
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            return json.dumps({"url": url, "error": "Access to private/internal addresses is not allowed."})
    except Exception as e:
        return json.dumps({"url": url, "error": f"Invalid URL: {e}"})

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, timeout=15, allow_redirects=True)
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "").lower()
        if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml", "application/json", "application/xml")):
            return json.dumps({"url": url, "content_type": ctype, "error": "Skipped: page is not readable text content (likely binary/PDF/media)."})
        if not r.encoding:
            r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        title = soup.title.get_text(strip=True) if soup.title else ""
        main = soup.find("main") or soup.find("article") or soup.find("body") or soup
        text = main.get_text(separator="\n", strip=True)
        text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...[truncated]"
        return json.dumps({
            "url": r.url,
            "title": title,
            "content": text or "(No readable text content extracted)",
        }, ensure_ascii=False)
    except Exception as e:
        print(f"[fetch_page] Failed: {e}")
        return json.dumps({"url": url, "error": f"Failed to fetch page: {e}"})


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
            result = M._client_location if M._client_location else "User denied location access"
        M._event_post("tool_ok", task_id, tc_id=tc["id"], result=result, sid=sid, round=round_num, tool_index=tool_index)
        return

    if tool_name == "read_file":
        file_url = args.get("file_url", "")
        filename = os.path.basename(urlparse(file_url).path)
        fpath = os.path.abspath(os.path.join(M.UPLOADS_DIR, filename))
        if fpath.startswith(os.path.abspath(M.UPLOADS_DIR)) and os.path.exists(fpath):
            text = M.read_file_text(fpath)
            if text:
                result = f"Content of {file_url}:\n\n{text}"
            else:
                result = f"Could not extract text from {file_url}. The file may contain only images."
        else:
            result = f"File not found: {file_url}"
        M._event_post("tool_ok", task_id, tc_id=tc["id"], result=result, sid=sid, round=round_num, tool_index=tool_index)
        return

    if tool_name == "read_image":
        url = args.get("url", "")
        fpath = M.resolve_image_path(url)
        if fpath is None:
            result = json.dumps({"ok": False, "error": f"Image not found: {url}"})
        else:
            result = json.dumps({"ok": True, "image_url": url})
        M._event_post("tool_ok", task_id, tc_id=tc["id"], result=result, sid=sid, round=round_num, tool_index=tool_index)
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
            )
        except Exception as e:
            print(f"[web_search] Unhandled exception for task {task_id}: {e}")
            result = json.dumps({"results": [], "query": args.get("query"), "error": str(e)})
        print(f"[web_search] RAW result for task {task_id}: {result[:300]}...")  # DEBUG
        with M._data_lock:
            t = M.tasks.get(task_id)
            if t:
                t.setdefault("_tools_used", []).append(tool_name)
                try:
                    t.setdefault("_search_details", []).append(json.loads(result))
                except Exception:
                    pass
        llm_result = (
            f"Web search results for query '{args.get('query')}'. "
            f"Analyze these search results thoroughly and provide a clear, accurate response based on the findings:\n\n{result}"
        )
        print(f"[web_search] LLM-bound result (with analysis instruction) for task {task_id}: {llm_result[:400]}...")  # DEBUG
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
            result = M.fetch_page(args.get("url", ""))
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
                    t.setdefault("_search_details", []).append({
                        "tool": "fetch_page",
                        "url": res.get("url", args.get("url", "")),
                        "title": res.get("title", ""),
                        "content": res.get("content", ""),
                        "error": res.get("error", ""),
                    })
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

    elif tool_name == "edit_image":
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
            M._enqueue_image_job(task_id, sid, tool_name, args, tc, round_num, tool_index)
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
