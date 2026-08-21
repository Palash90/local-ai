#!/usr/bin/env python3
import json
import os

from server.dotenv import load_dotenv

load_dotenv()

LLAMA_BASE = "http://localhost:8081"
LLAMA_URL = f"{LLAMA_BASE}/v1/chat/completions"

# The CPU-backed llama-server that serves automated self-chat agents
# (editor/moderator/registered agents). It runs CONCURRENTLY with the GPU
# llama-server on its own port so background agent runs never compete with
# interactive UI users for VRAM.
LLAMA_BASE_CPU = "http://localhost:8079"
LLAMA_URL_CPU = f"{LLAMA_BASE_CPU}/v1/chat/completions"

VENV_PYTHON = os.path.expanduser("~/local-ai/ComfyUI/venv/bin/python")
COMFYUI_DIR = os.path.expanduser("~/local-ai/ComfyUI")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8080/")
COMFYUI_URL = "http://localhost:8188"
HOST = os.environ.get("CHAT_HOST", "0.0.0.0")
PORT = 3001

# GoDaddy Dynamic DNS — updates the AAAA record for DDNS_DOMAIN/DDNS_SUBDOMAIN
# with this machine's stable global IPv6 on a timer (see the ConnectionManager
# thread in server/features/monitoring.py). Secret credentials come from the
# environment (e.g. an EnvironmentFile / /etc/environment); leave the keys
# empty to disable the updater entirely.
GODADDY_API_KEY = os.environ.get("GODADDY_API_KEY", "")
GODADDY_API_SECRET = os.environ.get("GODADDY_API_SECRET", "")
DDNS_DOMAIN = os.environ.get("DDNS_DOMAIN", "palashkantikundu.in")
DDNS_SUBDOMAIN = os.environ.get("DDNS_SUBDOMAIN", "home")
DDNS_CHECK_INTERVAL = int(os.environ.get("DDNS_CHECK_INTERVAL", "300"))

# GCP heartbeat — the ConnectionManager thread POSTs this machine's addresses
# to the receiver running on the GCP VM (scripts/gcp_heartbeat_server.py)
# over the WireGuard tunnel every 10s.
HEARTBEAT_URL = os.environ.get("HEARTBEAT_URL", "http://10.66.66.1:9863/heartbeat")

# External origin used to build public share links. Set this to a portless URL
# (e.g. http://192.168.1.10 or https://chat.example.com) when the server is also
# reachable on port 80/443, because WhatsApp and several other messengers stop
# auto-linking a URL at the ":" of a port: a share link like
# "http://192.168.1.10:3001/s/<token>" becomes a dead short URL that ends at the
# colon. Leave empty to keep building links from the browser's own origin.
SHARE_BASE_URL = os.environ.get("SHARE_BASE_URL", "").strip().rstrip("/")
REASONING_BUDGET = 4096
CPU_PARALLEL_SLOTS = 4  # Set to desired number of concurrent CPU agent slots

# ─────────────────────────────────────────────────────────────────────────────
# Unified RBAC / SSO — Authentik is the SINGLE identity provider.
#
# There is no users.json anymore. Browser users authenticate through nginx's
# auth_request → Authentik proxy outpost (the X-Authentik-* claim headers are
# trusted downstream); self-chat agents authenticate via an OAuth2 password
# grant and send the resulting JWT as "Authorization: Bearer <token>", which
# the backends verify against Authentik's JWKS (see server/auth.py).
#
# AUTHENTIK_BASE_URL must NOT have a trailing slash.
# ─────────────────────────────────────────────────────────────────────────────
AUTHENTIK_BASE_URL = os.environ.get("AUTHENTIK_BASE_URL", "https://home.palashkantikundu.in/sso").rstrip("/")
# Interactive browser SSO application (humans via nginx auth_request).
AUTH_CLIENT_ID = os.environ.get("AUTH_CLIENT_ID", "local-ai")
AUTH_CLIENT_SECRET = os.environ.get("AUTH_CLIENT_SECRET", "")
AUTH_SCOPE = os.environ.get("AUTH_SCOPE", "openid profile email groups")
# Machine-agent OIDC client (self-chat). Separate application in Authentik so
# agent credentials never mix with the human SSO client. The client_id must
# equal the Authentik application slug — the token/jwks endpoints are routed
# by slug, not by client_id.
AUTH_AGENTS_CLIENT_ID = os.environ.get("AUTH_AGENTS_CLIENT_ID", "")
AUTH_AGENTS_CLIENT_SECRET = os.environ.get("AUTH_AGENTS_CLIENT_SECRET", "")
AUTH_AGENTS_APP_SLUG = os.environ.get("AUTH_AGENTS_APP_SLUG", AUTH_AGENTS_CLIENT_ID)
# Token endpoint used by the machine-agent password grant. Authentik only
# exposes a generic token endpoint (the client_id in the body selects the
# provider); the per-slug routes exist for authorize/jwks but not token.
AUTH_AGENTS_TOKEN_URL = os.environ.get(
    "AUTH_AGENTS_TOKEN_URL",
    f"{AUTHENTIK_BASE_URL}/application/o/token/",
)
# JWKS endpoint used to verify agent access tokens. Authentik exposes it at
# /application/o/<application-slug>/jwks/.
AUTH_AGENTS_JWKS_URL = os.environ.get(
    "AUTH_AGENTS_JWKS_URL",
    f"{AUTHENTIK_BASE_URL}/application/o/{AUTH_AGENTS_APP_SLUG}/jwks/",
)
AUTH_AGENTS_ISSUER = os.environ.get(
    "AUTH_AGENTS_ISSUER",
    f"{AUTHENTIK_BASE_URL}/application/o/{AUTH_AGENTS_APP_SLUG}/",
)
# Map Authentik group names → the role scale used by the story RBAC
# (free < premium < admin). Users may be in multiple groups; the highest wins.
AUTH_ROLE_GROUPS = {
    "admin": "admin",
    "premium": "premium",
    "free": "free",
}

# Per-user context files are stored at ~/local-ai-files/contexts/<user>.txt
# (the users.json "context_file" field is gone along with users.json).
CONTEXTS_DIR = os.environ.get("CONTEXTS_DIR", os.path.expanduser("~/local-ai-files/contexts"))

# Which llama-server self-chat agents run on: "cpu" (the RAM-backed CPU server
# on 8079, so agents never compete with interactive UI users for VRAM) or "gpu"
# (the interactive GPU server on 8081, sharing the VRAM-backed model). Override
# with the SELF_CHAT_MODE environment variable.
SELF_CHAT_MODE = os.environ.get("SELF_CHAT_MODE", "cpu").strip().lower()
if SELF_CHAT_MODE not in ("cpu", "gpu"):
    SELF_CHAT_MODE = "cpu"

# Test-time flag: flip to True (manually) to keep EVERY request on the fast GPU
# lane and never admit anything — including self-chat agents — to the slow CPU
# lane. During testing it is easier to wait a few seconds for the GPU than to
# endure CPU speed. A real web-UI human request never goes to the CPU lane
# regardless of this flag: that invariant is enforced unconditionally at
# admission and in task_mode().
FORCE_GPU_LANE = True

# Research self-verification ("critic" pass). After a research answer is
# generated, each inline "(Author, Venue, Year) [url]" citation is re-fetched
# and checked by a second LLM call. These bounds are per-citation only — there
# is deliberately NO overall cap on a report's verification budget.
VERIFY_RETRIES = 2          # extra search/fetch attempts per citation
VERIFY_FETCH_CHARS = 6000   # source text shown to the critic LLM per citation
VERIFY_MAX_CITES_PER_URL = 3  # flag a source cited for more distinct claims than this

# Review-only self-chat roles that must NEVER call tools. The editor/moderator
# are the same creative LLM as the story-writing agents, and with the tool list
# enabled (tool_choice "auto") they spontaneously call generate_image/edit_image
# while revising markdown, burning ComfyUI VRAM on unwanted images. Their chat
# requests are sent with an empty tool list and tool_choice "none".
TOOL_FREE_AGENTS = {"editor", "moderator"}

# model.json holds the LLM model filenames (relative to ~/local-ai-files/my-models/)
# per runtime mode: "gpu" for interactive chat UI users, "cpu" for automated
# self-chat agents (editor/moderator/registered agents). Falls back to the legacy
# single-model model.txt when model.json is missing or has no usable entries.
MODEL_CONFIG_FILE = os.path.expanduser("~/local-ai-files/model.json")


def _load_model_ids(model_file, legacy_file):
    """Return the (gpu, cpu) model ids for the given config files.

    ``model_file`` is the JSON config holding per-mode ids; ``legacy_file`` is
    the plain-text single-model file used as a fallback.
    """
    gpu = ""
    cpu = ""
    try:
        with open(model_file, "r", encoding="utf-8") as file:
            data = json.load(file)
        if isinstance(data, dict):
            gpu = str(data.get("gpu") or data.get("default") or "").strip()
            cpu = str(data.get("cpu") or gpu).strip()
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if not gpu:
        try:
            with open(legacy_file, "r") as file:
                gpu = file.read().strip()
        except (FileNotFoundError, OSError):
            pass
        if not cpu:
            cpu = gpu
    return gpu, cpu


MODEL_ID, MODEL_ID_CPU = _load_model_ids(
    MODEL_CONFIG_FILE, os.path.expanduser("~/local-ai-files/model.txt")
)

COMFYUI_OUTPUT = os.path.expanduser("~/local-ai-files/ComfyUI/output")
UPLOADS_DIR = os.path.expanduser("~/local-ai-files/uploads")
LLAMA_SERVER_PATH = os.path.expanduser("~/local-ai/llama.cpp/build/bin/llama-server")
LLAMA_QWEN_NGL = "0"
LLAMA_GEMMA_NGL = "99"
LLAMA_SERVER_ARGS = [
    "--host", "0.0.0.0",
    "--port", "8081",
    "--models-dir", os.path.expanduser("~/local-ai-files/my-models/"),
    "--jinja",

    # GPU / VRAM & Performance
    "-ngl", LLAMA_GEMMA_NGL,
    "-fa", "on",
    "--ctx-size", "24576",       # 24K context for interactive UI chat
    "-ctk", "q8_0",
    "-ctv", "q8_0", # If you really need a very big context on VRAM, can make it q8_0
    "--no-mmproj-offload",

    # Threads & Batching
    "-t", "8",
    "-tb", "8",
    "-ub", "512",
    "--timeout", "3600",

    # Sampling Parameters
    "--temp", "1.0",
    "--top-p", "0.95",
    "--top-k", "64",
    "--min-p", "0.05"
]

# Second set of llama-server arguments used when processing automated
# self-chat messages (editor/moderator/agent runs). These are background,
# non-interactive jobs, so they deliberately run the model on the CPU only —
# slower, but they never compete with interactive users for VRAM. This server
# runs on its own port (8079) CONCURRENTLY with the GPU server on 8081, so the
# two are started and stopped independently (see restart_llama_server).
LLAMA_SERVER_ARGS_CPU = [
    "--host", "0.0.0.0",
    "--port", "8079",
    "--models-dir", os.path.expanduser("~/local-ai-files/my-models/"),
    "--jinja",

    # CPU-only execution — no layers offloaded to the GPU.
    "--n-gpu-layers", "0",
    "-fa", "off",
    "--ctx-size", "65536",
    "-ctk", "q8_0",            # Quantized KV cache keeps RAM usage low
    # Keep the multimodal projector (mmproj) in RAM too. llama-server
    # offloads the mmproj to the GPU by DEFAULT even with --n-gpu-layers 0,
    # which cudaMalloc-OOMs on the 4 GiB card while the GPU server is loaded.
    "--no-mmproj-offload",

    "-t", "6",
    "-tb", "6",

    # Reasoning & Thinking Limits
    "--reasoning-budget", str(REASONING_BUDGET),
    "--reasoning-budget-message", "Reasoning limit reached, summarize final answer.",

    "--temp", "1.0",
    "--top-p", "0.95",
    "--top-k", "64",
    "--min-p", "0.0",
    "--repeat-penalty", "1.0",
    "--device", "none"
]

FILES_DIR = os.path.expanduser("~/local-ai-files")
SESSIONS_DIR = os.path.join(FILES_DIR, "session")
SESSIONS_FILE = os.path.join(SESSIONS_DIR, "sessions.json")
SHARES_FILE = os.path.join(FILES_DIR, "shares.json")
IMG_PATH = os.path.expanduser("~/local-ai-files/ComfyUI/output")
COMFYUI_INPUT = os.path.expanduser("~/local-ai-files/ComfyUI/input")
PROMPT_PATH = os.path.expanduser("~/local-ai-files/sys_prompt.txt")
TASKS_DB = os.path.expanduser("~/local-ai-files/tasks.db")
THEMES_DB = os.path.expanduser("~/local-ai-files/themes.db")
IMAGE_TOKEN_COST = 1200
AUDIO_TOKEN_COST = 800
PER_MESSAGE_OVERHEAD = 4

with open(
    os.path.expanduser("~/local-ai-files/models.json"), "r", encoding="utf-8"
) as file:
    IMAGE_MODELS = json.load(file)

TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for real-time/current information. Use this for weather, news, sports, stock prices, recent events, or any query where up-to-date data matters. Do NOT answer time-sensitive questions from memory — always search. The results contain snippets only; if the snippets are insufficient to answer the question fully, follow up with fetch_page to read the full content of the relevant page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query"},
                        "current_time": {"type": "string", "description": "Current date and time. Pass ONLY for time-sensitive queries (news, events, hours, etc.) where recency matters. Omit for direct-link lookups or general information."},
                        "current_location": {"type": "string", "description": "User's location. Pass ONLY for location-specific results (weather, local news, nearby places, events). If you don't know the user's location, call get_user_location first to obtain it. Do NOT guess or fabricate location."}
                    },
                    "required": ["query"],
                },
            },
        },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch and read the full text content of a web page. Use this AFTER web_search when the search snippets are not enough to answer the question (e.g. you need details, data, or an article's body). Pass the full URL of the page to read. Long pages are returned one chunk at a time; if the result reports total_chunks greater than 1, call fetch_page again with chunk=2, 3, ... to read the rest. PDFs with no extractable text expose page_images rendered from the scanned pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL of the web page to fetch (must start with http:// or https://)."
                    },
                    "chunk": {
                        "type": "integer",
                        "description": "Which chunk of the page to read (1 = first). Omit to read the first chunk."
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate or draw an image. You MUST choose a style model.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed visual description of what to draw/generate.",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Things to avoid in the image",
                    },
                    "model": {
                        "type": "string",
                        "enum": list(IMAGE_MODELS.keys()),
                        "description": "Art style to use. Options: "
                        + ", ".join(
                            [
                                f"'{k}' ({v['description']})"
                                for k, v in IMAGE_MODELS.items()
                            ]
                        ),
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["landscape", "portrait", "square"],
                        "description": "Image framing/aspect ratio. "
                        "landscape = wide scene (default), portrait = tall or "
                        "single-subject close-up, square = balanced illustration.",
                    },
                },
                "required": ["prompt", "model"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": "Generic Img2Img image editor to modify, restyle, recolor, add elements, or transform existing or uploaded images.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Complete description of what the edited image should look like.",
                    },
                    "negative_prompt": {
                        "type": "string",
                        "description": "Elements to exclude from the visual generation.",
                    },
                    "denoise": {
                        "type": "number",
                        "description": "Denoising value (0.1 to 1.0). Use 0.25-0.4 for subtle color/lighting changes, 0.45-0.65 for structural edits and object additions, and 0.7-0.85 for massive re-imaginings.",
                    },
                },
                "required": ["prompt", "denoise"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_location",
            "description": "Request the user's current geographical location. Call this ONLY when you need location for a location-specific query (weather, local news, nearby places, etc.) and you don't already have the user's location. Returns the user's city/area or 'denied' if they refuse.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of an uploaded file (PDF, DOC, DOCX, XLS, XLSX). Call this when the user has attached a file and you need to read its content to answer their question. The file URL is provided in the user message as [FILE: url]. Pass that url as the file_url parameter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_url": {
                        "type": "string",
                        "description": "The file URL from the user message (the /uploads/... path)."
                    }
                },
                "required": ["file_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_image",
            "description": "View/read an image that was attached or generated earlier in the conversation. The image URL appears in the conversation as [IMAGE: url]. Call this when you actually need to see the image content to answer or describe it accurately. Pass that url as the url parameter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The image URL from the conversation, e.g. /uploads/<file>.jpg or /output/<file>.png"
                    }
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_user_context",
            "description": "Store information about the current user that persists across conversations. Saves preferences, personal details, important facts, or anything the user should not need to repeat. This APPENDS to existing context — only add NEW information, do not repeat what was already saved.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The new information to append to the user's context. Keep it concise and focused on what's new.",
                    }
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "manage_tasks",
            "description": "Manage to-do tasks. Can create, update, complete, delete, list, or get task details. Use this when the user wants to track tasks, set reminders, or manage their to-do list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["create", "update", "complete", "delete", "list", "get"],
                        "description": "The operation to perform.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Required for update/complete/delete/get. The task ID.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Required for create. Task title.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Task description or details.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                        "description": "Task priority (default: medium).",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["pending", "in_progress", "completed", "cancelled"],
                        "description": "For update: new status.",
                    },
                    "due_date": {
                        "type": "string",
                        "description": "Due date in ISO format (e.g. 2026-08-15T17:00:00).",
                    },
                    "reminder_at": {
                        "type": "string",
                        "description": "Reminder time in ISO format. The system will notify about this task at the given time.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to link this task to a conversation.",
                    },
                },
                "required": ["operation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "track_theme",
            "description": "Dedicated theme/combination tracker that guarantees creative variety across all generated content. It records every already-used combination of task-detail fields + mood + genre + role + persona, both globally (across ALL users) and per-user scope. Call BEFORE agreeing on a creative idea to see what has already been produced (never repeat it), and call again AFTER locking in an idea to log it. Use this INSTEAD of manage_tasks for theme/idea tracking.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["list", "log", "complete", "check", "stats"],
                        "description": "The operation to perform.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Which user scope the theme belongs to (the user whose content is being generated). For the self-chat window always use 'self-chat' so all agents share one history. Optional for list/stats, required for log/check.",
                    },
                    "global": {
                        "type": "boolean",
                        "description": "list/stats only: include the history across ALL users instead of just scope. Use to keep track of all users at a glance.",
                    },
                    "theme": {
                        "type": "string",
                        "description": "log only: a short 3-6 word slug of the concrete creative theme/premise/idea chosen.",
                    },
                    "genre": {
                        "type": "string",
                        "description": "log/check only: the task genre.",
                    },
                    "mood": {
                        "type": "string",
                        "description": "log/check only: the mood/tone used.",
                    },
                    "role": {
                        "type": "string",
                        "description": "log/check only: the relationship dynamic or role pair used.",
                    },
                    "persona": {
                        "type": "string",
                        "description": "log/check only: the persona(s) used.",
                    },
                    "details": {
                        "type": "object",
                        "description": "log/check only: the resolved task-detail fields as {field: value} pairs.",
                    },
                    "theme_id": {
                        "type": "string",
                        "description": "complete only: the id of the theme record to mark completed.",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "completed"],
                        "description": "log only: initial status (default: active).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "list only: max number of records to return (default 50).",
                    },
                },
                "required": ["operation"],
            },
        },
    },
]

TOOLS_TOKEN_COST = len(json.dumps(TOOLS)) // 4


def build_sys_content():
    with open(PROMPT_PATH, "r") as file:
        sys_content = file.read()
    model_list = "; ".join(f"{k}: {v['description']}" for k, v in IMAGE_MODELS.items())
    sys_content = sys_content.replace("%model_list%", model_list)
    sys_content = sys_content.replace("%_image_keys%", str(list(IMAGE_MODELS.keys())))
    return sys_content
