#!/usr/bin/env python3
"""E2E backend harness: runs the REAL chat engine and REAL HTTP API handlers.

This starts the actual production code paths the browser automation drives:

  * ``chat-webui.py`` is imported as a module (same trick the pytest suite
    uses), so every shared container, helper and the ``M`` proxy are the real
    ones. Its ``__main__`` block — which would demand live llama-servers and
    SearXNG — is never executed.
  * The only thing swapped for the test is the LLM *endpoint*: ``LLAMA_BASE``
    is pointed at a local OpenAI-compatible stub (``llm_stub.py``). The chat
    engine still streams from it over HTTP exactly like it does with
    llama-server. Everything else — sessions, users, tasks DB, context files,
    the queue workers, the ``server.api.Handler`` HTTP server — is real.
  * Sessions/tasks/users/context are redirected to an isolated temp dir so
    running the suite never touches ``~/local-ai-files``.

Usage: ``python3 tests/e2e/backend.py [port]`` (dist/ must already be built)
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from tests.e2e.llm_stub import LlmStubServer  # noqa: E402

E2E_PORT = int(os.environ.get("E2E_PORT", "3099"))
E2E_USER = os.environ.get("E2E_USER", "e2e")
E2E_PASSWORD = os.environ.get("E2E_PASSWORD", "e2e-pass")


def load_module(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def main():
    stub = LlmStubServer().start()
    stub_base = stub.base_url

    chat = load_module("chat_webui_e2e", "chat-webui.py")

    # --- point the engine at the stub LLM (only the endpoint changes) ---
    chat.LLAMA_BASE = stub_base
    chat.LLAMA_URL = f"{stub_base}/v1/chat/completions"
    chat.LLAMA_BASE_CPU = stub_base
    chat.LLAMA_URL_CPU = f"{stub_base}/v1/chat/completions"

    # --- isolate persisted state so the suite never touches ~/local-ai-files ---
    state_dir = tempfile.mkdtemp(prefix="local-ai-e2e-")
    sessions_dir = os.path.join(state_dir, "session")
    os.makedirs(sessions_dir, exist_ok=True)

    chat.USERS_FILE = os.path.join(state_dir, "users.json")
    chat.TASKS_DB = os.path.join(state_dir, "tasks.db")
    chat.SESSIONS_DIR = sessions_dir
    chat.SESSIONS_FILE = os.path.join(sessions_dir, "sessions.json")
    chat.PROMPT_PATH = os.path.join(state_dir, "sys_prompt.txt")
    chat.MODEL_ID = "e2e-model.gguf"
    chat.MODEL_ID_CPU = "e2e-model.gguf"

    with open(chat.PROMPT_PATH, "w") as f:
        f.write("You are the E2E test assistant.")
    with open(chat.USERS_FILE, "w") as f:
        json.dump(
            {
                "users": {
                    E2E_USER: {
                        "password": E2E_PASSWORD,
                        "context_file": os.path.join(state_dir, "contexts", f"{E2E_USER}.txt"),
                    }
                }
            },
            f,
        )

    # Reset caches that may have been populated during import.
    chat._users_cache = None
    chat._users_cache_time = 0
    chat._init_tasks_db()
    chat.load_sessions()

    # --- real background threads that own task processing ---
    threading.Thread(target=chat._event_loop, daemon=True).start()
    threading.Thread(target=chat._queue_worker, args=("gpu",), daemon=True).start()
    threading.Thread(target=chat._queue_worker, args=("cpu",), daemon=True).start()

    # --- real HTTP API server (server.api.Handler serves dist/ too) ---
    import http.server

    from server.api import Handler

    server = http.server.ThreadingHTTPServer(("127.0.0.1", E2E_PORT), Handler)
    print(
        f"[e2e] backend ready on http://127.0.0.1:{E2E_PORT} "
        f"(user={E2E_USER}, llm-stub={stub_base}, state={state_dir})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        stub.stop()


if __name__ == "__main__":
    main()
