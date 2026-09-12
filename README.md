# Polu's AI Assistant — Self-Hosted LLM, Image & Music Generation Stack

A self-hosted AI stack on a single laptop (RTX 3050, 4 GB VRAM, 16 GB RAM): a
chat web UI with tool use (web search, page fetch, image generation/editing,
on-device music composition, file reading, tasks, reminders), an OpenAI-compatible API, an MCP gateway with
batched agent jobs, a multi-agent story-writing pipeline, tiered story hosting,
and Authentik SSO in front of everything.

The interactive chat engine (`chat-webui.py` + `server/`) is the core; the
other services (`server/mcp_gateway.py`, `markdown_hosting.py`, `self-chat.py`,
`scripts/`) build on top of it.

**Docs:** [ARCHITECTURE.md](ARCHITECTURE.md) — runtime design & diagrams ·
[TEST_STEPS.md](TEST_STEPS.md) — manual regression plan ·
[server_startup_commands.md](server_startup_commands.md)

> **AI collaboration disclosure.** This repository is developed with AI coding
> assistants working alongside the human maintainer — including code, tests,
> docs, and commit messages. As with all open-source software here, everything
> is provided as-is under the [MIT License](LICENSE), without warranty of any
> kind and without liability. AI-generated content may contain errors: review
> security-sensitive paths (auth, networking, file serving) yourself before
> exposing this stack beyond localhost.

## Highlights

- **Chat with real tool use** — web search, page fetch, file reading, tasks and
  reminders, user location, all inside the conversation.
- **On-device image generation & editing** (ComfyUI `z_image`) with VRAM
  choreography so the chat model survives renders.
- **Original music composition** — the `generate_music` score DSL renders to
  WAV on-device, in a mandatory song form (intro → verse → bridge → outro —
  each phase plays a different musical role, not one looped motif), with a
  public audition showcase ([Music generation](#music-generation)).
- **OpenAI-compatible `/v1/*` API** plus an **MCP gateway** (`:8000`) with
  batched agent jobs ([Companion Services](#companion-services)).
- **Layered guardrails** — L1 patterns, L2 input judge, L3 output judge, critic
  citation checks and deterministic requirement gates; fail-open on the UI,
  fail-closed on MCP traffic.
- **Multi-agent story pipeline** with editor/moderator gates, published to
  role-gated story hosting (`:3002`).
- **Multilingual read-aloud (TTS)** with word-level sync ([Voice](#voice--read-aloud-tts)).
- **Authentik SSO** in front of everything ([Authentication](#authentication-sso)).
- **Context engineering** — Pensieve archival compaction, warm tool-docs
  cache, hot-reloadable prompts, KV slot checkpoints across unloads.
- **Built for a 4 GB VRAM laptop** — idle unload, thermal/RAM guards, per-lane
  queues ([Architecture](ARCHITECTURE.md)).

## Requirements

- NVIDIA GPU with the driver working — check with `nvidia-smi`.
- **Host OS path:** CUDA toolkit (`nvcc`) needed to build llama.cpp. `setup.sh` installs
  it automatically if it's missing.
- **Dockerized path:** **NVIDIA Container Toolkit** on the *host* (for `--gpus`), plus
  CUDA toolkit *inside* the container (`setup.sh` installs it there too).

## Ports & Services

| Port | Service | Started by | Notes |
|---|---|---|---|
| 3001 | chat-webui (core API + SPA) | `python chat-webui.py` | binds `127.0.0.1` (`CHAT_HOST`) — expose only via nginx |
| 3002 | markdown hosting (stories) | `restart_services.sh` (uvicorn) | FastAPI app, role-gated collections |
| 8000 | MCP gateway | in-process thread of chat-webui | FastMCP + OAuth; `MCP_USER` token auth (plus an outbound `start_mcp_client` thread for external MCP servers) |
| 8079 | llama-server (CPU) | lazy / `restart_servers` | self-chat agents, 32K ctx, RAM-backed |
| 8081 | llama-server (GPU) | lazy / `restart_servers` | interactive UI, 24K ctx, VRAM-backed |
| 8083 | llama-server (guardrail) | lazy by MCP gateway / judge | small verify model, idle-unloads after 300s |
| 8084 | llama-server (embed) | lazy by chat-webui / `restart_servers` | serves `/embedding` (nomic); vector layer of `page_cache` |
| 8080 | SearXNG | docker / systemd | web search backend; `setup.sh` binds `127.0.0.1:8080`, `docker-compose.yaml` binds `8080:8080` (all interfaces) — bind to localhost if you don't need LAN-wide search |
| 8188 | ComfyUI | lazy on image request | image generation; recycled after every render to return its RAM (`COMFYUI_RECYCLE_AFTER_RENDER=0` to disable) |
| 9000 | code host | `restart_services.sh` | `code_host.py` (lives outside this repo) |
| 9010 | Authentik proxy outpost | docker | nginx `auth_request` upstream |

## Quick Start — Host OS

```bash
# 1. Clone and build
git clone <this-repo> ~/git/local-ai
cd ~/git/local-ai
bash setup.sh

# 2. Post-processing — download your models (setup.sh does NOT download them)
#    LLM (chat):   put GGUFs into ~/local-ai-files/my-models/
#                  model.json holds "gpu" (chat UI) and "cpu" (self-chat
#                  agents) model ids — edit if you use other models
#                  (the guardrail/verify model is VERIFY_MODEL in .env/config)
#    Embeddings:   nomic-embed-text-v1.5.Q8_0 into ~/local-ai-files/my-models/
#                  (served on :8084 for the page_cache vector layer)
#    Image (z_image): copy these into ~/local-ai/ComfyUI/models/:
#      diffusion_models/z_image_turbo_bf16.safetensors
#      text_encoders/qwen_3_4b.safetensors
#      vae/ae.safetensors

# 3. Run — nothing else to configure
cd ~/git/local-ai && python chat-webui.py
```

> chat-webui exits(1) on boot when SearXNG is unreachable on :8080, and
> `:3002` story hosting requires `STORIES_PREMIUM_DIR`/`STORIES_ADMIN_DIR`.
> `setup.sh` builds only — it never starts services; start them with
> `restart_services.sh` (rebuilds `dist/`, brings up WireGuard, restarts
> cloud-app + files scan) or the individual commands below.

Access at `http://chat.local` or `http://localhost:3001`.

Authentication is unified SSO via Authentik — see "Authentication (SSO)" below.

Self-chat agents (editor/moderator/registered agents) default to the CPU
llama-server (port 8079) so they never compete with interactive UI users for
VRAM. Lane selection has three independent controls (later wins per request):
`SELF_CHAT_MODE` env (default `cpu`), the `--gpu` CLI flag (pins agents to the
GPU lane, overrides the env), and the `FORCE_GPU_LANE=true` test flag (pins
*everything* to GPU unless a request sets `mode` or the UI research+CPU toggle):

```bash
SELF_CHAT_MODE=gpu python chat-webui.py
```

> **Note:** `server/config.py` ships with `FORCE_GPU_LANE = False` (the
> intended behaviour: agents go to the CPU lane, UI users to the GPU lane). It
> is a test-time flag that, when set to `FORCE_GPU_LANE=true` in `.env`, pins
> *everything* (including agents) to the GPU lane unless a request explicitly
> sets `mode` or the UI's research+CPU toggle. Leave it off for production.

`chat-webui.py` auto-starts the GPU llama-server on boot if it's down (boot
`restart_servers` only ensures GPU; CPU/guardrail lazy-start on first use, the
embed server starts on its own eager thread) and starts ComfyUI on demand. If you
prefer to run the services manually:

```bash
# GPU llama-server — interactive chat UI users (VRAM-backed, 24K context)
~/local-ai/llama.cpp/build/bin/llama-server \
    --host 127.0.0.1 --port 8081 \
    --models-dir ~/local-ai-files/my-models/ \
    --jinja -ngl 99 -fa on --ctx-size 24576 \
    -ctk q8_0 -ctv q8_0 --no-mmproj-offload \
    -t 8 -tb 8 -ub 512 --timeout 3600 \
    --cache-reuse 256 --slot-save-path ~/local-ai-files/kv-slots \
    --temp 1.0 --top-p 0.95 --top-k 64 --min-p 0.05

# CPU llama-server — automated self-chat agents (RAM-backed, concurrent)
~/local-ai/llama.cpp/build/bin/llama-server \
    --host 127.0.0.1 --port 8079 \
    --models-dir ~/local-ai-files/my-models/ \
    --jinja --n-gpu-layers 0 -fa off --ctx-size 32768 \
    -ctk q8_0 --no-mmproj-offload --device none \
    -t 4 -tb 4 --cache-reuse 256 \
    --reasoning-budget 1024 \
    --reasoning-budget-message "Reasoning limit reached, summarize final answer." \
    --slot-save-path ~/local-ai-files/kv-slots \
    --temp 1.0 --top-p 0.95 --top-k 64 --min-p 0.0 --repeat-penalty 1.0

# Embedding llama-server — nomic vectors for page_cache (CPU, 2048 ctx)
~/local-ai/llama.cpp/build/bin/llama-server \
    --host 127.0.0.1 --port 8084 \
    --models-dir ~/local-ai-files/my-models/ \
    --jinja --embedding --pooling mean --embd-normalize 2 \
    --n-gpu-layers 0 -fa off --ctx-size 2048 -b 2048 -ub 2048 -t 6

ComfyUI is launched as `comfy_main.py` (a rename of ComfyUI's stock `main.py`),
so its process can be targeted by name without colliding with other `main.py`
processes in the `restart_services.sh` / `stop_services.sh` / kill logic:

cd ~/local-ai/ComfyUI && source venv/bin/activate && python comfy_main.py \
    --lowvram \
    --input-directory ~/local-ai-files/ComfyUI/input \
    --output-directory ~/local-ai-files/ComfyUI/output
```

## Quick Start — Dockerized (GPU)

The repo ships a `docker-compose.yaml` that runs the same stack inside an
`ubuntu:24.04` container with GPU passthrough, with SearXNG as a sibling service.

```bash
# 1. Start services (SearXNG + ai-container with GPU + shared dirs)
docker compose up -d

# 2. Enter the container and run setup
docker exec -it ai-container bash
cd /root/git/local-ai
bash setup.sh

# 3. Post-processing — download models into the shared host dirs
#    (same list as the Host OS path; image models land in
#     ~/local-ai/ComfyUI/models/ on the HOST, mounted into the container)

# 4. Run — inside the container
cd /root/git/local-ai && python chat-webui.py
```

Access at `http://localhost:3001` (published from the container). Notes:

- **Requires the NVIDIA Container Toolkit on the host**; the compose already passes
  `--gpus` to the container.
- `setup.sh` auto-detects the container: runs without `sudo`, skips systemd/mDNS/nginx
  (not available in a container), skips starting SearXNG (provided by compose), and
  installs the CUDA toolkit inside the container if `nvcc` is missing.
- The container needs internet during setup (`apt`, `git clone`, `npm`, CUDA toolkit);
  the compose attaches it to `external-net`.
- Config/data dirs (`~/local-ai-files`) are shared with the host, so models and
  sessions persist across container restarts.

## Repository Layout

```
local-ai/
├── chat-webui.py            Entrypoint: owns ALL shared state, re-exports config +
│                            features, registers the M proxy, starts 13 daemon threads,
│                            serves server/api.Handler on :3001
├── server/                  Core backend
│   ├── api.py               HTTP layer (routes, Handler, app-state injection)
│   ├── auth.py              Authentik identity: X-Authentik-* headers, JWT/JWKS, OIDC grant
│   ├── config.py            Constants, model ids, tool catalogs (TOOLS/TOOLS_DETAILED/
│   │                        TOOLS_HUMAN), llama-server arg sets, prompt builder
 │   ├── db.py                Unified SQLite layer (~/local-ai-files/local_ai.db), LOCAL_AI_DB env
 │   ├── batches_db.py / mcp_tasks_db.py   MCP queue tables (PENDING→WORKING→COMPLETED|ERROR, boot requeue, prune keep=50)
│   ├── input_guard.py       Pattern-based moderation: jailbreak/harmful input, strict output blocks
│   ├── openai_api.py        OpenAI-compatible /v1/* handlers (auth, models, SSE streaming)
│   ├── mcp_client.py        Outbound MCP client (mcp_config.json servers → extra chat tools)
│   ├── mcp_gateway.py       Inbound MCP server on :8000 (12 tools, OAuth, batch worker, verify)
│   ├── read_file.py         Upload text extraction (PDF/DOCX/DOC/XLSX)
│   ├── dotenv.py            Tiny .env parser
│   └── features/            Chat engine, one concern per module (see ARCHITECTURE.md)
│       ├── state.py         Shared containers/locks + the M entrypoint proxy; lane constants
│       ├── llm.py           llama-server load/unload FSM (incl. embed lane), task_mode routing,
│       │                    sampling router, KV slot checkpoints, _llm_worker (SSE parse),
│       │                    tool-call reassembly
│       ├── orchestration.py Queues, event loop, finalize (critic pass + L3 judge), reminders glue
 │   ├── tools.py         Tool dispatch: web_search, fetch_page (SSRF-guarded, 15s
 │   │                    timeout, `max_chars=24000` default with chunk paging; HTML
 │   │                    stripped, PDF/CSV/XLSX parsed, legacy `.xls`/binaries refused;
 │   │                    DNS-resolved IPs checked, SearXNG URLs rewritten), read_file/image,
 │   │                    update_user_context, manage_tasks, track_theme (agent-only),
 │   │                    tool_details (agent-filtered), memory_read (Pensieve recall,
 │   │                    limit 5); `generate_image` limited to 1 per task,
 │   │                    `get_user_location` waits up to 60s for the browser
│       ├── images.py        ComfyUI generate/edit workflows, VRAM choreography, image worker
│       ├── sessions.py      Per-user session files, prompt injection, auto-rename, compaction glue
│       ├── context.py       Token estimation, trim/compact, sanitize, effective-context reports,
│       │                    Pensieve archival-compaction hook
│       ├── shares.py        Public share snapshots + scoped image serving
│       ├── tasks_db.py      To-do tasks (SQLite) + manage_tasks tool handler
│       ├── themes_db.py     Creative-combination tracker (dedup) + track_theme tool handler
│       ├── users.py         Presence, per-user context files, agent registration
│       ├── judge.py         LLM safety/quality judges (harmful in/out, research verify, quality);
│       │                    judge calls hold while an image render is active (RAM guard)
│       ├── critic.py        Citation extraction + existence probe (direct fetch →
│       │                    bot-block → search) + per-citation LLM verification
│       │                    (reasoning-aware judge calls, 2048/4096 token budget)
│       ├── monitoring.py    Thermal/RAM/idle loops, server lifecycle, embed/cpu/guardrail
│       │                    ensure-* lazy starts + idle unload
 │   ├── page_cache.py    SQLite page cache + nomic vector layer (posts to :8084);
 │   │                    TTLs 300s fresh / 30d stale / 365d timeless music-theory
 │   │                    (LLM TTL 60s–30d), embed budget
 │   │                    3000 chars with 2s timeout, `~/local-ai-files/page_cache.db`
 │   │                    (`LOCAL_AI_PAGE_CACHE` override), keyed-only degrade offline
│       ├── pensieve/        Archival context compaction (deterministic; see ARCHITECTURE.md §10.5)
│       ├── websearch/       search.py, fetch.py, relevance.py, vector_store.py (embed-scored hits)
│       ├── toolstrip.py / urlclassify.py   tool shaping + URL classification helpers
│       ├── surface_loader.py Fernet-decryptable attack-surface pattern files
│       └── openai_adapter.py Tool-call ↔ OpenAI SSE format adapters (incremental chunks)
├── src/                     React 19 + Vite SPA (built to dist/, served by chat-webui)
├── prompts/                 System prompts, persona/genre/task pools, judge prompts
│   └── surface_attacks/     Guardrail pattern/judge files (optionally .enc via SURFACE_ATTACKS_KEY)
├── scripts/                 authentik_bootstrap.py, encrypt_surface.py,
│                            gcp_heartbeat_server.py, connection_manager.py
│                            (+ gcp-heartbeat.service, connection-manager.service,
│                            ufw.conf)
├── searxng/                 SearXNG settings volume
├── markdown_hosting.py      Story hosting service on :3002 (FastAPI, free/premium/admin RBAC)
├── self-chat.py             Offline multi-agent story pipeline (CLI; editor gate
│                            + confidence, kaya↔kolpo cross-critique)
├── genre_creator.py         Interactive console helper to author task genre schemas
├── mcp_config.json          External MCP servers for mcp_client.py
│                            (codebase-search = codebase-memory-mcp graph)
├── docker-compose.yaml      Containerized stack + SearXNG
├── authentik-compose.yaml   Authentik identity provider
├── local_cloud.sh / gcp_nginx.conf   nginx front-ends: `local_cloud.sh` is the
│                                TLS-terminating home front-end with the `auth_request`
│                                SSO gate; `gcp_nginx.conf` is an unauthenticated
│                                WireGuard pass-through to 10.66.66.3 (offline page on 502/503/504)
├── setup.sh / restart_services.sh / stop_services.sh / local_cloud.sh
├── ARCHITECTURE.md          System design deep-dive (diagrams: lanes, FSMs, event loop…)
└── TEST_STEPS.md            Manual interface test plan (curl-level, run before trusting a deploy)
```

The data dir (`~/local-ai-files/`, shared into the container) holds: `model.json`,
`models.json`, `sys_prompt.txt`, `sessions/`, `shares.json`, `contexts/<user>.txt`,
`my-models/` (GGUFs), `ComfyUI/{input,output}`, `uploads/`, `kv-slots/`,
`stories/`, `pensieve.db` (archived conversation blocks), `local_ai.db` (unified
SQLite: `tasks`, `theme_log`, `mcp_batches` + `mcp_batch_items`, `mcp_tasks`,
`user_judges`, `_db_meta`; WAL mode; one-time migration renames legacy
`tasks.db`/`themes.db`/`mcp_batches.db` to `*.migrated`) and the separate
`page_cache.db` (persistent search/page cache).

## Authentication (SSO)

Authentication is unified **SSO via Authentik** — the single identity provider for
every app on the box. There is **no `users.json` and no per-app password database**;
users, passwords and roles live in Authentik.

**Two identity paths:**

1. **Browsers** — nginx runs an `auth_request` subrequest against the Authentik
   proxy outpost (`location /ak-auth-ai` in `local_cloud.sh`). If the SSO session is
   valid the outpost answers 200 and populates `X-Authentik-*` claim headers, which
   nginx forwards to the upstream apps. On 401 nginx sends the browser to the SSO
   portal (`@ak-sso-ai`). The SPA calls `/api/check-auth` on load to learn who the
   user is.
2. **Machine agents** (`self-chat.py`, MCP clients) — authenticate via Authentik's
   OAuth2 password grant (separate `AUTH_AGENTS_*` OIDC client) and send the JWT as
   `Authorization: Bearer <token>`. Backends verify the signature against Authentik's
   JWKS (`server/auth.py` → `identity_from_bearer`).

Resolved identity is always a dict — `username`, `email`, `name`, `groups`, `role`
(`free`/`premium`/`admin`, mapped from the user's Authentik groups via
`AUTH_ROLE_GROUPS`; highest wins) and the Authentik `uid`. Roles decide Story
collection access (`markdown_hosting`) and the "overwrite user context" admin action.

Exact header contract (nginx must forward — and strip inbound — all of these):
`X-Authentik-Username` (fallback `X-Authentik-User`), `X-Authentik-Groups`
(fallback `X-Authentik-Group`, split on `|`, comma or whitespace),
`X-Authentik-Email`, `X-Authentik-Name`, `X-Authentik-UID`. Group→role mapping
is case-insensitive with default role `free`.
Agent JWT claims resolve `preferred_username` → `username` → `email` → `sub`
for the username and `groups` (fallback `ak_groups`) for groups, with `sub` as
uid; the issuer (`AUTH_AGENTS_ISSUER`) is enforced while `aud` is not verified.
JWKS is cached 300s; if Authentik's JWKS is unreachable the request fails
(`RuntimeError`, fail-closed). The password grant uses scope
`openid profile email groups`.

**Enabling steps** (one-time):

1. Fill the `AUTHENTIK_*` / `POSTGRES_*` secrets in `.env` (see `authentik-compose.yaml`).
2. Start Authentik: `docker compose -f authentik-compose.yaml up -d`.
3. Open `https://<host>/sso/if/flow/initial-setup/` and create the admin account.
4. Provision groups/users, the `local-ai` + machine-agent OIDC providers and the proxy
   outpost: `python3 scripts/authentik_bootstrap.py`.
5. Deploy the proxy outpost (`ghcr.io/goauthentik/proxy`) with the outpost token the
   bootstrap script prints, on `127.0.0.1:9010` (nginx's `ak_outpost` upstream).
 6. Ensure the apps are only reachable through the nginx front-end in `local_cloud.sh`
    (the `auth_request` gate on `/ai/`, `/api/` except `/api/public/`, `/stories/`,
    `/story/<free|premium|admin>`, `/media/<col>` and `/search/`; `/cloud/`,
    `/code/`, `/v1/`, `/s/`, `/mcp` have no `auth_request`), then reload nginx.

> The machine-agent path additionally needs the `AUTH_AGENTS_ISSUER`,
> `AUTH_AGENTS_JWKS_URL`, `AUTH_AGENTS_TOKEN_URL`, `AUTH_AGENTS_CLIENT_ID`
> (which doubles as the app slug) and `AUTH_AGENTS_CLIENT_SECRET` envs so
> backends can verify agent JWTs — browser SSO alone is not enough for agents.

## Architecture

The full runtime design — network topology, module layout, lane/queue
machinery, model state machines, REST surface, event loop, tool dispatch,
resource management and the moderation/verification pipeline — is documented
with diagrams in **[ARCHITECTURE.md](ARCHITECTURE.md)**. That also covers the
newer subsystems: the embedding server (`:8084`) powering the `page_cache`
vector layer, the `websearch/` relevance + vector-store pipeline, the Pensieve
archival-compaction layer (ARCHITECTURE.md §10.5), the music subsystem (§11.6),
and the deterministic artifact/verification gates (§13).

Resource hardening — how lanes stay isolated, when models auto-unload (and how
that is verified), and how tasks survive an image render or RAM evacuation — is
in **[HARDENING.md](HARDENING.md)**; its manual regression steps live in
TEST_STEPS.md `## J`.

## Companion Services

### MCP Gateway (`server/mcp_gateway.py`, :8000)

A FastMCP (streamable HTTP) server **in-process with chat-webui** (started by the
`run_mcp` thread), fronted by nginx with OAuth metadata at
`/.well-known/oauth-authorization-server`, `/authorize`, `/oauth/token` and an
`EnforcementAuthMiddleware` (401 without a valid bearer). It exposes 12 tools:
`get_user_context`, `list_sessions`, `create_session`, `get_session_messages`,
`rename_session`, `send_chat_message`, `get_message_status`, `start_chat_batch`,
`get_batch_status`, `get_batch_results`, `submit_batch_results`, `get_image`.

Batches queue into SQLite (`batches_db.py` + `mcp_tasks_db.py`), drain through
`_batch_worker` → the gpu/cpu lane (flagged `_mcp`), and run **LEVEL 2 (input) / LEVEL 3
(output)** LLM verification on the dedicated guardrail llama-server (:8083,
lazy-start, 300s idle-unload). `MCP_USER` owns the acting identity. Batch limits:
max 50 items, 2400s per item, 15s poll cadence, keep last 50, 3 retries;
`wait_hint` is 60s (research) / 30s (tools) / 20s (plain), re-poll no faster than
every 15–20s. Note the gateway exposes chat/batch tools only — `web_search` /
`fetch_page` exist solely on the chat lane. Token refresh uses a 60s margin;
upstream timeouts are 30s (60s for images).

`server/mcp_client.py` is the *outbound* side: external MCP servers declared in
`mcp_config.json` are connected at startup and their tools are merged into the
chat tool list (per-session cache, version-invalidated via
`mcp_manager._tools_version`). The model sees them under namespaced ids
(`<server>__<tool>`); dispatch routes through `MCPClientManager.is_mcp_tool()`
→ `dispatch_mcp_tool()` (asyncio bridge to the client loop, results truncated
to 8k chars with a truncation footer, `MCP_TOOL_TIMEOUT=300s`, env-overridable).
Servers with `enabled: false` are skipped and raw (non-namespaced) tool names
still route. The OpenAI lane never receives MCP tools (`no_tools: true` in
`openai_api.py`).

Shipped servers:

- **`codebase-search`** (`codebase-memory-mcp@latest`, stdio,
  `CBM_ALLOWED_ROOT=/home/palash/git`) — a code **knowledge graph** (e.g.
  `search_graph`, `query_graph` Cypher, `trace_path`, `get_architecture`,
  `detect_changes`, … — the exact tool set is defined by the external MCP
  server, not this repo), exposed to chat as `codebase-search__*`. A repo returns
  **zero results until indexed**: run `index_repository(repo_path=…)` once per
  repo and re-index after significant changes (`index_status` /
  `detect_changes` report coverage and blast radius). Graph state lives under
  `~/.cache/codebase-memory-mcp/`; `local-ai` itself is indexed as project
  `home-palash-git-local-ai`.

### Markdown Hosting (`markdown_hosting.py`, :3002)

FastAPI site publishing the self-chat stories with role-gated collections
(`free` → `premium` → `admin`, resolved from Authentik groups; guests see only
`free`). Routes: collection index `GET /`, `GET /story/<col>/<id>` (rendered HTML + KaTeX, images
rewritten to auth-gated `/media/…`), `GET /story/…/content` (live incremental
poll while a story is being written), `GET /media/<col>/<id>/<file>`, admin
`DELETE /story/…`, plus the read-aloud player (`/prose-segments`,
`/audio/{idx}`, `/words`, proxied to the chat-webui TTS engine — see Voice
below). Requires `STORIES_PREMIUM_DIR` / `STORIES_ADMIN_DIR` env vars (fail-fast
at startup); role resolution falls back to a hardcoded map (`palash`→admin,
`totan`→premium) when headers are absent. External links in the
rendered stories open in a new tab (`render_story_html` adds
`target="_blank" rel="noopener noreferrer"` to `http(s)` hrefs; in-page
`#anchors` stay in-tab).

### Self-Chat Pipeline (`self-chat.py`)

Offline multi-agent story production: persona agents (kolpo/kaya…) hold
cross-critique rounds, then editor/moderator review and a moderation gate
(GREEN/RED, auto-RED on duplicate/citation-drop/empty-body/wrong-script/name-leak) writes
stories + moderation JSON to tiered output dirs (`STORIES_FREE_DIR` /
`STORIES_PREMIUM_DIR` / `STORIES_ADMIN_DIR` by role; readers prefer
`<story>.edited.md` when present) under `~/local-ai-files/stories/`. CLI flags:
`--config <tasks.json>`, `--defaults`, `--dry-run` (validate + print plan, no
LLM calls), `--gpu` (pin agents to the GPU lane, overrides `SELF_CHAT_MODE`).
Agents log in via the
Authentik machine-client password grant and use the normal `/api/chat` API;
theme dedup goes through `track_theme`.

The **editor gate** grades every finished story against the task/genre
checklist (`VERDICT / CONFIDENCE / FLAGS`): flagged stories are discarded and
the conversation restarts from scratch (`SELF_CHAT_EDITOR_RESTARTS`, default 2,
then RED with the flags); clean stories below the confidence threshold
(`editor_min_confidence` task key or `SELF_CHAT_EDITOR_MIN_CONFIDENCE`,
default 70) get one cross-critique revision + re-review. `.moderation.json` is
written for GREEN too and carries the confidence number. Online (cpu-lane)
agent replies get a full cross-agent peer-review round (`AGENT_PEER_MAP`,
default kaya↔kolpo) whose verdict becomes the reply's confidence chip; both
resolve per-agent judge models from the `user_judges` table.

### Scripts

- `scripts/authentik_bootstrap.py` — one-time Authentik provisioning (groups, users,
  OIDC apps, outpost token) via the Authentik admin API.
- `scripts/encrypt_surface.py` — Fernet-encrypt `prompts/surface_attacks/*.txt` to
  `.enc` (set `SURFACE_ATTACKS_KEY` to enable decryption at load).
- `scripts/gcp_heartbeat_server.py` — the GCP-side receiver (DNS + heartbeat) that
  `connection_manager.py` talks to over WireGuard; feeds nginx/DDNS.
- `scripts/connection_manager.py` — the box-side DDNS (GoDaddy) + heartbeat sender;
  runs as its own systemd service (`scripts/connection-manager.service`), not a
  chat-webui thread.

## Frontend SPA (`src/` → `dist/`)

React 19 + Vite, no router/state library — chat UI served by chat-webui from
`dist/`. `App.jsx` owns auth check, session CRUD, background polling (other
sessions' tasks every 2s, `model-status` every 2s), location prompts and
hand-rolled `/s/<token>` share routing (plus `sessionStorage:opencode_pending`
resume and `localStorage last_sid` restore); `api.js` wraps the
`/api/*` endpoints and dispatches `auth:unauthorized` on 401 (which wipes app
state; an interstitial blocks direct port-3001 use without sign-in, and logout
redirects to Authentik `sign_out?rd=/`). Components:
`Sidebar` (Chats/Shares tabs, rename prompt, delete confirm, share rows with
preview/copy/revoke incl. purge confirm), `ChatArea` (smart autoscroll,
select-aware scroll suppress, `msgIndex` for shares), `Message` (markdown +
KaTeX + DOMPurify, external links forced `_blank`, search hover popups + fetch
page modal, tool badges, elapsed `⏱`/confidence `⚖` chips, artifact file chips,
per-code-block Copy, OCR `<details>` collapse, hidden system/tool/tool-call-only
filtering, empty-response fallback, reasoning block, TTS/copy/share buttons),
`InputBar` (image downscale to 1920px JPEG 0.8 → `/api/upload-image`, other files
→ `/api/extract-file` as `[FILE:]` prefix; 10 MB cap, 100 MB for PDFs; unknown
types prompt-to-confirm rather than hard-reject; Research toggle = 50 rounds and
gates the CPU toggle; Send↔Queue/Stop swap; Enter-to-send desktop-only; 120px
autogrow), `ModelBar` (status labels, red `<5` t/s warning, context donut
green/yellow/red at 60/80% with compressed `(raw)` tokens, Tasks + reminder-count
badge in the user menu), `StatusBox` (search/image/edit/thinking states),
`TaskPanel` (CRUD with priorities/dates), `ImageLightbox` (wheel zoom 0.25–10x,
drag/pan, pinch, Esc/back-button close via history trap), `LocationPrompt`
(Allow/Deny/blocked-permission path, 10s geolocation timeout), `OverloadWarning`
(distinct RAM-evacuation vs GPU-thermal text), `PublicShareView` (copy-link
topbar, `Shared by` line, metadata hidden).

Build with `npm install && npm run build` (Vite → `dist/`, `base: './'`); `npm run dev`
proxies only `/api` and `/output` to `http://localhost:3000` (stale: backend is
:3001), `npm run preview` serves the build locally.

## Music generation

The chat composes original music on-device: the `generate_music` tool takes a
compact score DSL (`@genre/@mood/@tempo/@section` directives plus lanes like
`[MELODY musicbox vol=90]` of note/chord/percussion tokens) and renders it to
a playable WAV + MIDI through a vendored FluidSynth — no network, no neural
model. The UI attaches an audio player with per-lane level meters and a
fold-out score; a public, unauthenticated **showcase** — one clip per genre
and per instrument timbre — lives at `/api/public/music/showcase` (:3001).

- Typing exactly `make music` in chat renders a random full arrangement
  instantly (non-LLM shortcut; handy for testing the render stack).
- The model fetches the DSL language once per conversation via
  `tool_details('generate_music')`; per-user caching makes later sessions
  compose directly (see ARCHITECTURE §10/§11).
- Re-render all audition clips after soundfont/DSL changes:
  `PYTHONPATH=. python3 -m server.features.music --showcase`.
- **Per-voice soundfonts:** every lane resolves to exactly one SF2/SF3 by GM
  program — base `GeneralUser-GS.sf2`, with auto-discovered upgrades (e.g.
  real strings/choirs from `MuseScore_General.sf3`); groups render as
  separate passes and are mixed. Env: `FLUID_SOUNDFONT` (base file),
  `FLUID_SOUNDFONT_MAP` (JSON `{"<program>|drum": "<path>"}` overrides),
  `MUSIC_SHOWCASE_DIR`. Soundfonts + the vendored fluidsynth live under
  `~/local-ai-files/music/` (see server_startup_commands.md).
- `[RHYTHM tabla]` renders from a real tabla soundfont when installed
  (`Tabla.sf2` is wired via `KIT_SOUNDFONTS`; if it's missing the render
  refuses rather than faking tabla on a rock kit).
- Missing fluidsynth/soundfonts degrade gracefully to the numpy synth.
- Playback streams a small Ogg Opus sidecar (`music_stream_url`, 64 kbps via
  `MUSIC_OPUS_BITRATE`, ~22× smaller than WAV, encoded with stdlib ctypes
  against system libopus — no ffmpeg/pip deps; served as `audio/ogg`); the UI
  fetches it once into a blob (HTTP `immutable` cache covers reloads), so
  replay/seek never re-buffers. Missing sidecar or failed fetch falls back to
  the WAV; Download keeps the full WAV.
- Scores must stay compact (~1100 chars; the DSL doc enforces this and asks
  for 2-3 short vamps per phase — never one vamp looped for 20 bars, and
  melody duets must trade the lead, not sprinkle a second voice): longer
  scores get cut off by the generation token budget, which makes llama.cpp
  reject the tool call. The server retries malformed tool calls twice with a
  shorten-and-re-emit steer before failing the task. The DSL doc itself only
  rides prompts while needed (pruned appendices, dropped once a render
  delivers, re-trimmed inside the token budget), so long music sessions
  can't overflow the context.

## Voice / read-aloud (TTS)

Each assistant message has a speak button: play → pause → resume from the
paused word, plus a stop button that resets to the beginning (`Message.jsx`
`SpeakButton`; playback failures never leave the button stuck).
`POST /api/tts` first cleans chat markdown into speakable text (formatting,
links/URLs, code blocks and table separators are never spoken; `Word:`
becomes `Word,`), detects the language (`[xx]` tag, Indic scripts, Spanish
markers), then synthesizes:

| Lang | Voice | Backend |
| ---- | ----- | ------- |
| en | en_US-lessac-high | Piper, local/offline |
| es | es_MX-claude-high | Piper, local/offline |
| hi | hi-IN-SwaraNeural (female) | edge-tts, online |
| te | te-IN-ShrutiNeural (female) | edge-tts, online |
| bn | bn-BD-NabanitaNeural (female) | edge-tts, online |
| kn | kn-IN-SapnaNeural (female) | edge-tts, online |

Piper voices are cached process-wide and synthesis is lock-serialized;
audio is stored in a two-tier content-addressed disk cache (`~/local-ai-files/tts_cache/`,
plus optional read-only secondary archive via `TTS_CACHE_SECONDARY_DIR` in `.env`),
pruned to 1 GB by default. Long messages are synthesized in sentence-bounded
chunks (≤1800 chars each, up to 8,000 chars on both authenticated and public
routes) and concatenated into one blob.
Deleting a chat session or revoking a share with purge removes its orphaned
audio files from both primary and secondary cache directories.
Piper `.onnx` files live in `~/.piper_voices/` (downloaded, not in the repo).
Known limit: Bengali SSML `<phoneme>` overrides were tried and reverted —
the `edge-tts` library escapes markup into literal speech and the service
rejects `<phoneme>` for Bengali voices, so Bengali অ-nuances (দেখলো/যেন-type
words) follow whatever the neural voice produces.

**Word-level sync + seeking.** Both engines emit word timestamps (Piper
phoneme alignments via the `onnx` package; edge-tts `WordBoundary` stream),
stored as `<key>.words.json` beside each cached blob (atomic writes; corrupt
files are dropped on read). `POST /api/tts-words` returns them for chat text;
multi-chunk offsets advance by true decoded audio duration (WAV header /
MP3 frame walk), not last-word end, so highlight never drifts down a story.
The story player highlights the speaking word and seeks on word click, aligning
DOM tokens to timestamps with lookahead (page markup the synthesis never saw
is skipped, not misassigned).

**Story pages** (`markdown_hosting` `/stories/`) get a 🔊 player in the sticky
topbar: prose segments from `/story/{c}/{id}/prose-segments`, audio per segment
from `/story/{c}/{id}/audio/{idx}`, words from `/story/{c}/{id}/words` — all
RBAC-enforced (guests: free stories only), chained with pause/resume/stop, and
a "▶ Tap to play" recovery when the browser blocks autoplay after a long fetch.
Story text uses `story_markdown_to_speech_text` (metadata header, round markers,
verdict blocks stripped — prose only).

**Public shares** expose a scoped, unauthenticated `POST
/api/public/share/<token>/tts` that synthesizes only the stored snapshot text
(client text ignored); the share page shows a copy-link button and hides
generation metadata (elapsed/confidence/tool badges).

**TTS env knobs** (all in `.env`, see `server/config.py`): `TTS_CACHE_DIR`
(default `~/local-ai-files/tts_cache`), `TTS_CACHE_SECONDARY_DIR` (unset =
secondary tier skipped), `TTS_CACHE_MAX_BYTES` (default 1 GB),
`TTS_MAX_CHARS` / `TTS_MAX_CHARS_PUBLIC` (default 8000/8000),
`TTS_CHUNK_CHARS` (default 1800), `TTS_INTERNAL_TOKEN` (loopback secret for
markdown_hosting → chat-webui; blank disables story audio).

## Security & Deployment Notes

> **Intended scope: a private, trusted home deployment** — e.g. a household of a few
> users on a home LAN (this project targets ~2–4 concurrent users). The following
> limitations are **accepted risk** for that use case. This stack is **not** built for
> production, the public internet, or a shared LAN where many unknown users work —
> do **not** use it under those conditions.

> **Authentication is unified SSO.** Browser access requires an Authentik session
> (nginx `auth_request`), and per-user identity comes from the forwarded
> `X-Authentik-*` headers (see "Authentication (SSO)"). The notes below assume the
> SSO-enabled nginx front-end (`local_cloud.sh`); running `chat-webui.py` directly
> bypasses all of it.

- **Bypass on bare `chat-webui.py`.** It binds `127.0.0.1` by default (`CHAT_HOST`),
  so it is only reachable through the nginx front-end or from the box itself. Setting
  `CHAT_HOST=0.0.0.0` re-exposes every endpoint without the SSO gate — don't.
- **Header trust.** The trusted upstream set is exactly `X-Authentik-Username`
  (fallback `X-Authentik-User`), `X-Authentik-Groups` (fallback
  `X-Authentik-Group`), `X-Authentik-Email`, `X-Authentik-Name` and
  `X-Authentik-UID`; any path that lets a
  client reach :3001/:3002 directly (or a proxy that forgets to strip inbound
  `X-Authentik-*`, *including the singular fallbacks*) spoofs identity. Keep the nginx rules from `local_cloud.sh`.
- **Moderation is best-effort.** `input_guard` (pattern lists under
  `prompts/surface_attacks/`), the L2 input judge (fail-closed on MCP/gateway
  traffic), L3 judge and the critic citation pass catch the
  common cases — MCP/guardrail traffic is screened fail-closed, but the interactive
  UI lane is deliberately **fail-open** (a judge outage must never drop a reply).
  The small L2 judge model can also **false-positive on benign technical phrasing**
  (e.g. "Debug this Rust program" prompts have been blocked as HARMFUL while
  equivalent "identify the bug" phrasing passes) — if a batch item dies with
  `LEVEL 2 LLM VERIFICATION FAILED`, check the `[guardrail][L2] raw verdict:` log
  line before assuming bad input. Tasks submitted by `MCP_USER` over `/api/chat`
  are flagged `_mcp` and get the same fail-closed L3 output judge as
  gateway-admitted MCP traffic. There is no kid-safe filter; choose your model
  accordingly.
- **Judge calls pause during image renders.** Every judge POST
  (`judge.wait_until_render_safe`) holds while ComfyUI is generating, so a judge
  model load can never collide with a render and trigger an emergency RAM
  evacuation (600s cap, then proceed; 30s cooldown after the render; judge LLM
  calls have a 240s timeout floor, 90s minimum).
- **RAM evacuation resumes tasks instead of failing them.** When
  `_evacuate_ram` fires, each lane's in-flight task is requeued to the front of
  its queue with the non-terminal `requeued` status (the UI pending bubble keeps
  polling — no error flash) plus a `_resumed` flag. After the servers restart,
  the queue's `start` event skips `_prepare_session`, so the user message is
  **not** appended twice and the answer still arrives exactly once.
- **Image/file endpoints require identity + ownership.** `/output/…`, `/uploads/…` and
  `/api/image/…` answer only with valid SSO headers or a verified agent JWT, and
  `/output/…` paths must match the caller's username (generated images are user-scoped).
  Public share pages load images exclusively through `/api/public/share/<token>/image/…`,
  which serves only files referenced by that share's snapshot.
- **CORS is wide open.** Responses carry `Access-Control-Allow-Origin: *`. SSO cookies
  are HttpOnly + SameSite, so cross-origin pages cannot ride the session, but same-origin
  scripts can call the API.
- **No TLS on bare 3001.** Always use the TLS-terminating nginx front-end
  (`local_cloud.sh` / `gcp_nginx.conf`); never port-forward 3001/8081/8079/8083 directly.
- **Third-party calls.** `fetch_page`, web search (SearXNG backends), location lookup
  (Nominatim), DDNS (GoDaddy) and optional edge-tts all leave the box; `fetch_page`
  rejects private-IP targets (SSRF guard). Disable or replace if strict data
  residency matters.
- **Compose exposes SearXNG on all host interfaces** (`8080:8080`), while the host path
  binds it to `127.0.0.1`. Bind it to localhost if you don't need LAN-wide search.

## Testing

Unit tests live beside the code (`server/features/tests/`,
`server/features/music/tests/`, `server/features/pensieve/tests/` — 74 tests
at last count); run them with `python -m pytest server/features/tests
server/features/music/tests server/features/pensieve/tests -q
--import-mode=importlib` (the import mode matters: without it the local
`server/dotenv.py` shadows the real `dotenv` package during collection).
The manual interface plan in **[TEST_STEPS.md](TEST_STEPS.md)** covers what
unit tests can't — curl-level checks across the OpenAI API (§A), chat API +
shares/tasks/presence (§B), tools + SSRF (§C), MCP gateway (§D), story RBAC
(§E), self-chat pipeline (§F), SPA (§G), infra (§H), moderation & verification
(§I) and resource management (§J). Run it (especially
§Pre-flight + §A) after every deploy or llama-server restart before trusting
results.