#!/usr/bin/env python3
"""Chat Web UI — script entrypoint.

The chat engine implementation now lives in the :mod:`server.features` package.
This file remains the single script entrypoint and the owner of every shared
value: it re-exports the feature modules' functions and state, registers itself
with the ``M`` proxy (see :mod:`server.features.state`), and wires up the
background threads.

Feature code resolves shared state, config values and cross-cutting helpers at
call time through the ``M`` proxy, so monkeypatching ``chat-webui.<name>`` —
which the test-suite relies on — keeps working across module boundaries.
"""
import http.server, json, os, re, glob, uuid, base64, requests, subprocess, time, random, threading, sys, io, tempfile, queue as _queue_mod  # noqa: F401

sys.stdout.reconfigure(line_buffering=True)  # noqa
from datetime import datetime  # noqa: F401
from urllib.parse import urlparse, parse_qs  # noqa: F401

from server.read_file import read_file_text

from server.config import (  # noqa: F401
    AUDIO_TOKEN_COST,
    COMFYUI_DIR,
    COMFYUI_INPUT,
    COMFYUI_OUTPUT,
    COMFYUI_URL,
    FILES_DIR,
    HOST,
    IMG_PATH,
    IMAGE_MODELS,
    IMAGE_TOKEN_COST,
    LLAMA_BASE,
    LLAMA_GEMMA_NGL,
    LLAMA_QWEN_NGL,
    LLAMA_SERVER_ARGS,
    LLAMA_SERVER_PATH,
    LLAMA_URL,
    MODEL_ID,
    PER_MESSAGE_OVERHEAD,
    PORT,
    PROMPT_PATH,
    REASONING_BUDGET,
    SEARXNG_URL,
    SESSIONS_DIR,
    SESSIONS_FILE,
    TASKS_DB,
    TOOLS,
    TOOLS_TOKEN_COST,
    UPLOADS_DIR,
    USERS_FILE,
    VENV_PYTHON,
    build_sys_content,
)

from server.api import APP_STATE_NAMES, Handler, set_app_state

import sys

sys.path.insert(0, COMFYUI_DIR)

# ---------------------------------------------------------------------------
# Feature modules — the chat engine implementation.
# ---------------------------------------------------------------------------

from server.features.state import (  # noqa: E402
    ACTIVE_WINDOW_SECONDS,
    AUTO_COMPACT_THRESHOLD,
    MAX_INPUT_TOKENS,
    MAX_QUEUE_SIZE,
    RAM_EVAC_THRESHOLD,
    RAM_RESUME_THRESHOLD,
    TEMP_THRESHOLD_OFF,
    TEMP_THRESHOLD_ON,
    _active_tokens,
    _agent_tokens,
    _agent_users,
    _client_location,
    _current_task_id,
    _data_lock,
    _effective_contexts,
    _effective_contexts_lock,
    _event_queue,
    _gpu_temp,
    _last_llm_use,
    _last_tps,
    _llm_pool,
    _location_events,
    _model_transition_lock,
    _overheated,
    _queue_cond,
    _queue_lock,
    _ram_evacuating,
    _task_queue,
    _tokens_lock,
    _tool_pool,
    _users_cache,
    _users_cache_time,
    model_status,
    register_entrypoint,
    sessions,
    sessions_meta,
    tasks,
)

from server.features.tasks_db import (  # noqa: E402
    _db_fetch,
    _db_fetch_one,
    _db_run,
    _init_tasks_db,
    handle_task_tool,
    task_complete,
    task_create,
    task_delete,
    task_get,
    task_list,
    task_update,
)

from server.features.users import (  # noqa: E402
    _safe_username,
    get_current_user,
    get_user_context_path,
    get_user_password,
    load_users,
    read_user_context,
    write_user_context,
)

from server.features.sessions import (  # noqa: E402
    _load_extra_prompts,
    _prepare_session,
    _session_file,
    _session_meta_from,
    load_sessions,
    save_sessions,
)

from server.features.context import (  # noqa: E402
    _summarize_with_llm,
    _text_tokens,
    compact_messages_copy,
    context_token_report,
    effective_token_estimate,
    estimate_tokens,
    prepare_context_for_llm,
    strip_html,
    trim_messages_for_context,
)

from server.features.llm import (  # noqa: E402
    _llm_worker,
    _start_llm_round,
    is_llama_alive,
    load_llama_model,
    unload_llama_model,
)

from server.features.tools import (  # noqa: E402
    _dispatch_tool,
    _tool_worker,
    fetch_page,
    web_search,
)

from server.features.images import (  # noqa: E402
    _image_url_rel,
    _input_dir,
    _output_dir,
    _output_rel,
    edit_image,
    free_comfyui_vram,
    generate_image,
)

from server.features.monitoring import (  # noqa: E402
    _evacuate_ram,
    _idle_unload_loop,
    _reminder_loop,
    _thermal_monitor,
    ensure_comfyui_running,
    get_gpu_temp,
    get_ram_usage,
    kill_comfyui,
    kill_llama_server,
    model_status_snapshot,
    restart_servers,
)

from server.features.orchestration import (  # noqa: E402
    _delete_task_image,
    _event_loop,
    _event_post,
    _finalize_task,
    _queue_worker,
    _set_task_error,
    _task_user,
    location_str,
    set_client_location,
    set_status,
)

# Point the M proxy at this module: from here on, feature modules resolve all
# shared state, config values and cross-cutting helpers through it.
register_entrypoint(sys.modules[__name__])

_init_tasks_db()

SYS_CONTENT = build_sys_content()

print("Prompt:\n", "*" * 80, "\n", SYS_CONTENT, "\n", "*" * 80)

set_app_state({name: globals()[name] for name in APP_STATE_NAMES})


if __name__ == "__main__":
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    load_sessions()
    try:
        r = requests.get(f"{LLAMA_BASE}/health", timeout=3)
        if r.status_code != 200:
            raise Exception("health check failed")
        print("[startup] llama-server is running")
    except Exception:
        print("[startup] llama-server not reachable — starting...")
        restart_servers()
    try:
        r = requests.get(SEARXNG_URL, timeout=3)
        if r.status_code in (200, 301, 302):
            print("[startup] SearXNG is running")
        else:
            raise Exception(f"status {r.status_code}")
    except Exception as e:
        print(f"[startup] ERROR: SearXNG is not reachable at {SEARXNG_URL} ({e}). Web search will not work. Exiting.")
        sys.exit(1)
    threading.Thread(target=_event_loop, daemon=True).start()
    threading.Thread(target=_queue_worker, daemon=True).start()
    threading.Thread(target=_idle_unload_loop, daemon=True).start()
    threading.Thread(target=_thermal_monitor, daemon=True).start()
    threading.Thread(target=_reminder_loop, daemon=True).start()
    print(f"Chat UI running on http://localhost:{PORT}")
    s = http.server.HTTPServer((HOST, PORT), Handler)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        s.shutdown()
