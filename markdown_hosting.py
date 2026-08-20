import os
import json
import html
import shutil
import markdown
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse, FileResponse
from urllib.parse import quote

from server.dotenv import load_dotenv

load_dotenv()

app = FastAPI()

BASE_STORIES_DIR = "./stories"

# Directory roots resolved from environment variables.
# Hierarchy: everyone -> free dir, premium +1 dir, admin +1 more dir.
# PREMIUM and ADMIN dirs are REQUIRED — markdown hosting fails fast if missing.
_FREE_DIR = os.getenv("STORIES_FREE_DIR", os.path.expanduser("~/local-ai-files/stories"))
_PREMIUM_DIR = os.getenv("STORIES_PREMIUM_DIR", "")
_ADMIN_DIR = os.getenv("STORIES_ADMIN_DIR", "")

for _var, _val in (("STORIES_PREMIUM_DIR", _PREMIUM_DIR), ("STORIES_ADMIN_DIR", _ADMIN_DIR)):
    if not _val:
        raise RuntimeError(
            f"markdown_hosting.py cannot start: required environment variable "
            f"{_var} is not set."
        )

# Each collection requires a minimum role level to access.
ROLE_LEVEL = {
    "guest": 0,
    "free": 0,
    "user": 0,
    "premium": 1,
    "admin": 2,
}

COLLECTION_RULES = {
    "free_stories": {"path": _FREE_DIR, "min_level": 0},     # everyone
    "premium_stories": {"path": _PREMIUM_DIR, "min_level": 1},  # free + premium
    "admin_stories": {"path": _ADMIN_DIR, "min_level": 2},    # free + premium + admin
}


# --- Auth & RBAC Helpers (shared Authentik-backed identity) ---

def get_current_user(request: Request) -> str | None:
    """Username authenticated through nginx's auth_request (X-Authentik-*).

    Direct localhost callers may present ``Authorization: Bearer <jwt>``; that
    is verified against Authentik's JWKS (see server/auth.py). Returns None
    when no identity is present.
    """
    from server.auth import get_current_user as _get_current_user
    return _get_current_user(request.headers)


def get_current_role(request: Request) -> str:
    """Role (free/premium/admin) derived from Authentik group membership."""
    from server.auth import get_identity as _get_identity
    identity = _get_identity(request.headers)
    return identity["role"] if identity else "free"


def user_role_level(username: str | None, role: str | None = None) -> int:
    """Map a username (or resolved role) to its hierarchy level (0=guest ... 2=admin)."""
    if role is None:
        if not username:
            return ROLE_LEVEL["guest"]
        return ROLE_LEVEL.get(get_user_role(username), ROLE_LEVEL["free"])
    return ROLE_LEVEL.get(role, ROLE_LEVEL["free"])


def get_user_role(username: str) -> str:
    """Resolve role from the shared identity provider's group membership.

    Kept for backward compatibility; the chat server derives role from
    X-Authentik-Groups via server/auth.py.
    """
    from server.auth import role_from_groups
    return role_from_groups(_stories_role_groups_for(username))


def _stories_role_groups_for(username: str) -> list:
    """Best-effort group mapping for a username seen without claim headers.

    Legacy fallback so direct localhost requests (no nginx auth_request) still
    get a sensible role. Once nginx fronts the service this path is never hit.
    """
    if username in {"palash"}:
        return ["admin"]
    if username in {"totan"}:
        return ["premium"]
    return ["free"]


def enforce_rbac(collection_folder: str, request: Request | None = None, username: str | None = None):
    """Checks the user's role against the collection's minimum required level."""
    rule = COLLECTION_RULES.get(collection_folder)
    if not rule:
        raise HTTPException(status_code=404, detail="Collection not found")

    if request is not None:
        level = user_role_level(None, get_current_role(request))
        authed = bool(get_current_user(request))
    else:
        authed = bool(username)
        level = user_role_level(username)

    min_level = rule["min_level"]
    if level < min_level:
        if not authed and min_level > 0:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required.",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: higher subscription/role required for this story collection.",
        )


def error_page(status_code: int, detail: str) -> str:
    """Render a styled HTML error page for browser navigation.

    API and live-polling endpoints keep their JSON error responses (see
    ``_http_exception_html_handler``); anything a user would navigate to in the
    browser gets a proper page instead of raw JSON. Unauthenticated browser
    traffic never reaches here anonymously in production — nginx runs
    ``auth_request`` against the Authentik proxy outpost, so a 401 means the
    Authentik session simply needs to be established.
    """
    titles = {
        401: "Authentication Required",
        403: "Access Denied",
        404: "Not Found",
    }
    title = titles.get(status_code, f"Error {status_code}")
    if status_code == 401:
        message = "Please log in to view this story collection."
    else:
        message = html.escape(str(detail))
    login_block = """
    <span class="login-toggle"><a href="/sso/outpost.goauthentik.io/start?rd=%2Fstories%2F">Sign in with SSO</a></span>
    """
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{title}</title>
        <style>
            * {{ box-sizing: border-box; }}
            html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
            body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 18px; max-width: 40em; margin: 0 auto; padding: 16px; line-height: 1.7; background: #fafafa; color: #111; }}
            h1 {{ color: #c44; }}
            a.back {{ color: #666; text-decoration: none; }}
            .topbar {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; font-family: sans-serif; font-size: 14px; }}
            .topbar .login {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
            .topbar .login.hidden {{ display: none; }}
            .topbar .login-toggle a {{ color: #06c; font-weight: bold; }}
            .topbar input {{ padding: 6px 8px; border: 1px solid #aaa; border-radius: 6px; font-size: 14px; background: #fff; color: #111; }}
            .topbar button {{ background: none; border: 1px solid #888; color: #888; border-radius: 6px; padding: 5px 12px; cursor: pointer; font-family: sans-serif; font-size: 13px; }}
            .topbar button:hover {{ background: #eee; }}
            .topbar button.primary {{ background: #06c; color: #fff; border-color: #06c; }}
            .topbar button.primary:hover {{ background: #0577e6; }}
            .topbar #login-msg {{ color: #c44; font-size: 12px; width: 100%; }}
            @media (max-width: 600px) {{
                body {{ padding: 12px; font-size: 19px; }}
                .topbar {{ flex-direction: column; align-items: stretch; }}
                .topbar .login {{ flex-direction: column; align-items: stretch; }}
                .topbar input {{ width: 100%; }}
            }}
            @media (prefers-color-scheme: dark) {{
                body {{ background: #16181d; color: #e6e6e6; }}
                h1 {{ color: #ff8080; }}
                a.back {{ color: #999; }}
                .topbar .login-toggle a {{ color: #7ab8ff; }}
                .topbar input {{ background: #1f232b; border-color: #3a3f4a; color: #e6e6e6; }}
                .topbar input::placeholder {{ color: #888; }}
                .topbar button {{ background: #1f232b; border-color: #555; color: #cfcfcf; }}
                .topbar button:hover {{ background: #262b34; }}
                .topbar button.primary {{ background: #3a7ee0; border-color: #3a7ee0; color: #fff; }}
                .topbar button.primary:hover {{ background: #4a8cec; }}
                .topbar #login-msg {{ color: #ff8080; }}
            }}
        </style>
    </head>
    <body>
        <nav class="topbar">
            <a href="/" class="back">← Back to Collections</a>
            {login_block}
        </nav>
        <h1>{title}</h1>
        <p>{message}</p>
    </body>
    </html>
    """


@app.exception_handler(HTTPException)
async def _http_exception_html_handler(request: Request, exc: HTTPException):
    """JSON errors for API/live-poll endpoints, HTML pages for browser routes."""
    path = request.url.path
    if path.startswith("/api/") or path.endswith("/content"):
        return await http_exception_handler(request, exc)
    return HTMLResponse(
        status_code=exc.status_code,
        content=error_page(exc.status_code, exc.detail),
    )


# --- Authentication Endpoints ---

# Browser authentication is handled entirely by nginx's auth_request against
# the Authentik proxy outpost (/sso/...). The endpoints that previously issued
# self-managed X-Auth-Token cookies no longer exist: identity comes from the
# X-Authentik-* claim headers nginx forwards upstream.


# --- Dynamic Story Engine & Media Router ---

def pick_story_md(folder_path):
    """Prefer the editor's revised file (story_rN_ts.edited.md) over the original."""
    mds = [f for f in os.listdir(folder_path) if f.endswith(".md")]
    if not mds:
        return None
    edited = [f for f in mds if f.endswith(".edited.md")]
    return os.path.join(folder_path, (edited or mds)[0])


def story_moderation(folder_path):
    """Return the moderator verdict dict for a story, or None."""
    for f in os.listdir(folder_path):
        if f.endswith(".moderation.json"):
            try:
                with open(os.path.join(folder_path, f), encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, json.JSONDecodeError):
                return None
    return None


def moderation_badge(mod):
    """HTML snippet showing the GREEN/RED verdict, or empty string.

    For RED verdicts, includes the moderator's reason as a tap/hover tooltip
    (see .mod-badge CSS/JS shared by both pages).
    """
    if not mod:
        return ""
    v = mod.get("verdict", "")
    color = "#2a7" if v == "GREEN" else ("#c44" if v == "RED" else "#888")
    if v == "RED":
        reason = html.escape(mod.get("reasons") or mod.get("reason") or "No reason provided.")
        return (
            f' <span class="mod-badge" tabindex="0" data-reason="{reason}" '
            f'style="color:{color}; font-size:11px; font-family:sans-serif; '
            f'cursor:pointer; border-bottom:1px dotted {color};">({v})</span>'
        )
    return (
        f' <span style="color:{color}; font-size:11px; font-family:sans-serif;">'
        f"({v})</span>"
    )


def list_collection_stories(root: str):
    """Return [(genre_label | None, story_id)] for a collection root.

    Legacy flat story folders (md directly inside root) are reported with
    genre None; genre folders contain story subdirectories.
    """
    entries = sorted(
        os.listdir(root),
        key=lambda e: os.path.getmtime(os.path.join(root, e)),
        reverse=True,
    )
    items = []
    for entry in entries:
        full = os.path.join(root, entry)
        if not os.path.isdir(full):
            continue
        if any(f.endswith(".md") for f in os.listdir(full)):
            items.append((None, entry))
            continue
        for sub in sorted(os.listdir(full), reverse=True):
            subfull = os.path.join(full, sub)
            if os.path.isdir(subfull) and any(
                f.endswith(".md") for f in os.listdir(subfull)
            ):
                items.append((entry, os.path.join(entry, sub)))
    return items


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Collections index listing available story collections and their stories."""
    username = get_current_user(request)
    role = get_current_role(request)
    user_level = user_role_level(username, role)
    cards = []
    for name, rule in COLLECTION_RULES.items():
        if rule["min_level"] > user_level:
            continue
        root = rule["path"]
        if not os.path.isdir(root):
            continue
        items = list_collection_stories(root)
        if not items:
            continue
        grouped = {}
        for genre, sid in items:
            grouped.setdefault(genre, []).append(sid)
        sections = []
        for genre, sids in grouped.items():
            heading = f"<h3>{genre.replace('_', ' ').title()}</h3>" if genre else ""
            lis = []
            for sid in sids:
                badge = moderation_badge(story_moderation(os.path.join(root, sid)))
                
                # Safely quote path segments for URLs and escape HTML text display
                encoded_sid = "/".join(quote(part) for part in sid.split("/"))
                display_name = html.escape(sid.split("/")[-1])
                
                lis.append(
                    f'<li><a href="/story/{name}/{encoded_sid}">{display_name}</a>{badge}</li>'
                )
            sections.append(heading + "<ul>" + "".join(lis) + "</ul>")
        cards.append(
            f"<h2>{name.replace('_', ' ').title()}</h2>" + "".join(sections)
        )
    body = "".join(cards) or "<p>No story collections found yet.</p>"
    if username:
        auth_html = f"""
        <span class="logged">Logged in as <strong>{username}</strong> ({role})</span>
        <button id="logout-btn">Log out</button>
        """
    else:
        auth_html = """
        <span class="login-toggle"><a href="/sso/outpost.goauthentik.io/start?rd=%2Fstories%2F">Sign in with SSO</a></span>
        """
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Story Collections</title>
        <style>
            * {{ box-sizing: border-box; }}
            html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
            body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 18px; max-width: 40em; margin: 0 auto; padding: 16px; line-height: 1.7; background: #fafafa; color: #111; }}
            h1, h2, h3 {{ color: #333; line-height: 1.3; }}
            ul {{ margin: 0 0 1.2em; padding-left: 1.4em; }}
            li {{ margin-bottom: 0.6em; }}
            a {{ color: #06c; text-decoration: none; }}
            .topbar {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 20px; font-family: sans-serif; font-size: 14px; }}
            .topbar .logged {{ margin: 0; color: #555; }}
            .topbar .login {{ display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }}
            .topbar .login.hidden {{ display: none; }}
            .topbar .login-toggle a {{ color: #06c; font-weight: bold; }}
            .topbar input {{ padding: 6px 8px; border: 1px solid #aaa; border-radius: 6px; font-size: 14px; background: #fff; color: #111; }}
            .topbar button {{ background: none; border: 1px solid #888; color: #888; border-radius: 6px; padding: 5px 12px; cursor: pointer; font-family: sans-serif; font-size: 13px; }}
            .topbar button:hover {{ background: #eee; }}
            .topbar button.primary {{ background: #06c; color: #fff; border-color: #06c; }}
            .topbar button.primary:hover {{ background: #0577e6; }}
            .topbar #login-msg {{ color: #c44; font-size: 12px; width: 100%; }}
            .mod-badge {{ position: relative; }}
            .mod-badge:hover::after, .mod-badge.show-tip::after {{
                content: attr(data-reason);
                position: absolute; left: 0; top: 100%; margin-top: 4px;
                background: #222; color: #fff; padding: 6px 10px; border-radius: 6px;
                font-size: 12px; line-height: 1.4; white-space: normal;
                width: max-content; max-width: 240px; z-index: 10;
            }}
            @media (max-width: 600px) {{
                body {{ padding: 12px; font-size: 19px; }}
                .topbar {{ flex-direction: column; align-items: stretch; }}
                .topbar .login {{ flex-direction: column; align-items: stretch; }}
                .topbar input {{ width: 100%; }}
            }}
            @media (prefers-color-scheme: dark) {{
                body {{ background: #16181d; color: #e6e6e6; }}
                h1, h2, h3 {{ color: #f0f0f0; }}
                a {{ color: #7ab8ff; }}
                .topbar .logged {{ color: #aaa; }}
                .topbar .login-toggle a {{ color: #7ab8ff; }}
                .topbar input {{ background: #1f232b; border-color: #3a3f4a; color: #e6e6e6; }}
                .topbar input::placeholder {{ color: #888; }}
                .topbar button {{ background: #1f232b; border-color: #555; color: #cfcfcf; }}
                .topbar button:hover {{ background: #262b34; }}
                .topbar button.primary {{ background: #3a7ee0; border-color: #3a7ee0; color: #fff; }}
                .topbar button.primary:hover {{ background: #4a8cec; }}
                .topbar #login-msg {{ color: #ff8080; }}
            }}
        </style>
    </head>
    <body>
        <nav class="topbar">{auth_html}</nav>
        <h1>Story Collections</h1>
        {body}
        <script>
            const logoutBtn = document.getElementById('logout-btn');
            if (logoutBtn) {{
                logoutBtn.addEventListener('click', () => {{
                    window.location.href = '/sso/outpost.goauthentik.io/end?rd=%2Fstories%2F';
                }});
            }}
            // Tap-to-toggle tooltip for moderation badges (title= doesn't work on mobile touch).
            document.querySelectorAll('.mod-badge').forEach(el => {{
                el.addEventListener('click', e => {{
                    e.stopPropagation();
                    document.querySelectorAll('.mod-badge.show-tip').forEach(o => {{
                        if (o !== el) o.classList.remove('show-tip');
                    }});
                    el.classList.toggle('show-tip');
                }});
            }});
            document.addEventListener('click', () => {{
                document.querySelectorAll('.mod-badge.show-tip').forEach(o => o.classList.remove('show-tip'));
            }});
        </script>
    </body>
    </html>
    """
@app.get("/media/{collection}/{story_id:path}/{filename}")
async def serve_story_image(
    collection: str, 
    story_id: str, 
    filename: str, 
    request: Request
):
    """Serves media dynamically while checking the authenticated session."""
    enforce_rbac(collection, request=request)
    
    root = COLLECTION_RULES[collection]["path"]
    file_path = os.path.join(root, story_id, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Image not found")
        
    return FileResponse(file_path)


def render_story_html(collection: str, story_id: str, content: str) -> str:
    """Render story markdown and rewrite image srcs to the authenticated media route.

    pymdownx.arithmatex (generic mode) leaves $...$ / $$...$$ math untouched
    but wraps it in <span class="arithmatex"> / <div class="arithmatex">
    so the KaTeX auto-render script loaded on the story page can find and
    typeset it client-side.
    """
    html_content = markdown.markdown(
        content,
        extensions=['extra', 'tables', 'fenced_code', 'pymdownx.arithmatex'],
        extension_configs={'pymdownx.arithmatex': {'generic': True}},
    )
    html_content = html_content.replace(
        '<table>', '<div class="table-wrap"><table>'
    ).replace('</table>', '</table></div>')
    return html_content.replace('src="', f'src="/media/{collection}/{story_id}/')


@app.get("/story/{collection}/{story_id:path}/content")
async def story_content(
    collection: str, 
    story_id: str, 
    request: Request
):
    """Returns the current rendered story HTML for live polling."""
    enforce_rbac(collection, request=request)

    folder_path = os.path.join(COLLECTION_RULES[collection]["path"], story_id)
    if not os.path.exists(folder_path):
        raise HTTPException(status_code=404, detail="Story folder not found")

    md_file = pick_story_md(folder_path)
    if not md_file:
        raise HTTPException(status_code=404, detail="No markdown file found in story directory")

    with open(md_file, 'r', encoding='utf-8') as f:
        content = f.read()

    return {"html": render_story_html(collection, story_id, content)}


@app.delete("/story/{collection}/{story_id:path}")
async def delete_story(
    collection: str, 
    story_id: str, 
    request: Request
):
    """Deletes the story folder (markdown + images). Admin role required."""
    enforce_rbac(collection, request=request)

    if get_current_role(request) != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required to delete stories.",
        )

    folder_path = os.path.join(COLLECTION_RULES[collection]["path"], story_id)
    if not os.path.exists(folder_path):
        raise HTTPException(status_code=404, detail="Story folder not found")

    shutil.rmtree(folder_path, ignore_errors=True)
    if os.path.exists(folder_path):
        raise HTTPException(status_code=500, detail="Failed to delete story folder")
    return {"ok": True, "deleted": story_id}


@app.get("/story/{collection}/{story_id:path}", response_class=HTMLResponse)
async def read_story(
    collection: str, 
    story_id: str, 
    request: Request
):
    """Reads story Markdown dynamically and enforces access controls."""
    enforce_rbac(collection, request=request)
    
    folder_path = os.path.join(COLLECTION_RULES[collection]["path"], story_id)
    if not os.path.exists(folder_path):
        raise HTTPException(status_code=404, detail="Story folder not found")
        
    md_file = pick_story_md(folder_path)
    if not md_file:
        raise HTTPException(status_code=404, detail="No markdown file found in story directory")
        
    with open(md_file, 'r', encoding='utf-8') as f:
        content = f.read()

    html_content = render_story_html(collection, story_id, content)
    is_admin = get_current_role(request) == "admin"

    verdict_html = ""
    mod = story_moderation(folder_path)
    if mod:
        v = mod.get("verdict", "")
        color = "#2a7" if v == "GREEN" else ("#c44" if v == "RED" else "#888")
        if v == "RED":
            reason = html.escape(mod.get("reasons") or mod.get("reason") or "No reason provided.")
            verdict_html = (
                f'<div class="mod-badge" tabindex="0" data-reason="{reason}" '
                f'style="font-family:sans-serif; color:{color}; font-size:13px; '
                f'margin-bottom:12px; display:inline-block; cursor:pointer; '
                f'border-bottom:1px dotted {color};">Moderation: {v} (tap for reason)</div>'
            )
        else:
            verdict_html = (
                f'<div style="font-family:sans-serif; color:{color}; font-size:13px; '
                f'margin-bottom:12px;">Moderation: {v}</div>'
            )

    delete_button_html = '<button id="delete-btn">Delete story</button>' if is_admin else ""

    # Escaped parameters for safe JavaScript injection and HTTP URL generation
    story_id_js = json.dumps(story_id)
    encoded_collection = quote(collection)
    encoded_story_path = "/".join(quote(part) for part in story_id.split("/"))

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{html.escape(story_id.split('/')[-1].replace('-', ' ').title())}</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css">
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js"></script>
        <style>
            * {{ box-sizing: border-box; }}
            html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
            body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 18px; max-width: 40em; margin: 0 auto; padding: 16px; line-height: 1.7; background: #fafafa; color: #111; }}
            article {{ overflow-wrap: break-word; }}
            article p, article li {{ font-size: 1em; }}
            img {{ max-width: 100%; height: auto; border-radius: 8px; margin: 20px 0; display: block; }}
            .topbar {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 16px; font-family: sans-serif; font-size: 14px; }}
            a.back {{ color: #666; text-decoration: none; }}
            .topbar button {{ background: none; border: 1px solid #c44; color: #c44; border-radius: 6px; padding: 4px 12px; cursor: pointer; font-family: sans-serif; font-size: 14px; }}
            .topbar button:hover {{ background: #fceaea; }}
            blockquote {{ border-left: 4px solid #ddd; margin: 0 0 1em; padding: 0 0 0 16px; color: #555; }}
            code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-family: monospace; font-size: 0.85em; }}
            pre {{ background: #f0f0f0; padding: 12px; border-radius: 6px; overflow-x: auto; -webkit-overflow-scrolling: touch; }}
            pre code {{ background: none; padding: 0; font-size: 0.85em; }}
            .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 1em 0; }}
            table {{ border-collapse: collapse; font-size: 0.9em; }}
            th, td {{ border: 1px solid #ccc; padding: 6px 10px; }}
            .mod-badge {{ position: relative; }}
            .mod-badge:hover::after, .mod-badge.show-tip::after {{
                content: attr(data-reason);
                position: absolute; left: 0; top: 100%; margin-top: 4px;
                background: #222; color: #fff; padding: 6px 10px; border-radius: 6px;
                font-size: 12px; line-height: 1.4; white-space: normal;
                width: max-content; max-width: 280px; z-index: 10;
            }}
            @media (max-width: 600px) {{
                body {{ padding: 12px; font-size: 19px; }}
                .topbar {{ flex-direction: column; align-items: stretch; }}
                .topbar button {{ width: 100%; }}
            }}
            @media (prefers-color-scheme: dark) {{
                body {{ background: #16181d; color: #e6e6e6; }}
                a.back {{ color: #999; }}
                .topbar button {{ border-color: #e05a5a; color: #ff7a7a; }}
                .topbar button:hover {{ background: #2a1c1c; }}
                blockquote {{ border-left-color: #444; color: #aaa; }}
                code {{ background: #2a2e37; }}
                pre {{ background: #2a2e37; }}
                th, td {{ border-color: #3a3f4a; }}
            }}
        </style>
    </head>
    <body>
        <nav class="topbar">
            <a href="/" class="back">← Back to Collections</a>
            {delete_button_html}
        </nav>
        {verdict_html}
        <article id="story-article">{html_content}</article>
        <script>
            const storyId = {story_id_js};
            const article = document.getElementById('story-article');
            let lastHtml = article.innerHTML;

            function typesetMath() {{
                if (typeof renderMathInElement !== 'function') {{
                    setTimeout(typesetMath, 100);
                    return;
                }}
                renderMathInElement(article, {{
                    delimiters: [
                        {{left: '\\\\[', right: '\\\\]', display: true}},
                        {{left: '\\\\(', right: '\\\\)', display: false}},
                        {{left: '$$', right: '$$', display: true}},
                        {{left: '$', right: '$', display: false}}
                    ],
                    throwOnError: false
                }});
            }}
            typesetMath();

            async function poll() {{
                try {{
                    const r = await fetch('/story/{encoded_collection}/{encoded_story_path}/content');
                    if (!r.ok) return;
                    const data = await r.json();
                    const newHtml = data.html;
                    if (newHtml !== lastHtml) {{
                        if (newHtml.startsWith(lastHtml)) {{
                            const nearBottom = window.innerHeight + window.scrollY > document.body.scrollHeight - 150;
                            article.insertAdjacentHTML('beforeend', newHtml.slice(lastHtml.length));
                            if (nearBottom) window.scrollTo(0, document.body.scrollHeight);
                        }} else {{
                            article.innerHTML = newHtml;
                        }}
                        lastHtml = newHtml;
                        typesetMath();
                    }}
                }} catch (e) {{}}
                setTimeout(poll, 3000);
            }}
            poll();

            const deleteBtn = document.getElementById('delete-btn');
            if (deleteBtn) {{
                deleteBtn.addEventListener('click', async () => {{
                    if (!confirm(`Delete story "${{storyId}}" and all its images?`)) return;
                    try {{
                        const r = await fetch('/story/{encoded_collection}/{encoded_story_path}', {{ method: 'DELETE' }});
                        if (r.ok) {{
                            window.location.href = '/';
                        }} else {{
                            const d = await r.json();
                            alert('Delete failed: ' + (d.detail || r.status));
                        }}
                    }} catch (e) {{
                        alert('Delete failed: ' + e.message);
                    }}
                }});
            }}

            // Tap-to-toggle tooltip for the moderation reason (title= doesn't work on mobile touch).
            document.querySelectorAll('.mod-badge').forEach(el => {{
                el.addEventListener('click', e => {{
                    e.stopPropagation();
                    el.classList.toggle('show-tip');
                }});
            }});
            document.addEventListener('click', () => {{
                document.querySelectorAll('.mod-badge.show-tip').forEach(o => o.classList.remove('show-tip'));
            }});
        </script>
    </body>
    </html>
    """