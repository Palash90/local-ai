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
    DDNS_CHECK_INTERVAL,
    DDNS_DOMAIN,
    DDNS_SUBDOMAIN,
    FILES_DIR,
    FORCE_GPU_LANE,
    GODADDY_API_KEY,
    GODADDY_API_SECRET,
    HOST,
    IMG_PATH,
    IMAGE_MODELS,
    IMAGE_TOKEN_COST,
    LLAMA_BASE,
    LLAMA_BASE_CPU,
    LLAMA_GEMMA_NGL,
    LLAMA_QWEN_NGL,
    LLAMA_SERVER_ARGS,
    LLAMA_SERVER_ARGS_CPU,
    LLAMA_SERVER_PATH,
    LLAMA_URL,
    LLAMA_URL_CPU,
    MODEL_ID,
    MODEL_ID_CPU,
    PER_MESSAGE_OVERHEAD,
    PORT,
    PROMPT_PATH,
    REASONING_BUDGET,
    SEARXNG_URL,
    SELF_CHAT_MODE,
    SESSIONS_DIR,
    SESSIONS_FILE,
    SHARE_BASE_URL,
    SHARES_FILE,
    TASKS_DB,
    THEMES_DB,
    TOOL_FREE_AGENTS,
    TOOLS,
    TOOLS_TOKEN_COST,
    UPLOADS_DIR,
    USERS_FILE,
    VENV_PYTHON,
    VERIFY_FETCH_CHARS,
    VERIFY_MAX_CITES_PER_URL,
    VERIFY_RETRIES,
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
    MAX_TOOL_ROUNDS,
    RAM_EVAC_THRESHOLD,
    RAM_RESUME_THRESHOLD,
    TEMP_THRESHOLD_OFF,
    TEMP_THRESHOLD_ON,
    _active_tokens,
    _agent_tokens,
    _agent_users,
    _client_location,
    _cpu_last_llm_use,
    _cpu_model_status,
    _current_task_ids,
    _data_lock,
    _effective_contexts,
    _effective_contexts_lock,
    _event_queue,
    _gpu_temp,
    _image_queue,
    _last_llm_use,
    _last_tps,
    _llm_pools,
    _location_events,
    _model_transition_lock,
    _overheated,
    _queue_conds,
    _queue_locks,
    _ram_evacuating,
    _task_queues,
    _tokens_lock,
    _tool_pools,
    _users_cache,
    _users_cache_time,
    model_status,
    register_entrypoint,
    sessions,
    sessions_meta,
    shares,
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

from server.features.themes_db import (  # noqa: E402
    _init_themes_db,
    handle_theme_tool,
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

from server.features.shares import (  # noqa: E402
    create_share,
    get_share,
    list_shares,
    load_shares,
    revoke_share,
    save_shares,
)

from server.features.context import (  # noqa: E402
    _image_to_data_url,
    _latest_read_image_url,
    _reference_historical_images,
    _summarize_with_llm,
    _text_tokens,
    compact_messages_copy,
    context_token_report,
    effective_token_estimate,
    estimate_tokens,
    prepare_context_for_llm,
    resolve_image_path,
    strip_html,
    trim_messages_for_context,
)

from server.features.llm import (  # noqa: E402
    _consult_worker,
    _inject_read_image,
    _llm_worker,
    _start_llm_round,
    active_model_id,
    consult_expert_model,
    is_llama_alive,
    load_llama_model,
    server_base,
    server_last_use,
    server_model_id,
    server_status,
    server_url,
    task_mode,
    unload_llama_model,
)
from server.features.tools import (  # noqa: E402
    _dispatch_tool,
    _tool_worker,
    fetch_page,
    web_search,
)

from server.features.images import (  # noqa: E402
    _enqueue_image_job,
    _image_url_rel,
    _image_worker,
    _input_dir,
    _output_dir,
    _output_rel,
    edit_image,
    free_comfyui_vram,
    generate_image,
)

from server.features.monitoring import (  # noqa: E402
    _cpu_lane_needed,
    _ensure_llama_server_for_task,
    _evacuate_ram,
    _idle_unload_loop,
    _reminder_loop,
    _thermal_monitor,
    _connection_manager,
    ensure_comfyui_running,
    ensure_llama_server,
    get_gpu_temp,
    get_ram_usage,
    kill_comfyui,
    kill_llama_server,
    model_status_snapshot,
    restart_llama_server,
    restart_servers,
)

from server.features.orchestration import (  # noqa: E402
    _delete_task_image,
    _event_loop,
    _event_post,
    _finalize_task,
    _human_priority_active,
    _queue_worker,
    _set_task_error,
    _task_max_rounds,
    _task_user,
    location_str,
    set_client_location,
    set_status,
)

from server.features.critic import (  # noqa: E402
    extract_citations,
    run_verification,
    run_verification_worker,
)

# Point the M proxy at this module: from here on, feature modules resolve all
# shared state, config values and cross-cutting helpers through it.
register_entrypoint(sys.modules[__name__])

_init_tasks_db()
_init_themes_db()

SYS_CONTENT = build_sys_content()

print("Prompt:\n", "*" * 80, "\n", SYS_CONTENT, "\n", "*" * 80)

set_app_state({name: globals()[name] for name in APP_STATE_NAMES})


if __name__ == "__main__":
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    load_sessions()
    load_shares()
    try:
        r = requests.get(f"{LLAMA_BASE}/health", timeout=3)
        if r.status_code != 200:
            raise Exception("health check failed")
        print("[startup] GPU llama-server is running")
    except Exception:
        print("[startup] GPU llama-server not reachable — starting...")
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
    # One queue worker per lane: GPU (interactive UI users) and CPU (self-chat
    # agents) now run fully independently, so an agent task can never make a
    # UI user wait behind it.
    threading.Thread(target=_queue_worker, args=("gpu",), daemon=True).start()
    threading.Thread(target=_queue_worker, args=("cpu",), daemon=True).start()
    threading.Thread(target=_image_worker, daemon=True).start()
    threading.Thread(target=_idle_unload_loop, daemon=True).start()
    threading.Thread(target=_thermal_monitor, daemon=True).start()
    threading.Thread(target=_reminder_loop, daemon=True).start()
    threading.Thread(target=_connection_manager, daemon=True).start()
    print(f"Chat UI running on http://localhost:{PORT}")
    s = http.server.HTTPServer((HOST, PORT), Handler)
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        s.shutdown()