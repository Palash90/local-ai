"""Shared application state and the entrypoint proxy.

The chat engine's implementation lives in the :mod:`server.features` package,
but the entrypoint module (``chat-webui.py``) remains the single owner of every
shared value: containers, scalar flags, constants, config values and the
cross-cutting helpers. Feature modules resolve those names at *call time*
through the ``M`` proxy registered here, which simply forwards attribute reads
and writes to the entrypoint module.

This indirection is what keeps per-test monkeypatching working. The test-suite
patches ``chat-webui.<name>`` (functions, containers, scalars, stdlib modules)
and expects every feature module to observe those patches, so no feature module
may bind a shared name at import time.
"""

import queue as _queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from server.config import CPU_PARALLEL_SLOTS


class _Registry:
    """Holds the single entrypoint module reference."""

    entrypoint = None


def register_entrypoint(module):
    """Point the ``M`` proxy at the module that owns the shared state."""
    _Registry.entrypoint = module


class _Proxy:
    """Forward attribute access to the registered entrypoint module."""

    def __getattr__(self, name):
        ep = _Registry.entrypoint
        if ep is None:
            raise AttributeError(
                f"no entrypoint registered yet; import chat-webui.py before using {name}"
            )
        return getattr(ep, name)

    def __setattr__(self, name, value):
        _Registry.entrypoint.__setattr__(name, value)


M = _Proxy()

# ---------------------------------------------------------------------------
# In-memory application state (the same objects chat-webui.py re-exports)
# ---------------------------------------------------------------------------

sessions = {}
sessions_meta = {}
tasks = {}
shares = {}

_active_tokens = {}
_tokens_lock = threading.Lock()
_agent_tokens = set()
_agent_users = set()

# Username → last-activity timestamp for SSO/header-authenticated clients.
# With Authentik fronting the browser there is no per-request token anymore;
# the browser's 2s model-status poll keeps touching this seat as the heartbeat.
_user_last_seen = {}
_user_last_seen_lock = threading.Lock()

# A human user counts as "active" (blocking self-chat agents) while any of
# their requests has been seen within this window. The browser's 2s model-status
# poll acts as the heartbeat.
ACTIVE_WINDOW_SECONDS = 120

_effective_contexts = {}
_effective_contexts_lock = threading.Lock()

_model_transition_lock = threading.Lock()
_data_lock = threading.Lock()

MAX_QUEUE_SIZE = 5

# Tool-loop budget per task. Normal chats stay light (10 LLM rounds ≈ small
# number of tool calls); tasks sent with the UI's "research" toggle get the
# deep recursive budget so the agent can chunk-walk pages and re-search until
# the question is answered.
MAX_TOOL_ROUNDS = {"default": 10, "research": 50}

# Two independent task lanes so interactive UI (GPU) users and self-chat
# agents (CPU) never queue behind each other. Each lane has its own list,
# lock/condition and "currently running" marker. Only image-generation VRAM
# choreography (see _image_queue in images.py) stays globally serialized,
# since ComfyUI only has one physical GPU to render on regardless of which
# lane requested the image.
_task_queues = {"gpu": [], "cpu": [], "mcp": []}
_queue_locks = {
    "gpu": threading.Lock(),
    "cpu": threading.Lock(),
    "mcp": threading.Lock(),
}
_queue_conds = {mode: threading.Condition(lock) for mode, lock in _queue_locks.items()}
_current_task_ids = {"gpu": None, "cpu": None, "mcp": None}

_event_queue = _queue.Queue()
# Serializes image generation/editing so VRAM management (llama unload/free/load)
# and the ``image_active`` model status never race between concurrent chats.
_image_queue = _queue.Queue()
# One LLM-round pool and one tool-call pool PER LANE. These used to be single
# shared pools (_llm_pool max_workers=1, _tool_pool max_workers=2) — even after
# splitting task admission into gpu/cpu lanes, actually *running* a round or a
# tool call still funneled through those single shared pools, so a GPU (UI)
# task and a CPU (agent) task would still block on each other's turn in the
# pool. Splitting per-lane makes them genuinely independent end-to-end.
_llm_pools = {
    "gpu": ThreadPoolExecutor(max_workers=1),
    "cpu": ThreadPoolExecutor(max_workers=CPU_PARALLEL_SLOTS),
    "mcp": ThreadPoolExecutor(max_workers=1),
}
_tool_pools = {
    "gpu": ThreadPoolExecutor(max_workers=2),
    "cpu": ThreadPoolExecutor(max_workers=2),
    "mcp": ThreadPoolExecutor(max_workers=2),
}

_location_events = {}

# Scalars the engine reads and rebinds at runtime.
#
# There are TWO llama-servers running concurrently: the GPU server on 8081
# serves interactive chat UI users, and the CPU server on 8079 serves automated
# self-chat agents. Each keeps its own model status and idle timestamp.
model_status = "unloaded"
_cpu_model_status = "unloaded"
_last_tps = None
_last_llm_use = time.time()
_cpu_last_llm_use = time.time()

_mcp_model_status = "unloaded"
_mcp_last_llm_use = time.time()

# KV-cache slot checkpoints per llama-server lane (see llm.py). Maps mode →
# {"file": str, "model": str, "ts": float, "n_tokens": int} describing the
# last successfully saved /slots/{id}?action=save snapshot, which is restored
# after the model loads again so the conversation KV is not re-prefilled.
_slot_checkpoints = {}
# Per-lane flag set whenever a completion reaches a llama-server (its slot KV
# changed) and cleared once that KV is captured by save/restore. Gates whether
# an unload snapshots the slot again.
_slot_kv_dirty = {"gpu": False, "cpu": False, "mcp": False}
_client_location = None
_overheated = False
_gpu_temp = None
_ram_evacuating = False
_users_cache = None
_users_cache_time = 0

# Context / token budget constants.
# The interactive UI chat runs on the GPU llama-server, which is launched with
# --ctx-size 24576 (24K). Keep this in sync with server/config.py so the UI's
# context meter and the /api/model-status payload reflect the real budget.
MAX_INPUT_TOKENS = 24576
AUTO_COMPACT_THRESHOLD = int(MAX_INPUT_TOKENS * 0.7)

# Monitoring constants.
TEMP_THRESHOLD_ON = 90
TEMP_THRESHOLD_OFF = 75
RAM_EVAC_THRESHOLD = 95
RAM_RESUME_THRESHOLD = 70
