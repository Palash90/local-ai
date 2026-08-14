import builtins
import importlib.util
import io
import json
import os
import queue
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# markdown_hosting.py fails to import unless these are set.
os.environ.setdefault("STORIES_PREMIUM_DIR", "/tmp/opencode/stories_premium")
os.environ.setdefault("STORIES_ADMIN_DIR", "/tmp/opencode/stories_admin")


def _load_module(name, relpath, pre=None, post=None):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    if pre:
        pre()
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    if post:
        post(mod)
    return mod


# ---------------------------------------------------------------------------
# chat-webui.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def chat_webui():
    return _load_module("chat_webui_test", "chat-webui.py")


_CHAT_STATE = [
    "sessions", "sessions_meta", "tasks", "shares", "_active_tokens",
    "_agent_tokens", "_agent_users", "_effective_contexts", "_client_location",
    "_users_cache", "_users_cache_time", "model_status", "_cpu_model_status",
    "_last_tps", "_current_task_ids", "_overheated", "_gpu_temp",
    "_event_post", "_ram_evacuating",
]


def _drain_image_queue(q):
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            return


def _clear_task_queues(chat_webui):
    queues = getattr(chat_webui, "_task_queues", None)
    if isinstance(queues, dict):
        for lane in queues.values():
            lane[:] = []
    elif queues is not None:
        queues[:] = []


@pytest.fixture(autouse=True)
def _reset_chat_state(chat_webui):
    snapshots = {}
    for name in _CHAT_STATE:
        if hasattr(chat_webui, name):
            snapshots[name] = getattr(chat_webui, name)
    try:
        _clear_task_queues(chat_webui)
        _drain_image_queue(chat_webui._image_queue)
        chat_webui.shares.clear()
        yield
    finally:
        for name, value in snapshots.items():
            setattr(chat_webui, name, value)
        _clear_task_queues(chat_webui)
        _drain_image_queue(chat_webui._image_queue)
        chat_webui.shares.clear()


@pytest.fixture
def temp_paths(chat_webui, monkeypatch, tmp_path):
    """Point every file-backed module global at a scratch tmp_path."""
    monkeypatch.setattr(chat_webui, "TASKS_DB", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(chat_webui, "THEMES_DB", str(tmp_path / "themes.db"))
    monkeypatch.setattr(chat_webui, "USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(chat_webui, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(chat_webui, "SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(chat_webui, "SHARES_FILE", str(tmp_path / "shares.json"))
    monkeypatch.setattr(chat_webui, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(chat_webui, "IMG_PATH", str(tmp_path / "output"))
    monkeypatch.setattr(chat_webui, "COMFYUI_OUTPUT", str(tmp_path / "comfy_output"))
    monkeypatch.setattr(chat_webui, "COMFYUI_INPUT", str(tmp_path / "comfy_input"))
    monkeypatch.setattr(chat_webui, "_users_cache", None)
    os.makedirs(chat_webui.UPLOADS_DIR, exist_ok=True)
    os.makedirs(chat_webui.IMG_PATH, exist_ok=True)
    chat_webui._init_tasks_db()
    chat_webui._init_themes_db()
    return tmp_path


# ---------------------------------------------------------------------------
# server/api.py Handler harness
# ---------------------------------------------------------------------------

class FakeHeaders:
    def __init__(self, headers=None):
        self._headers = headers or {}

    def get(self, name, default=None):
        return self._headers.get(name, default)

    def get_all(self, name):
        val = self._headers.get(name)
        return [val] if val is not None else []


class HandlerHarness:
    """Instantiate server.api.Handler without a real socket and capture output."""

    def __init__(self, path, method="GET", body=b"", headers=None, data=None):
        from server.api import Handler

        if data is not None and isinstance(data, (dict, list)):
            body = json.dumps(data).encode()

        self.wfile = io.BytesIO()

        class _H(Handler):
            def send_response(self, code, message=None):
                self.status = code

            def send_header(self, key, value):
                self.sent_headers.append((key, value))

            def end_headers(self):
                pass

            def send_error(self, code, message=None, explain=None):
                self.status = code
                self.wfile.write(f"error:{code}".encode())

            def log_message(self, fmt, *args):
                pass

        all_headers = dict(headers or {})
        if body:
            all_headers.setdefault("Content-Length", str(len(body)))

        h = _H.__new__(_H)
        h.path = path
        h.command = method
        h.headers = FakeHeaders(all_headers)
        h.rfile = io.BytesIO(body)
        h.wfile = self.wfile
        h.request_version = "HTTP/1.0"
        h.status = None
        h.sent_headers = []
        self.handler = h

    @property
    def status(self):
        return self.handler.status

    @property
    def sent_headers(self):
        return self.handler.sent_headers

    def dispatch(self, method=None):
        getattr(self.handler, "do_" + (method or self.handler.command))()
        return self

    @property
    def json(self):
        try:
            return json.loads(self.wfile.getvalue().decode())
        except (ValueError, UnicodeDecodeError):
            return None


@pytest.fixture
def handler(chat_webui):
    """Factory that injects chat-webui state into server.api and dispatches."""
    from server.api import APP_STATE_NAMES, set_app_state

    set_app_state({name: getattr(chat_webui, name) for name in APP_STATE_NAMES})

    def factory(path, method="GET", body=b"", headers=None, data=None):
        hh = HandlerHarness(path, method=method, body=body, headers=headers, data=data)
        hh.dispatch()
        return hh

    return factory


@pytest.fixture
def make_user(temp_paths, chat_webui):
    """Create a users.json with the given users and reset the cache."""

    def _make(users, roles=None, context_files=None):
        data = {"users": {}}
        for name, password in users.items():
            entry = {"password": password}
            if roles and name in roles:
                entry["role"] = roles[name]
            if context_files and name in context_files:
                entry["context_file"] = context_files[name]
            data["users"][name] = entry
        with open(chat_webui.USERS_FILE, "w") as f:
            json.dump(data, f)
        chat_webui._users_cache = None
        chat_webui._users_cache_time = 0
        return data

    return _make


# ---------------------------------------------------------------------------
# self-chat.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def self_chat():
    def _pre():
        os.environ["SELF_CHAT_PASSWORD"] = "test-pass"
        builtins.input = lambda *args, **kwargs: "n"
        sys.argv = ["self-chat.py"]

    return _load_module("self_chat_test", "self-chat.py", pre=_pre)


# ---------------------------------------------------------------------------
# markdown_hosting.py
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def markdown_hosting():
    return _load_module("markdown_hosting_test", "markdown_hosting.py")


@pytest.fixture
def mh(tmp_path, monkeypatch, markdown_hosting):
    free_dir = tmp_path / "free"
    premium_dir = tmp_path / "premium"
    admin_dir = tmp_path / "admin"
    for d in (free_dir, premium_dir, admin_dir):
        d.mkdir(exist_ok=True)
    monkeypatch.setattr(markdown_hosting, "USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setattr(
        markdown_hosting, "COLLECTION_RULES",
        {
            "free_stories": {"path": str(free_dir), "min_level": 0},
            "premium_stories": {"path": str(premium_dir), "min_level": 1},
            "admin_stories": {"path": str(admin_dir), "min_level": 2},
        },
    )
    markdown_hosting._active_tokens.clear()
    markdown_hosting._users_cache = None
    markdown_hosting._users_cache_time = 0
    return markdown_hosting


@pytest.fixture
def mh_client(mh):
    from fastapi.testclient import TestClient

    with TestClient(mh.app) as client:
        yield client


@pytest.fixture
def mh_make_user(mh):
    def _make(users):
        with open(mh.USERS_FILE, "w") as f:
            json.dump({"users": users}, f)
        mh._users_cache = None
        mh._users_cache_time = 0

    return _make
