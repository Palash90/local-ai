"""Feature modules for the chat web UI.

The chat engine used to be a single ``chat-webui.py`` file. The implementation
now lives in this package, split by feature area:

* :mod:`~server.features.state` — shared state, the ``M`` entrypoint proxy
* :mod:`~server.features.tasks_db` — SQLite-backed to-do tasks
* :mod:`~server.features.users` — login, user context, usernames
* :mod:`~server.features.sessions` — conversation persistence
* :mod:`~server.features.context` — token estimation / context compaction
* :mod:`~server.features.llm` — llama-server lifecycle and streaming
* :mod:`~server.features.tools` — LLM tool implementations
* :mod:`~server.features.images` — ComfyUI image generation / editing
* :mod:`~server.features.monitoring` — health loops, restart, thermal/RAM
* :mod:`~server.features.orchestration` — event loop and task queue

``chat-webui.py`` remains the single entrypoint: it owns every shared value and
registers itself as the entrypoint module. Feature code resolves shared state,
config values and cross-cutting helpers at call time through the ``M`` proxy
(see :mod:`server.features.state`), so monkeypatching ``chat-webui.<name>`` —
as the test-suite does — keeps working across module boundaries.
"""

from server.features import (
    context,
    images,
    llm,
    monitoring,
    orchestration,
    sessions,
    state,
    tasks_db,
    tools,
    users,
)

__all__ = [
    "context",
    "images",
    "llm",
    "monitoring",
    "orchestration",
    "sessions",
    "state",
    "tasks_db",
    "tools",
    "users",
]
