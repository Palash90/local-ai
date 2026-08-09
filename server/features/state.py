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

_active_tokens = {}
_tokens_lock = threading.Lock()
_agent_tokens = set()
_agent_users = set()

# A human user counts as "active" (blocking self-chat agents) while any of
# their tokens has been seen within this window. The browser's 2s model-status
# poll carries the auth token, acting as a heartbeat.
ACTIVE_WINDOW_SECONDS = 120

_effective_contexts = {}
_effective_contexts_lock = threading.Lock()

_model_transition_lock = threading.Lock()
_data_lock = threading.Lock()

MAX_QUEUE_SIZE = 5
_task_queue = []
_queue_lock = threading.Lock()
_queue_cond = threading.Condition(_queue_lock)
_current_task_id = None

_event_queue = _queue.Queue()
_llm_pool = ThreadPoolExecutor(max_workers=1)
_tool_pool = ThreadPoolExecutor(max_workers=2)

_location_events = {}

# Scalars the engine reads and rebinds at runtime.
model_status = "unloaded"
_last_tps = None
_last_llm_use = time.time()
_client_location = None
_overheated = False
_gpu_temp = None
_ram_evacuating = False
_users_cache = None
_users_cache_time = 0

# Context / token budget constants.
MAX_INPUT_TOKENS = 32768
AUTO_COMPACT_THRESHOLD = int(MAX_INPUT_TOKENS * 0.7)

# Monitoring constants.
TEMP_THRESHOLD_ON = 85
TEMP_THRESHOLD_OFF = 65
RAM_EVAC_THRESHOLD = 95
RAM_RESUME_THRESHOLD = 70
