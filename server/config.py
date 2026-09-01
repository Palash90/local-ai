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
SEARXNG_PUBLIC_URL = os.environ.get(
    "SEARXNG_PUBLIC_URL", "https://home.palashkantikundu.in/search"
)
COMFYUI_URL = "http://localhost:8188"
HOST = os.environ.get("CHAT_HOST", "127.0.0.1")
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

# OpenAI-compatible API key for /v1/* endpoints.  Set via the OPENAI_API_KEY
# environment variable (e.g. in .env).  Leave empty to disable the endpoints.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

REASONING_BUDGET = 2048
MAX_OUTPUT_TOKENS = 8192

# ─────────────────────────────────────────────────────────────────────────────
# Sampling router (two-call split).
#
# Round 0 of every task fires a tiny greedy classifier call (bounded by
# SAMPLING_ROUTER_MAX_TOKENS) that labels the user's message intent; the label
# maps to a sampling profile injected into all LLM rounds of that task via
# per-request temperature/top_k/top_p (supported by llama-server,
# tools/server/server-chat.cpp). Any router failure falls back to empty
# overrides (= server defaults), never blocks generation.
# ─────────────────────────────────────────────────────────────────────────────
SAMPLING_ROUTER_MAX_TOKENS = 12
SAMPLING_ROUTER_TIMEOUT = 90  # covers a cold model load on either lane
SAMPLING_ROUTER_PROMPT = (
    "Classify the user's latest message by what kind of response it needs.\n"
    "- creative: stories, poems, lyrics, fiction, worldbuilding, roleplay\n"
    "- code: programming, debugging, math, data analysis, exact technical "
    "output\n"
    "- factual: facts, explanations, research, news, how-to questions\n"
    "- chat: casual conversation, opinions, everything else\n"
    "Answer with EXACTLY ONE word: creative, code, factual, or chat."
)
SAMPLING_BUCKETS = {
    "creative": {"temperature": 1.0, "top_k": 80, "top_p": 0.97},
    "code": {"temperature": 0.3, "top_k": 40, "top_p": 0.9},
    "factual": {"temperature": 0.5, "top_k": 40, "top_p": 0.9},
    "chat": {"temperature": 1.0, "top_k": 64, "top_p": 0.95},
}
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

# Test-time flag: when True, every request is admitted to the fast GPU lane
# and nothing — including self-chat agents — reaches the slow CPU lane.
# Defaults to False; enable temporarily via FORCE_GPU_LANE=true in .env for
# testing. A real web-UI human request never goes to the CPU lane
# regardless of this flag: that invariant is enforced unconditionally at
# admission and in task_mode().
FORCE_GPU_LANE = os.environ.get("FORCE_GPU_LANE", "false").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Research self-verification ("critic" pass). After a research answer is
# generated, each inline "(Author, Venue, Year) [url]" citation is re-fetched
# and checked by a second LLM call. These bounds are per-citation only — there
# is deliberately NO overall cap on a report's verification budget.
VERIFY_RETRIES = 2          # extra search/fetch attempts per citation
VERIFY_FETCH_CHARS = 6000   # source text shown to the critic LLM per citation
VERIFY_MAX_CITES_PER_URL = 3  # flag a source cited for more distinct claims than this
# LLM judge re-scheduling of research answers. After the critic pass, the judge
# scores the finished answer (verdict + QUALITY: NN/100). Below-quality, missing
# citation, or requirement-mismatch answers are re-scheduled (regenerated via the
# generation model with a steering message) up to these bounds; UNSAFE answers
# that still fail after retries are declined instead of delivered.
VERIFY_QUALITY_GATE = 70     # answers scoring below this (0-100) get re-scheduled
VERIFY_MAX_RETRIES = 2       # max judge/quality re-runs before decline/deliver

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

# ─────────────────────────────────────────────────────────────────────────────
# KV-cache slot checkpoints (--slot-save-path).
#
# The GPU llama-server is unloaded from VRAM for every ComfyUI render (and both
# servers idle-unload after 300s), which throws away the KV cache of the whole
# conversation prefix. With --slot-save-path the server exposes
# POST /slots/{id}?action=save|restore, so llm.py snapshots the KV to this
# directory right before an unload and restores it after the model loads again
# — the next completion then only evaluates NEW tokens instead of re-prefilling
# the entire context.
#
# The directory MUST exist before llama-server starts: its arg parser rejects
# a missing --slot-save-path directory at startup, hence the makedirs here.
# ─────────────────────────────────────────────────────────────────────────────
LLAMA_SLOT_SAVE_DIR = os.environ.get(
    "LLAMA_SLOT_SAVE_DIR", os.path.expanduser("~/local-ai-files/kv-slots")
)
os.makedirs(LLAMA_SLOT_SAVE_DIR, exist_ok=True)

LLAMA_QWEN_NGL = "0"
LLAMA_GEMMA_NGL = "99"

# ─────────────────────────────────────────────────────────────────────────────
# MCP verification server — a lightweight CPU-only llama-server dedicated to
# LEVEL 2 (input) and LEVEL 3 (output) LLM verification for the MCP gateway.
# Runs gemma4-e4b-qat (small, fast QAT model) with a tight 8K context.
# Started by the MCP gateway on first batch and auto-unloaded after
# VERIFY_IDLE_TIMEOUT seconds of inactivity to free RAM.
# ─────────────────────────────────────────────────────────────────────────────
VERIFY_PORT = int(os.environ.get("VERIFY_PORT", "8083"))
VERIFY_MODEL = os.environ.get("VERIFY_MODEL", "gemma-4-E2B-it-Q4_K_M")
VERIFY_CONTEXT_SIZE = int(os.environ.get("VERIFY_CONTEXT_SIZE", "8192"))
VERIFY_IDLE_TIMEOUT = int(os.environ.get("VERIFY_IDLE_TIMEOUT", "300"))
LLAMA_SERVER_ARGS = [
    "--host", "127.0.0.1",
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

    # Prompt-cache reuse: allow slots to reuse/shift cached prefix segments
    # across multi-turn chats and tool rounds instead of re-prefilling.
    "--cache-reuse", "256",

    # KV-cache checkpointing: enables POST /slots/{id}?action=save|restore so
    # the conversation KV survives model unload/reload cycles (image gen).
    # The router passes this down to each loaded model instance.
    "--slot-save-path", LLAMA_SLOT_SAVE_DIR,

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
    "--host", "127.0.0.1",
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

    # Prompt-cache reuse (see LLAMA_SERVER_ARGS): agent turns share long
    # prefixes (system prompt + tools), so shifted reuse saves CPU prefill.
    "--cache-reuse", "256",

    # Reasoning & Thinking Limits
    "--reasoning-budget", str(REASONING_BUDGET),
    "--reasoning-budget-message", "Reasoning limit reached, summarize final answer.",

    "--temp", "1.0",
    "--top-p", "0.95",
    "--top-k", "64",
    "--min-p", "0.0",
    "--repeat-penalty", "1.0",
    "--device", "none",

    # KV-cache checkpointing (see LLAMA_SERVER_ARGS): the CPU lane also
    # idle-unloads, and re-prefilling an agent story context on CPU is slow.
    "--slot-save-path", LLAMA_SLOT_SAVE_DIR,
]


LLAMA_BASE_GUARDRAIL = "http://localhost:8083"
LLAMA_URL_GUARDRAIL = f"{LLAMA_BASE_GUARDRAIL}/v1/chat/completions"
MODEL_ID_GUARDRAIL = "gemma-4-E2B-it-Q4_K_M"
MCP_USER = os.environ.get("MCP_USER", "")

# Background agent peer review: who critiques whom for the full cross-agent
# round on the CPU lane. Keys are agent usernames; a reply finalized by the
# key user is reviewed by the value user before finalization.
AGENT_PEER_MAP = json.loads(
    os.environ.get("AGENT_PEER_MAP", '{"kaya": "kolpo", "kolpo": "kaya"}')
)

# Agent usernames known to the system regardless of the in-memory token
# registry. Lane routing must never depend on the registry alone: a
# chat-webui restart wipes it, and unregistered agents then silently fall
# back to the GPU lane (smaller ctx → exceed_context_size_error).
KNOWN_AGENT_USERS = set(
    u.strip()
    for u in os.environ.get(
        "AGENT_USERNAMES", "kolpo,kaya,editor,moderator"
    ).split(",")
    if u.strip()
)

LLAMA_SERVER_ARGS_GUARDRAIL = [
    "--host", "127.0.0.1",
    "--port", "8083",
    "--models-dir", os.path.expanduser("~/local-ai-files/my-models/"),
    "--jinja",
    "--n-gpu-layers", "0",
    "-fa", "off",
    "--ctx-size", "16384",
    # Two slots so concurrent judge calls (L3 output + answer-quality often
    # fire together) stop serializing behind a single slot. ctx is split
    # across slots (8K each) — judge prompts are small, so this is plenty.
    "--parallel", "2",
    "-ctk", "q8_0",
    "--no-mmproj-offload",
    "-t", "4",
    "-tb", "4",
    "--cache-reuse", "256",
    "--reasoning-budget", str(REASONING_BUDGET),
    "--reasoning-budget-message", "Reasoning limit reached, summarize final answer.",
    "--temp", "1.0",
    "--top-p", "0.95",
    "--top-k", "64",
    "--min-p", "0.0",
    "--repeat-penalty", "1.0",
    "--device", "none",
    "--slot-save-path", LLAMA_SLOT_SAVE_DIR,
]

# Backward compatibility aliases
LLAMA_BASE_MCP = LLAMA_BASE_GUARDRAIL
LLAMA_URL_MCP = LLAMA_URL_GUARDRAIL
MODEL_ID_MCP = MODEL_ID_GUARDRAIL
LLAMA_SERVER_ARGS_MCP = LLAMA_SERVER_ARGS_GUARDRAIL

# Dedicated embedding llama-server (port 8084). Serves the nomic 137M embedding
# model through llama.cpp's /embedding endpoint; the vector layer of the page
# cache (server/features/page_cache.py) posts to it so persisted pages/searches
# can be recalled semantically without Ollama or the chat CPU model.
LLAMA_BASE_EMBED = "http://localhost:8084"
MODEL_ID_EMBED = "nomic-embed-text-v1.5.Q8_0"
LLAMA_SERVER_ARGS_EMBED = [
    "--host", "127.0.0.1",
    "--port", "8084",
    "--models-dir", os.path.expanduser("~/local-ai-files/my-models/"),
    # Restrict this process to embedding only (no chat/completion) and use
    # mean pooling + L2 normalisation, which is how nomic embeddings modelcard
    # expects retrieval vectors to be produced.
    "--embedding",
    "--pooling", "mean",
    "--embd-normalize", "2",
    "--n-gpu-layers", "0",
    "-fa", "off",
    # Embedding prompts are tiny (title+text prefix); a short ctx keeps RAM and
    # prefill modest. 8K supports a few thousand-token documents comfortably.
    "--ctx-size", "8192",
    # Use spare CPU without starving the concurrent chat CPU model on 8079.
    "-t", "6",
    "-tb", "6",
    "--device", "none",
]

FILES_DIR = os.path.expanduser("~/local-ai-files")
SESSIONS_DIR = os.path.join(FILES_DIR, "session")
SESSIONS_FILE = os.path.join(SESSIONS_DIR, "sessions.json")
SHARES_FILE = os.path.join(FILES_DIR, "shares.json")
IMG_PATH = os.path.expanduser("~/local-ai-files/ComfyUI/output")
COMFYUI_INPUT = os.path.expanduser("~/local-ai-files/ComfyUI/input")
PROMPT_PATH = os.path.expanduser("~/local-ai-files/sys_prompt.txt")
# Unified SQLite database: tasks, theme_log and MCP batches all live in this
# one file (see server/db.py). Override at runtime with LOCAL_AI_DB.
APP_DB = os.path.expanduser("~/local-ai-files/local_ai.db")
# Persistent page/search cache (see server/features/page_cache.py). Lives with
# the app DB, outside the repo; override at runtime with LOCAL_AI_PAGE_CACHE.
PAGE_CACHE_DB = os.path.expanduser("~/local-ai-files/page_cache.db")
IMAGE_TOKEN_COST = 1200
AUDIO_TOKEN_COST = 800
PER_MESSAGE_OVERHEAD = 4

with open(
    os.path.expanduser("~/local-ai-files/models.json"), "r", encoding="utf-8"
) as file:
    IMAGE_MODELS = json.load(file)

# ─────────────────────────────────────────────────────────────────────────────
# Tool definitions — two-tier.
#
# TOOLS_DETAILED is the source of truth: full descriptions and per-field
# guidance for every callable tool. TOOLS is the slim wire format actually sent
# with every chat request: name + one-line description + bare parameter schemas
# (types/enums/required only). This cuts the static prompt overhead by ~60% on
# every (re)prefill. The model can recover the full docs at runtime via the
# `tool_details` meta-tool, which is dispatched in server/features/tools.py.
# ─────────────────────────────────────────────────────────────────────────────
TOOLS_DETAILED = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web for real-time/current information. Use this for weather, news, sports, stock prices, recent events, or any query where up-to-date data matters. Do NOT answer time-sensitive questions from memory — always search. The results contain snippets only; if the snippets are insufficient to answer the question fully, follow up with fetch_page to read the full content of the relevant page. NEVER for questions about the user's own code, projects, or codebase — those must use the codebase-search tools.",
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
            "description": "Read the text content of an uploaded file (PDF, DOC, DOCX, XLS, XLSX). Image-only PDFs are OCR'd when Tesseract is available. Call this when the user has attached a file and you need to read its content to answer their question. The file URL is provided in the user message as [FILE: url]. Pass that url as the file_url parameter.",
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

# Hot-path tools whose FULL definitions always go out on the wire — the model
# must never need a `tool_details` round-trip before using these.
_FULL_DOCS_TOOLS = {"web_search", "fetch_page"}

_TOOL_SHORT_DESC = {
    "web_search": (
        "Search the web for real-time/current information (news, weather, "
        "prices, events). Returns snippets only. Never for the user's own "
        "code/codebase — use codebase-search tools for those."
    ),
    "fetch_page": (
        "Read the full text of a web page by URL; long pages are chunked, "
        "re-call with chunk=2, 3, ... for the rest."
    ),
    "generate_image": (
        "Generate/draw an image from a prompt. A style model MUST be chosen."
    ),
    "edit_image": (
        "Img2img editor: restyle/modify an existing or uploaded image via "
        "prompt + denoise strength."
    ),
    "get_user_location": (
        "Ask the user's browser for their current city/area (may be denied)."
    ),
    "read_file": (
        "Extract text from an uploaded file (PDF/DOC/DOCX/XLS/XLSX) via its "
        "[FILE: url] path; OCR is used for scanned PDFs when available."
    ),
    "read_image": (
        "View an image attached or generated earlier in the conversation via "
        "its [IMAGE: url] path."
    ),
    "update_user_context": (
        "Append lasting facts/preferences about the user; persists across "
        "conversations. New info only."
    ),
    "manage_tasks": (
        "Create/update/complete/delete/list/get the user's to-do tasks and "
        "reminders."
    ),
    "track_theme": (
        "Log/check creative theme+genre+mood+role combos to guarantee "
        "variety (never repeat one)."
    ),
}


def _slim_tools(detailed):
    """Build the wire-format tool list from TOOLS_DETAILED.

    Tools in _FULL_DOCS_TOOLS (the hot, latency-sensitive path) pass through
    verbatim so their calling behaviour is unchanged. Everything else is
    slimmed to name + one-line description + bare parameter schemas
    (type + enum + required only); full docs stay reachable via `tool_details`.
    """
    slim = []
    for t in detailed:
        fn = t["function"]
        if fn["name"] in _FULL_DOCS_TOOLS:
            slim.append(t)
            continue
        params_in = fn.get("parameters", {}) or {}
        props = {}
        for key, spec in (params_in.get("properties") or {}).items():
            s = {"type": spec.get("type")}
            if "enum" in spec:
                s["enum"] = spec["enum"]
            props[key] = s
        params = {"type": "object", "properties": props}
        if params_in.get("required"):
            params["required"] = params_in["required"]
        slim.append({
            "type": "function",
            "function": {
                "name": fn["name"],
                "description": _TOOL_SHORT_DESC.get(fn["name"], ""),
                "parameters": params,
            },
        })
    return slim


TOOLS = _slim_tools(TOOLS_DETAILED)
TOOLS.append({
    "type": "function",
    "function": {
        "name": "tool_details",
        "description": (
            "Return the FULL usage docs (every parameter with detailed field "
            "guidance) for the named tools. Call this before using any tool "
            "you are not completely sure how to parameterise correctly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Comma-separated list of tool names.",
                },
            },
            "required": ["name"],
        },
    },
})

# Tools reserved for the self-chat pipeline (Kaya/Kolpo story rounds). They are
# stripped from human UI requests (see TOOLS_HUMAN) to cut static prompt
# tokens; the dispatch layer enforces the same split defensively.
AGENT_ONLY_TOOLS = {"track_theme"}

TOOLS_TOKEN_COST = len(json.dumps(TOOLS)) // 4

# Human-facing subset: everything except AGENT_ONLY_TOOLS.
TOOLS_HUMAN = [
    t for t in TOOLS if t["function"]["name"] not in AGENT_ONLY_TOOLS
]
TOOLS_HUMAN_TOKEN_COST = len(json.dumps(TOOLS_HUMAN)) // 4


def build_sys_content():
    with open(PROMPT_PATH, "r") as file:
        sys_content = file.read()
    model_list = "; ".join(f"{k}: {v['description']}" for k, v in IMAGE_MODELS.items())
    sys_content = sys_content.replace("%model_list%", model_list)
    sys_content = sys_content.replace("%_image_keys%", str(list(IMAGE_MODELS.keys())))
    return sys_content
