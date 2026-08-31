# Local AI — Self-Hosted LLM + Image Generation Stack

A self-hosted AI stack on a single laptop (RTX 3050, 4 GB VRAM, 16 GB RAM): a
chat web UI with tool use (web search, page fetch, image generation/editing,
file reading, tasks, reminders), an OpenAI-compatible API, an MCP gateway with
batched agent jobs, a multi-agent story-writing pipeline, tiered story hosting,
and Authentik SSO in front of everything.

The interactive chat engine (`chat-webui.py` + `server/`) is the core; the
other services (`server/mcp_gateway.py`, `markdown_hosting.py`, `self-chat.py`,
`scripts/`) build on top of it.

**Docs:** [ARCHITECTURE.md](ARCHITECTURE.md) — runtime design & diagrams ·
[TEST_STEPS.md](TEST_STEPS.md) — manual regression plan ·
[server_startup_commands.md](server_startup_commands.md) · [sso-debugging.md](sso-debugging.md)

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
| 8000 | MCP gateway | in-process thread of chat-webui | FastMCP + OAuth; `MCP_USER` token auth |
| 8079 | llama-server (CPU) | lazy / `restart_servers` | self-chat agents, 64K ctx, RAM-backed |
| 8081 | llama-server (GPU) | lazy / `restart_servers` | interactive UI, 24K ctx, VRAM-backed |
| 8083 | llama-server (guardrail) | lazy by MCP gateway / judge | small verify model, idle-unloads after 300s |
| 8080 | SearXNG | docker / systemd | web search backend |
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
#    Image (z_image): copy these into ~/local-ai/ComfyUI/models/:
#      diffusion_models/z_image_turbo_bf16.safetensors
#      text_encoders/qwen_3_4b.safetensors
#      vae/ae.safetensors

# 3. Run — nothing else to configure
cd ~/git/local-ai && python chat-webui.py
```

Access at `http://chat.local` or `http://localhost:3001`.

Authentication is unified SSO via Authentik — see "Authentication (SSO)" below.

Self-chat agents (editor/moderator/registered agents) default to the CPU
llama-server (port 8079) so they never compete with interactive UI users for
VRAM. Switch lanes with `SELF_CHAT_MODE` (env var or `server/config.py`):

```bash
SELF_CHAT_MODE=gpu python chat-webui.py
```

> **Note:** `server/config.py` currently ships with `FORCE_GPU_LANE = True`, a
> test-time flag that pins *everything* (including agents) to the GPU lane
> unless a request explicitly sets `mode` or the UI's research+CPU toggle. Set
> it to `False` for the intended CPU-agent behaviour.

`chat-webui.py` auto-starts the GPU llama-server on boot if it's down (CPU and
guardrail servers lazy-start on first use) and starts ComfyUI on demand. If you
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
    --jinja --n-gpu-layers 0 -fa off --ctx-size 65536 \
    -ctk q8_0 --no-mmproj-offload --device none \
    -t 6 -tb 6 --cache-reuse 256 \
    --reasoning-budget 2048 \
    --reasoning-budget-message "Reasoning limit reached, summarize final answer." \
    --slot-save-path ~/local-ai-files/kv-slots \
    --temp 1.0 --top-p 0.95 --top-k 64 --min-p 0.0 --repeat-penalty 1.0

cd ~/local-ai/ComfyUI && source venv/bin/activate && python main.py \
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
│                            features, registers the M proxy, starts 11 daemon threads,
│                            serves server/api.Handler on :3001
├── server/                  Core backend
│   ├── api.py               HTTP layer (routes, Handler, app-state injection)
│   ├── auth.py              Authentik identity: X-Authentik-* headers, JWT/JWKS, OIDC grant
│   ├── config.py            Constants, model ids, tool catalogs (TOOLS/TOOLS_DETAILED/
│   │                        TOOLS_HUMAN), llama-server arg sets, prompt builder
│   ├── db.py                Unified SQLite layer (~/local-ai-files/local_ai.db), LOCAL_AI_DB env
│   ├── tasks/…              (see features/) — batches_db.py, mcp_tasks_db.py: MCP queue tables
│   ├── input_guard.py       Pattern-based moderation: jailbreak/harmful input, strict output blocks
│   ├── openai_api.py        OpenAI-compatible /v1/* handlers (auth, models, SSE streaming)
│   ├── mcp_client.py        Outbound MCP client (mcp_config.json servers → extra chat tools)
│   ├── mcp_gateway.py       Inbound MCP server on :8000 (12 tools, OAuth, batch worker, verify)
│   ├── read_file.py         Upload text extraction (PDF/DOCX/DOC/XLSX)
│   ├── dotenv.py            Tiny .env parser
│   └── features/            Chat engine, one concern per module (see ARCHITECTURE.md)
│       ├── state.py         Shared containers/locks + the M entrypoint proxy; lane constants
│       ├── llm.py           llama-server load/unload FSM, task_mode routing, sampling router,
│       │                    KV slot checkpoints, _llm_worker (SSE parse), tool-call reassembly
│       ├── orchestration.py Queues, event loop, finalize (critic pass + L3 judge), reminders glue
│       ├── tools.py         Tool dispatch: web_search, fetch_page (SSRF-guarded), read_file/image,
│       │                    update_user_context, manage_tasks, track_theme, tool_details
│       ├── images.py        ComfyUI generate/edit workflows, VRAM choreography, image worker
│       ├── sessions.py      Per-user session files, prompt injection, auto-rename, compaction glue
│       ├── context.py       Token estimation, trim/compact, sanitize, effective-context reports
│       ├── shares.py        Public share snapshots + scoped image serving
│       ├── tasks_db.py      To-do tasks (SQLite) + manage_tasks tool handler
│       ├── themes_db.py     Creative-combination tracker (dedup) + track_theme tool handler
│       ├── users.py         Presence, per-user context files, agent registration
│       ├── judge.py         LLM safety/quality judges (harmful in/out, research verify, quality);
│       │                    judge calls hold while an image render is active (RAM guard)
│       ├── critic.py        Citation extraction + existence probe (direct fetch →
│       │                    bot-block → search) + per-citation LLM verification
│       │                    (reasoning-aware judge calls, 2048/4096 token budget)
│       ├── monitoring.py    Thermal/RAM/idle loops, server lifecycle, DDNS + GCP heartbeat
│       ├── surface_loader.py Fernet-decryptable attack-surface pattern files
│       └── openai_adapter.py Tool-call ↔ OpenAI SSE format adapters (incremental chunks)
├── src/                     React 19 + Vite SPA (built to dist/, served by chat-webui)
├── prompts/                 System prompts, persona/genre/task pools, judge prompts
│   └── surface_attacks/     Guardrail pattern/judge files (optionally .enc via SURFACE_ATTACKS_KEY)
├── scripts/                 authentik_bootstrap.py, encrypt_surface.py, gcp_heartbeat_server.py
├── searxng/                 SearXNG settings volume
├── markdown_hosting.py      Story hosting service on :3002 (FastAPI, free/premium/admin RBAC)
├── self-chat.py             Offline multi-agent story pipeline (CLI; editor gate
│                            + confidence, kaya↔kolpo cross-critique)
├── genre_creator.py         Interactive console helper to author task genre schemas
├── mcp_config.json          External MCP servers for mcp_client.py
│                            (codebase-search = codebase-memory-mcp graph)
├── docker-compose.yaml      Containerized stack + SearXNG
├── authentik-compose.yaml   Authentik identity provider
├── local_cloud.sh / gcp_nginx.conf   nginx front-ends (auth_request SSO gate, TLS)
├── setup.sh / restart_services.sh / stop_services.sh / local_cloud.sh
├── ARCHITECTURE.md          System design deep-dive (diagrams: lanes, FSMs, event loop…)
└── TEST_STEPS.md            Manual interface test plan (curl-level, run before trusting a deploy)
```

The data dir (`~/local-ai-files/`, shared into the container) holds: `model.json`,
`models.json`, `sys_prompt.txt`, `sessions/`, `shares.json`, `contexts/<user>.txt`,
`my-models/` (GGUFs), `ComfyUI/{input,output}`, `uploads/`, `kv-slots/`,
`stories/`, `local_ai.db` (tasks + theme log + MCP batches + per-user
`user_judges` assignments).

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

**Enabling steps** (one-time):

1. Fill the `AUTHENTIK_*` / `POSTGRES_*` secrets in `.env` (see `authentik-compose.yaml`).
2. Start Authentik: `docker compose -f authentik-compose.yaml up -d`.
3. Open `https://<host>/sso/if/flow/initial-setup/` and create the admin account.
4. Provision groups/users, the `local-ai` + machine-agent OIDC providers and the proxy
   outpost: `python3 scripts/authentik_bootstrap.py`.
5. Deploy the proxy outpost (`ghcr.io/goauthentik/proxy`) with the outpost token the
   bootstrap script prints, on `127.0.0.1:9010` (nginx's `ak_outpost` upstream).
6. Ensure the apps are only reachable through the nginx front-end in `local_cloud.sh`
   (the `auth_request` gate on `/ai/`, `/api/`, `/stories/`, `/story/`), then reload nginx.

## Architecture

The full runtime design — network topology, module layout, lane/queue
machinery, model state machines, REST surface, event loop, tool dispatch,
resource management and the moderation/verification pipeline — is documented
with diagrams in **[ARCHITECTURE.md](ARCHITECTURE.md)**.

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
`_batch_worker` → the guardrail lane, and run **LEVEL 2 (input) / LEVEL 3
(output)** LLM verification on the dedicated guardrail llama-server (:8083,
lazy-start, 300s idle-unload). `MCP_USER` owns the acting identity.

`server/mcp_client.py` is the *outbound* side: external MCP servers declared in
`mcp_config.json` are connected at startup and their tools are merged into the
chat tool list (per-session cache, version-invalidated via
`mcp_manager._tools_version`). The model sees them under namespaced ids
(`<server>__<tool>`); dispatch routes through `MCPClientManager.is_mcp_tool()`
→ `dispatch_mcp_tool()` (asyncio bridge to the client loop, results truncated
to 8k chars). The OpenAI lane never receives MCP tools (`no_tools: true` in
`openai_api.py`).

Shipped servers:

- **`codebase-search`** (`codebase-memory-mcp@latest`, stdio,
  `CBM_ALLOWED_ROOT=/home/palash/git`) — a code **knowledge graph** (15 tools:
  `search_graph`, `query_graph` Cypher, `trace_path`, `get_architecture`,
  `detect_changes`, …), exposed to chat as `codebase-search__*`. A repo returns
  **zero results until indexed**: run `index_repository(repo_path=…)` once per
  repo and re-index after significant changes (`index_status` /
  `detect_changes` report coverage and blast radius). Graph state lives under
  `~/.cache/codebase-memory-mcp/`; `local-ai` itself is indexed as project
  `home-palash-git-local-ai`.

### Markdown Hosting (`markdown_hosting.py`, :3002)

FastAPI site publishing the self-chat stories with role-gated collections
(`free` → `premium` → `admin`, resolved from Authentik groups). Routes:
collection index `GET /`, `GET /story/<col>/<id>` (rendered HTML + KaTeX, images
rewritten to auth-gated `/media/…`), `GET /story/…/content` (live incremental
poll while a story is being written), admin `DELETE /story/…`. Requires
`STORIES_PREMIUM_DIR` / `STORIES_ADMIN_DIR` env vars. External links in the
rendered stories open in a new tab (`render_story_html` adds
`target="_blank" rel="noopener noreferrer"` to `http(s)` hrefs; in-page
`#anchors` stay in-tab).

### Self-Chat Pipeline (`self-chat.py`)

Offline multi-agent story production: persona agents (kolpo/kaya…) hold
cross-critique rounds, then editor/moderator review and a moderation gate
(GREEN/RED, auto-RED on duplicate/citation-drop/empty-body/wrong-script/name-leak) writes
stories + moderation JSON to `~/local-ai-files/stories/`. CLI flags:
`--config <tasks.json>`, `--defaults`, `--dry-run` (validate + print plan, no
LLM calls), `--gpu` (pin agents to the GPU lane). Agents log in via the
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
  `_connection_manager` talks to over WireGuard; feeds nginx/DDNS.

## Frontend SPA (`src/` → `dist/`)

React 19 + Vite, no router/state library — chat UI served by chat-webui from
`dist/`. `App.jsx` owns auth check, session CRUD, the `/api/status/:id` polling
loop, location prompts and share routing (`/s/<token>`); `api.js` wraps the
`/api/*` endpoints and dispatches `auth:unauthorized` on 401. Components:
`Sidebar`, `ChatArea`, `Message` (markdown + KaTeX + DOMPurify, external links
open in a new tab via a sanitize hook, search popups,
reasoning block, TTS/copy/share buttons), `InputBar` (file upload with
extension whitelist), `ModelBar` (live temp/tps), `StatusBox`, `TaskPanel`,
`ImageLightbox`, `LocationPrompt`, `OverloadWarning`, `PublicShareView`.

Build with `npm install && npm run build` (Vite → `dist/`); `npm run dev`
proxies to the backend for development.

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
- **Header trust.** `X-Authentik-*` headers are trusted upstream; any path that lets a
  client reach :3001/:3002 directly (or a proxy that forgets to strip inbound
  `X-Authentik-*`) spoofs identity. Keep the nginx rules from `local_cloud.sh`.
- **Moderation is best-effort.** `input_guard` (pattern lists under
  `prompts/surface_attacks/`), the L3 judge and the critic citation pass catch the
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
  evacuation (600s cap, then proceed; 30s cooldown after the render).
- **RAM evacuation resumes tasks instead of failing them.** When
  `_evacuate_ram` fires, each lane's in-flight task is requeued to the front of
  its queue with the non-terminal `requeued` status (the UI pending bubble keeps
  polling — no error flash) plus a `_resumed` flag. After the servers restart,
  the queue's `start` event skips `_prepare_session`, so the user message is
  **not** appended twice and the answer still arrives exactly once.
- **Image/file endpoints require identity.** `/output/…`, `/uploads/…` and
  `/api/image/…` answer only with valid SSO headers or a verified agent JWT. Public
  share pages load images exclusively through `/api/public/share/<token>/image/…`,
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

There is **no automated test suite yet** (no `tests/`, no pytest/vitest config, no CI).
The current regression gate is the manual interface plan in **[TEST_STEPS.md](TEST_STEPS.md)** —
curl-level checks across the OpenAI API (§A), chat API + shares/tasks/presence (§B),
tools + SSRF (§C), MCP gateway (§D), story RBAC (§E), self-chat pipeline (§F),
SPA (§G), infra (§H), moderation & verification (§I), resource management (§J)
and the Android client (§K). Run it (especially §Pre-flight + §A)
after every deploy or llama-server restart before trusting results.
