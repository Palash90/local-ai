import os
import json
import time
import uuid
import shutil
import threading
import markdown
from fastapi import FastAPI, HTTPException, Header, Request, Response, status
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

app = FastAPI()

BASE_STORIES_DIR = "./stories"
USERS_FILE = os.path.expanduser("~/local-ai-files/users.json")

# State matching chat-webui.py
_active_tokens = {}
_tokens_lock = threading.Lock()

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


# --- User store (mirrors chat-webui.py login mechanism) ---

_users_cache = None
_users_cache_time = 0


def load_users():
    """Return {username: {password, context_file, role, ...}} with caching."""
    global _users_cache, _users_cache_time
    now = time.time()
    if _users_cache is not None and now - _users_cache_time < 30:
        return _users_cache
    try:
        with open(USERS_FILE) as f:
            data = json.load(f)
        _users_cache = data.get("users", {})
        _users_cache_time = now
    except (FileNotFoundError, json.JSONDecodeError):
        _users_cache = {}
        _users_cache_time = now
    return _users_cache


def get_user_password(username: str) -> str | None:
    """Fetch password from the shared users file (same as chat-webui.py)."""
    users = load_users()
    u = users.get(username)
    return u.get("password", "") if u else ""


def get_user_context_path(username: str) -> str:
    """Fetch context file path from the shared users file (same as chat-webui.py)."""
    users = load_users()
    u = users.get(username)
    if u and u.get("context_file"):
        return os.path.join(u["context_file"])
    return ""


def get_user_role(username: str) -> str:
    """Resolve role from the shared users file."""
    users = load_users()
    u = users.get(username)
    if u and u.get("role"):
        return u["role"]
    return "premium" if username in {"palash", "totan"} else "free"


# --- Request Schemas ---

class LoginRequest(BaseModel):
    username: str
    password: str


# --- Auth & RBAC Helpers ---

def get_current_user(request: Request) -> str | None:
    """Extracts token from Header or Cookie and checks memory cache."""
    token = request.headers.get("X-Auth-Token") or request.cookies.get("X-Auth-Token") or ""
    if not token:
        return None

    with _tokens_lock:
        return _active_tokens.get(token)


def user_role_level(username: str | None) -> int:
    """Map a username to its required-role hierarchy level (0=guest/free ... 2=admin)."""
    if not username:
        return ROLE_LEVEL["guest"]
    return ROLE_LEVEL.get(get_user_role(username), ROLE_LEVEL["free"])


def enforce_rbac(collection_folder: str, username: str | None):
    """Checks user role against the collection's minimum required level."""
    rule = COLLECTION_RULES.get(collection_folder)
    if not rule:
        raise HTTPException(status_code=404, detail="Collection not found")

    min_level = rule["min_level"]
    if user_role_level(username) < min_level:
        if not username and min_level > 0:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication token missing or invalid.",
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: higher subscription/role required for this story collection.",
        )


# --- Authentication Endpoints ---

@app.post("/api/login")
async def login(credentials: LoginRequest, response: Response):
    username = credentials.username.strip()
    password = credentials.password.strip()

    if get_user_password(username) == password:
        token = str(uuid.uuid4())
        with _tokens_lock:
            _active_tokens[token] = username
            
        # Set cookie for browser navigation alongside API JSON response
        response.set_cookie(key="X-Auth-Token", value=token, httponly=True)
        
        return {
            "token": token,
            "username": username,
            "context_file": get_user_context_path(username),
        }
    
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, 
        detail="Invalid credentials"
    )


@app.post("/api/logout")
async def logout(
    request: Request, 
    response: Response, 
    x_auth_token: str | None = Header(None, alias="X-Auth-Token")
):
    token = x_auth_token or request.cookies.get("X-Auth-Token") or ""
    with _tokens_lock:
        _active_tokens.pop(token, None)
        
    response.delete_cookie(key="X-Auth-Token")
    return {"ok": True}


# --- Dynamic Story Engine & Media Router ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Collections index listing available story collections and their stories."""
    username = get_current_user(request)
    user_level = user_role_level(username)
    cards = []
    for name, rule in COLLECTION_RULES.items():
        if rule["min_level"] > user_level:
            continue
        root = rule["path"]
        if not os.path.isdir(root):
            continue
        story_ids = sorted(
            os.listdir(root),
            key=lambda sid: os.path.getmtime(os.path.join(root, sid)),
            reverse=True,
        )
        if not story_ids:
            continue
        cards.append(
            f"<h2>{name.replace('_', ' ').title()}</h2><ul>"
            + "".join(
                f'<li><a href="/story/{name}/{sid}">{sid}</a></li>' for sid in story_ids
            )
            + "</ul>"
        )
    body = "".join(cards) or "<p>No story collections found yet.</p>"
    if username:
        auth_html = f"""
        <p style="float:right; margin:0;">Logged in as <strong>{username}</strong>
        <button id="logout-btn" style="margin-left:8px; background:none; border:1px solid #888; color:#888; border-radius:6px; padding:2px 10px; cursor:pointer; font-family:sans-serif; font-size:12px;">Log out</button></p>
        """
    else:
        auth_html = f"""
        <p style="float:right; margin:0;">
            <input id="login-user" placeholder="Username" style="padding:4px 8px; border:1px solid #aaa; border-radius:6px; margin-right:4px;">
            <input id="login-pass" type="password" placeholder="Password" style="padding:4px 8px; border:1px solid #aaa; border-radius:6px; margin-right:4px;">
            <button id="login-btn" style="background:#06c; color:#fff; border:none; border-radius:6px; padding:5px 12px; cursor:pointer; font-family:sans-serif;">Log in</button>
            <span id="login-msg" style="color:#c44; font-size:12px;"></span>
        </p>
        """
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Story Collections</title>
        <style>
            body {{ font-family: Georgia, serif; max-width: 750px; margin: 40px auto; padding: 0 20px; line-height: 1.8; background: #fafafa; color: #111; }}
            h1, h2 {{ color: #333; }}
            a {{ color: #06c; text-decoration: none; }}
            @media (prefers-color-scheme: dark) {{
                body {{ background: #16181d; color: #e6e6e6; }}
                h1, h2 {{ color: #f0f0f0; }}
                a {{ color: #7ab8ff; }}
            }}
        </style>
    </head>
    <body>
        {auth_html}
        <h1>Story Collections</h1>
        {body}
        <script>
            async function doLogin() {{
                const user = document.getElementById('login-user').value.trim();
                const pass = document.getElementById('login-pass').value;
                const msg = document.getElementById('login-msg');
                try {{
                    const r = await fetch('/api/login', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ username: user, password: pass }}),
                    }});
                    if (r.ok) {{
                        window.location.reload();
                    }} else {{
                        const d = await r.json();
                        msg.textContent = d.detail || 'Invalid credentials';
                    }}
                }} catch (e) {{
                    msg.textContent = e.message;
                }}
            }}
            const loginBtn = document.getElementById('login-btn');
            if (loginBtn) {{
                loginBtn.addEventListener('click', doLogin);
                document.getElementById('login-pass').addEventListener('keydown', e => {{
                    if (e.key === 'Enter') doLogin();
                }});
            }}
            const logoutBtn = document.getElementById('logout-btn');
            if (logoutBtn) {{
                logoutBtn.addEventListener('click', async () => {{
                    await fetch('/api/logout', {{ method: 'POST' }});
                    window.location.reload();
                }});
            }}
        </script>
    </body>
    </html>
    """


@app.get("/media/{collection}/{story_id}/{filename}")
async def serve_story_image(
    collection: str, 
    story_id: str, 
    filename: str, 
    request: Request
):
    """Serves media dynamically while checking active token session."""
    username = get_current_user(request)
    enforce_rbac(collection, username)
    
    root = COLLECTION_RULES[collection]["path"]
    file_path = os.path.join(root, story_id, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Image not found")
        
    return FileResponse(file_path)


def render_story_html(collection: str, story_id: str, content: str) -> str:
    """Render story markdown and rewrite image srcs to the authenticated media route."""
    html_content = markdown.markdown(content, extensions=['extra', 'tables', 'fenced_code'])
    return html_content.replace('src="', f'src="/media/{collection}/{story_id}/')


@app.get("/story/{collection}/{story_id}/content")
async def story_content(
    collection: str, 
    story_id: str, 
    request: Request
):
    """Returns the current rendered story HTML for live polling."""
    username = get_current_user(request)
    enforce_rbac(collection, username)

    folder_path = os.path.join(COLLECTION_RULES[collection]["path"], story_id)
    if not os.path.exists(folder_path):
        raise HTTPException(status_code=404, detail="Story folder not found")

    md_files = [f for f in os.listdir(folder_path) if f.endswith('.md')]
    if not md_files:
        raise HTTPException(status_code=404, detail="No markdown file found in story directory")

    with open(os.path.join(folder_path, md_files[0]), 'r', encoding='utf-8') as f:
        content = f.read()

    return {"html": render_story_html(collection, story_id, content)}


@app.delete("/story/{collection}/{story_id}")
async def delete_story(
    collection: str, 
    story_id: str, 
    request: Request
):
    """Deletes the story folder (markdown + images) with RBAC enforcement."""
    username = get_current_user(request)
    enforce_rbac(collection, username)

    folder_path = os.path.join(COLLECTION_RULES[collection]["path"], story_id)
    if not os.path.exists(folder_path):
        raise HTTPException(status_code=404, detail="Story folder not found")

    shutil.rmtree(folder_path, ignore_errors=True)
    if os.path.exists(folder_path):
        raise HTTPException(status_code=500, detail="Failed to delete story folder")
    return {"ok": True, "deleted": story_id}


@app.get("/story/{collection}/{story_id}", response_class=HTMLResponse)
async def read_story(
    collection: str, 
    story_id: str, 
    request: Request
):
    """Reads story Markdown dynamically and enforces access controls."""
    username = get_current_user(request)
    enforce_rbac(collection, username)
    
    folder_path = os.path.join(COLLECTION_RULES[collection]["path"], story_id)
    if not os.path.exists(folder_path):
        raise HTTPException(status_code=404, detail="Story folder not found")
        
    md_files = [f for f in os.listdir(folder_path) if f.endswith('.md')]
    if not md_files:
        raise HTTPException(status_code=404, detail="No markdown file found in story directory")
        
    with open(os.path.join(folder_path, md_files[0]), 'r', encoding='utf-8') as f:
        content = f.read()

    html_content = render_story_html(collection, story_id, content)

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{story_id.replace('-', ' ').title()}</title>
        <style>
            body {{ font-family: Georgia, serif; max-width: 750px; margin: 40px auto; padding: 0 20px; line-height: 1.8; background: #fafafa; color: #111; }}
            img {{ max-width: 100%; height: auto; border-radius: 8px; margin: 20px 0; display: block; }}
            a.back {{ display: inline-block; margin-bottom: 20px; color: #666; text-decoration: none; font-family: sans-serif; }}
            blockquote {{ border-left: 4px solid #ddd; margin: 0; padding-left: 16px; color: #555; }}
            code {{ background: #f0f0f0; padding: 2px 6px; border-radius: 4px; font-family: monospace; }}
            pre {{ background: #f0f0f0; padding: 12px; border-radius: 6px; overflow-x: auto; }}
            pre code {{ background: none; padding: 0; }}
            table {{ border-collapse: collapse; }}
            th, td {{ border: 1px solid #ccc; padding: 6px 10px; }}
            @media (prefers-color-scheme: dark) {{
                body {{ background: #16181d; color: #e6e6e6; }}
                a.back {{ color: #999; }}
                blockquote {{ border-left-color: #444; color: #aaa; }}
                code {{ background: #2a2e37; }}
                pre {{ background: #2a2e37; }}
                th, td {{ border-color: #3a3f4a; }}
            }}
        </style>
    </head>
    <body>
        <a href="/" class="back">← Back to Collections</a>
        <button id="delete-btn" style="float:right; background:none; border:1px solid #c44; color:#c44; border-radius:6px; padding:4px 12px; cursor:pointer; font-family:sans-serif; font-size:13px;">Delete story</button>
        <article id="story-article">{html_content}</article>
        <script>
            const article = document.getElementById('story-article');
            let lastHtml = article.innerHTML;
            async function poll() {{
                try {{
                    const r = await fetch('/story/{collection}/{story_id}/content');
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
                    }}
                }} catch (e) {{}}
                setTimeout(poll, 3000);
            }}
            poll();

            document.getElementById('delete-btn').addEventListener('click', async () => {{
                if (!confirm('Delete this story and all its images?')) return;
                try {{
                    const r = await fetch('/story/{collection}/{story_id}', {{ method: 'DELETE' }});
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
        </script>
    </body>
    </html>
    """