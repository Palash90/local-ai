# Architecture — Local AI

Deep-dive into the runtime design of the chat engine and its companion
services. For setup, SSO enablement and a module map, see the
[README](README.md); the manual regression plan lives in
[TEST_STEPS.md](TEST_STEPS.md) (section numbers below are referenced there).

The engine's implementation lives in `server/` + `server/features/`;
`chat-webui.py` is the entrypoint that owns all shared state. Feature modules
never bind shared names at import — they resolve `M.<name>` at call time
through the `state.register_entrypoint(...)` proxy, which is what keeps
per-test monkeypatching of `chat-webui.<name>` working everywhere.

## System Design

### 1. Infrastructure & Network

```mermaid
graph TD
    subgraph Hardware ["Hardware (RTX 3050 Laptop)"]
        HW1["GPU: NVIDIA RTX 3050 — 4 GB VRAM"]
        HW2["RAM: 16 GB"]
        HW3["Same dev machine hosts everything"]
        HW4["Target: 2–4 concurrent users"]
    end

    subgraph ExternalServices ["External Services"]
        Docker["Docker Engine"]
        Docker --> SearXNG["SearXNG Container\nlocalhost:8080"]
        Docker --> Authentik["Authentik IdP (server 127.0.0.1:9008)\n(SSO portal + JWKS)"]
        Docker --> Outpost["Authentik proxy outpost\n127.0.0.1:9010"]
        Docker --> Nextcloud["Nextcloud cloud-app 127.0.0.1:8082\n+ cloud-db (mariadb)"]
        Nginx["Nginx Reverse Proxy + TLS\nauth_request SSO gate"]
        Avahi["avahi-daemon\nmDNS: chat.local"]
        GCP["GCP VM\nheartbeat + DDNS target\n(scripts/gcp_heartbeat_server.py\nvia WireGuard 10.66.66.1:9863)"]
    end

    subgraph Network ["Network Topology"]
        LAN["LAN Devices"] -->|"https://chat.local"| Nginx
        Nginx -.->|"auth_request"| Outpost
        Nginx -->|"proxy_pass"| HTTPServer["chat-webui.py\n127.0.0.1:3001\n(API + SPA + /v1 + MCP :8000 thread)"]
        Nginx -->|"proxy_pass"| MDHost["markdown_hosting.py\n127.0.0.1:3002"]
        Nginx -->|"proxy_pass"| CodeHost["code host\n127.0.0.1:9000"]
        HTTPServer -->|"localhost:8081"| LLamaGPU["llama-server (GPU)\ninteractive UI users"]
        HTTPServer -->|"localhost:8079"| LLamaCPU["llama-server (CPU)\nself-chat agents"]
        HTTPServer -->|"localhost:8083"| LLamaGuard["llama-server (guardrail)\njudge / L2-L3 verify"]
        HTTPServer -->|"localhost:8084"| LLamaEmbed["llama-server (embed)\nnomic embeddings"]
        HTTPServer -->|"localhost:8188"| ComfyUIRuntime["ComfyUI"]
        HTTPServer -->|"localhost:8080"| SearXNG
        HTTPServer -->|"nominatim.openstreetmap.org"| Nominatim["Reverse Geocoding"]
        SelfChat["self-chat.py"] -->|"Bearer JWT /api/chat"| HTTPServer
        MCPClient["MCP clients (Claude, …)"] -->|"https …/mcp"| Nginx --> HTTPServer
        HTTPServer -.->|"heartbeat POST (via\nconnection_manager systemd)"| GCP
    end
```

### 2. Code Layout & Build

```mermaid
graph TD
    subgraph CodeRepo ["Code (~/git/local-ai/)"]
        CR0["chat-webui.py\nEntrypoint — owns shared state,\nimports server.* + features.*"]
        CR0 --> CRa["server/\napi.py (HTTP layer), auth.py (SSO),\nconfig.py (constants+tools),\ndb.py (SQLite), input_guard.py,\nopenai_api.py, mcp_client/gateway"]
        CR0 --> CRb["server/features/\nllm, orchestration, tools,\nimages, sessions, context, shares,\njudge, critic, monitoring, tasks/themes_db,\nmusic/ (score DSL→WAV), tool_docs"]
        CR1["markdown_hosting.py\nStory site (FastAPI :3002)"]
        CR2["self-chat.py\nOffline agent pipeline (CLI)"]
        CR3["setup.sh\nBootstrap: deps, clones, build"]
        CR4["src/ → dist/\nReact 19 + Vite SPA"]
        CR5["llama.cpp/ (outside repo)\nBuilt: cmake -DGGML_CUDA=ON\nBinary: build/bin/llama-server"]
        CR6["ComfyUI/ (outside repo)\nvenv: ComfyUI/venv/"]
    end

    subgraph StatePattern ["Shared-state pattern"]
        SP1["features/* never bind shared names at import"]
        SP2["state.register_entrypoint(chat-webui)\n→ M proxy resolves M.<name> at call time"]
        SP3["api.py gets state via set_app_state()"]
    end
    CR0 --> SP2

    subgraph DataDir ["Data (~/local-ai-files/)"]
        DF1["model.json — LLM ids:\ngpu / cpu"]
        DF2["models.json — ComfyUI style defs (z_image…)"]
        DF3["sys_prompt.txt — template\n%model_list% %current_time% %current_location%"]
        DF4["session/sessions_<user>.json — per-user chats"]
        DF5["contexts/<user>.txt — per-user memory"]
        DF6["my-models/ — GGUF files"]
        DF7["ComfyUI/{input,output}/"]
        DF8["uploads/ — extract-file / upload-image drops"]
        DF9["shares.json — public share snapshots"]
        DF10["kv-slots/ — llama KV slot checkpoints"]
        DF11["stories/ — self-chat output (free/premium/admin trees)"]
        DF12["local_ai.db — SQLite (WAL): tasks, theme_log,\nMCP batches + items, mcp_tasks, user_judges\n(LOCAL_AI_DB override; legacy *.migrated)"]
        DF13["page_cache.db — persistent search/page cache\n(LOCAL_AI_PAGE_CACHE override)"]
        DF14["tts_cache/ — TTS audio + words cache\n(TTS_CACHE_* envs; optional secondary archive)"]
        DF15["music/ — generated WAV/MIDI per user,\nshowcase/ (public audition clips),\nsoundfonts/ + vendor/ (fluidsynth)"]
        DF16["tool_docs_cache/<user>.json — warm\ntool_details docs (hash-keyed,\nself-invalidating)"]
    end

    subgraph BuildFlags ["Build Flags"]
        BF1["llama.cpp\ncmake -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release"]
        BF2["ComfyUI venv pip install"]
        BF3["Frontend\nnpm install && npm run build → dist/"]
        BF4["System deps\ngit python3 cmake avahi-daemon\npdftotext catdoc antiword nginx docker.io"]
    end
```

### 3. Runtime Constants & Locks

```mermaid
graph TD
    subgraph RCNetwork ["Service URLs (server/config.py)"]
        RC1["LLAMA_BASE = localhost:8081 (GPU)"]
        RC2["LLAMA_BASE_CPU = localhost:8079 (CPU)\nserver ctx 32768, lane budget 24576\n(CPU_CTX_SIZE env, reasoning-budget 1024)"]
        RC3["LLAMA_BASE_GUARDRAIL = localhost:8083\n(VERIFY_PORT, gemma E2B, server ctx 16384 p2\n→ lane budget 8192)"]
        RC4["COMFYUI_URL = localhost:8188\nSEARXNG_URL = 127.0.0.1:8080"]
        RC5["HOST = 127.0.0.1 (CHAT_HOST)\nPORT = 3001 · MCP gateway :8000"]
    end

    subgraph RCLimits ["Limits and Pools (features/state.py)"]
        RL1["MAX_QUEUE_SIZE = 15 (per lane)"]
        RL2["MAX_INPUT_TOKENS = 24576\nAUTO_COMPACT_THRESHOLD = 70%"]
        RL3["MAX_TOOL_ROUNDS = default 10 /\nresearch 50 (UI research toggle)"]
        RL4["_llm_pools: gpu 1 / cpu 1 / guardrail 1\n(CPU_PARALLEL_SLOTS = 1)"]
        RL5["_tool_pools: gpu 2 / cpu 2 / guardrail 2"]
        RL6["Idle unload = 300s per lane\nVERIFY_IDLE_TIMEOUT = 300s\n(CPU override: CPU_IDLE_UNLOAD_SECONDS)"]
        RL7["LLM timeout = 600s · ComfyUI poll = up to ~300s"]
        RL8["ACTIVE_WINDOW_SECONDS = 120 (presence)"]
    end

    subgraph RCThermal ["Thermal and RAM Thresholds"]
        RT1["TEMP_THRESHOLD_ON = 90 C"]
        RT2["TEMP_THRESHOLD_OFF = 75 C"]
        RT3["RAM_EVAC_THRESHOLD = 95%"]
        RT4["RAM_RESUME_THRESHOLD = 70%"]
        RT5["FORCE_GPU_LANE = False by default (test flag:\npins all traffic to GPU lane; enable via\nFORCE_GPU_LANE=true in .env)"]
    end

    subgraph RCVerify ["Verification Budgets (critic/judge)"]
        RV1["VERIFY_RETRIES = 2 per citation"]
        RV2["VERIFY_FETCH_CHARS = 6000"]
        RV3["VERIFY_MAX_CITES_PER_URL = 3"]
        RV4["VERIFY_QUALITY_GATE = 80/100\nVERIFY_MAX_RETRIES = 2"]
        RV5["SAMPLING_BUCKETS: creative / code /\nfactual / chat (router picks per task)"]
        RV6["Judge render gate:\nwait_until_render_safe — 600s cap\n+ 30s cooldown while _image_active"]
    end

    subgraph RCThreads ["Thread Pools and Locks"]
        LK1["_llm_pools / _tool_pools — per lane"]
        LK2["_event_queue: queue.Queue\nsingle dispatcher"]
        LK3["_image_queue + _image_worker\nserializes image jobs (one GPU)"]
        LK4["_data_lock guards sessions/tasks/shares;\n_queue_locks+_queue_conds per lane"]
        LK5["_model_transition_lock serializes\nload/unload; _chat_generating counter\nblocks unload mid-stream"]
        LK6["_tokens_lock guards agent presence sets"]
    end
```

### 4. Server Startup

```mermaid
graph TD
    A["python chat-webui.py"] --> A1["At import:\nload .env + configs (model.json, models.json,\nsys_prompt.txt) → server/config.py"]
    A1 --> A2["Import features/* (they resolve state via M later)\nregister_entrypoint(chat-webui)\n_init_tasks_db + _init_themes_db + init_batches_db\n+ mcp_tasks ensure (SQLite, idempotent)\nbuild_sys_content\nset_app_state(...) → server/api"]
    A2 --> B["__main__: makedirs uploads,\nload_sessions, load_shares"]
    B --> C{"GPU llama-server /health\nlocalhost:8081?"}
    C -- "200 OK" --> D{"SearXNG reachable\n:8080?"}
    C -- "Dead" --> Restart["restart_servers:\nkill llama-servers + ComfyUI,\nspawn ComfyUI + GPU (CPU/guardrail/embed\nlazy-start on first use, not here),\npoll /health up to 120s, kill on timeout"]
    Restart --> D
    D -- "No" --> Exit["print ERROR & sys.exit(1)\n(web search is mandatory)"]
    D -- "Yes" --> E["Start 13 Daemon Threads"]
    E --> E1["ensure_embed_ready\n(embed llama-server :8084)"] & E2["_event_loop"] & E3["_queue_worker gpu"] & E4["_queue_worker cpu"] & E5["_queue_worker guardrail"] & E6["_mcp_db_worker\n(SQLite MCP task queue)"] & E7["_image_worker"] & E8["_idle_unload_loop 10s"] & E9["_periodic_cpu_kv_save_loop"] & E10["_thermal_monitor 10s"] & E11["_reminder_loop 12h"] & E12["run_mcp\n(MCP gateway :8000)"] & E13["start_mcp_client\n(outbound MCP servers)"]
    E --> F["ThreadingHTTPServer.serve_forever\n127.0.0.1:3001"]
```

CPU (8079) and guardrail (8083) llama-servers are **lazy**:
`ensure_llama_server` / the MCP gateway start them on first
use; the embed server (:8084) starts on its own eager thread instead.
`_idle_unload_loop` unloads the CPU/guardrail models after 300s idle
(CPU override: `CPU_IDLE_UNLOAD_SECONDS`).

The DDNS + GCP heartbeat component (`connection_manager.py`) is **not** a
chat-webui thread — it runs as its own systemd service
(`scripts/connection-manager.service`, `Wants=wg-quick@wg0.service`).

### 5. Model State Machines

Three independent state machines — one per llama-server lane.

```mermaid
stateDiagram-v2
    direction LR

    state "GPU server (8081) — UI users" as G {
        [*] --> unloaded: gpu
        unloaded --> loading : load_llama_model("gpu")
        loading --> chat_loaded : 200 from /models/load + health OK
        loading --> unloaded : failed
        chat_loaded --> unloading : unload_llama_model("gpu")
        unloading --> unloaded : 200 from /models/unload
        unloading --> chat_loaded : failed but health OK
        chat_loaded --> image_active : generate_image / edit_image
        image_active --> chat_loaded : free_comfyui_vram + reload
    }

    state "CPU server (8079) — self-chat agents" as C {
        [*] --> cpu_unloaded
        cpu_unloaded --> cpu_loading : load_llama_model("cpu")
        cpu_loading --> cpu_loaded : 200 + health OK
        cpu_loading --> cpu_unloaded : failed
        cpu_loaded --> cpu_unloading : unload
        cpu_unloading --> cpu_unloaded : 200
    }

    state "Guardrail server (8083) — judge/verify" as V {
        [*] --> guard_unloaded
        guard_unloaded --> guard_loaded : ensure_guardrail_ready\n(MCP batch, L2/L3 verify)
        guard_loaded --> guard_unloaded : 300s idle\n(VERIFY_IDLE_TIMEOUT)
    }
```

Image generation unloads the **GPU** and **guardrail** models for VRAM (ComfyUI)
*and* evicts the **CPU** model for RAM — the eviction is immediate (images don't
wait on the slow CPU lane: the render's up-front wait is scoped to the
gpu/guardrail VRAM lanes), and a round the unload force-kills is requeued to
resume after the render (see [HARDENING.md](HARDENING.md) §3-4). Per-lane idle timestamps (`_last_llm_use`,
`_cpu_last_llm_use`, `_guardrail_last_llm_use`; CPU governed by
`CPU_IDLE_UNLOAD_SECONDS`) drive independent unloads. On unload the KV
cache is checkpointed to `kv-slots/` (`--slot-save-path`, per-session
`_session_kv`) and restored after reload, so long conversations don't re-prefill.
A render eviction that kills a CPU round mid-stream can make that unload save
time out on the busy slot; a background **periodic saver**
(`_periodic_cpu_kv_save_loop`, `CPU_KV_SAVE_INTERVAL_SECONDS`, default 120 s)
keeps a recent prefix cached so the requeued round resumes from it rather than a
full re-prefill.

### 6. REST API Endpoints (`chat-webui` :3001)

```mermaid
graph TD
    Client([User Client])

    subgraph AuthEndpoints ["Auth (SSO)"]
        Client -->|"Browser: nginx auth_request → SSO portal"| SSO["X-Authentik-* forwarded upstream\n(Username/User, Groups/Group, Email, Name, UID)"]
        Client -->|"GET /api/check-auth"| CheckAuth["identity_from_headers →\n{authenticated, username, role, email}"]
        Client -->|"Agents: Authorization: Bearer <JWT>"| Bearer["Verify vs Authentik JWKS (issuer enforced,\naud not verified, 300s cache, fail-closed)\nserver/auth.py)"]
        Client -->|"Heartbeat presence"| Presence2["No server session to end;\n/api/leaving only marks heartbeat stale"]
    end

    subgraph SessionEndpoints ["Sessions"]
        Client -->|"POST /api/sessions"| NewSession["Create UUID session"]
        Client -->|"GET /api/sessions"| ListSessions["List user sessions (updated desc)"]
        Client -->|"GET /api/sessions/:id/messages"| GetMessages["Messages + token_estimate"]
        Client -->|"PUT /api/sessions/:id"| RenameSession
        Client -->|"DELETE /api/sessions/:id"| DeleteSession["Delete + cleanup output/upload images\n+ KV slots + TTS audio (share-protected refs kept)"]
    end

    subgraph ChatEndpoints ["Chat & Tasks"]
        Client -->|"POST /api/chat\n{session_id, message, image?, audio?,\nresearch?, cpu?, mode?, no_tools?, peer_review?\n+ always client_timestamp}"| Chat["→ {task_id}, queued per lane"]
        Client -->|"GET /api/status/:task_id"| Poll["status/message/response/tools_used/image"]
        Client -->|"GET/POST/PUT/DELETE /api/tasks"| Tasks["To-dos + reminders (SQLite;\nmanage_tasks ops; reminder cols)"]
        Client -->|"GET /api/themes"| Themes["Theme log + stats (UNIQUE scope+combo_hash\ndedup; track_theme ops)"]
        Client -->|"GET /api/model-status"| ModelStatus["model, tps, overheated, gpu_temp,\nram_evacuating, max_context, reminders"]
        Client -->|"POST /api/cancel/:id"| CancelChat["Cancel queued/in-flight task"]
    end

    subgraph ShareEndpoints ["Shares"]
        Client -->|"POST /api/shares · GET /api/shares\nDELETE /api/shares/:token[?purge=1]"| Shares["Assistant-only + owner-only snapshot\n(whitelist copy; _reasoning never stored)\nrevoke returns {session_exists, purged}"]
        Client -->|"GET /s/:token (SPA page)"| SharePage["Public read-only view\n(copy-link button; metadata hidden)"]
        Client -->|"GET /api/public/share/:token\n+ /image/:path + /file/:path\n+ POST .../tts (snapshot-only audio)"| ShareAPI["Snapshot JSON + scoped image/file serving\n+ scoped TTS (no auth, no client text)"]
        Client -->|"GET /api/public/music[/showcase]\n+ /api/public/music/:file"| Showcase["Unauthenticated music showcase page\n+ audition clips (music/showcase/)"]
    end

    subgraph UtilityEndpoints ["Utility"]
        Client -->|"POST /api/extract-file · POST /api/upload-image"| Uploads["Save to uploads/ → {url,name}"]
        Client -->|"GET /api/image/:id"| ImgEdit["Serve working image"]
        Client -->|"POST /api/location"| SetLocation["Nominatim reverse geocode"]
        Client -->|"GET/POST /api/user-context"| UserCtx["read/append; overwrite = admin only"]
        Client -->|"POST /api/tts · POST /api/tts-words"| TTS["md→speech cleanup → lang detect\n(en/es Piper, hi/te/bn/kn edge-tts)\nchunked <=1800 chars -> two-tier cache\n+ <key>.words.json; highlight offsets use\ntrue chunk audio durations (no drift)"]
        Client -->|"POST /api/internal/tts"| InternalTTS["Loopback-only + TTS_INTERNAL_TOKEN\n(story-page proxy; blank disables)"]
        Client -->|"GET /api/active-users · POST /api/leaving"| Presence["Active window tracking"]
        Client -->|"GET /output/… · GET /uploads/…"| ServeFiles["Identity + ownership-gated\nfile serving (resolve_image_file)"]
    end

    subgraph OpenAIEndpoints ["OpenAI-compatible /v1/* (Bearer OPENAI_API_KEY)"]
        Client -->|"GET /v1/ · GET /v1/models · /v1/models/:id"| V1M["Model list/retrieve"]
        Client -->|"POST /v1/chat/completions"| V1C["Non-stream + SSE streaming,\nincremental tool_calls, multimodal image_url"]
    end

    subgraph SPA ["Static / SPA Serving"]
        Client -->|"GET / · /s/:token · fallback"| SPAIndex["dist/index.html; assets from dist/"]
    end
```

### 7. Chat Ingress & Lane Routing

```mermaid
graph TD
    Client([User Client]) -->|"POST /api/chat"| AuthCheck{"identity via\nX-Authentik-* / JWT"}
    AuthCheck -- No --> AuthErr[401]
    AuthCheck -- Yes --> SessionCheck{"session exists\nand owned by user?"}
    SessionCheck -- No --> SessionErr[404]
    SessionCheck -- Yes --> Route{"Lane selection (api.py)"}
    Route -->|"user in _agent_users"| AgentLane["default SELF_CHAT_MODE\n(cpu); --gpu sends mode=gpu override"]
    Route -->|"human"| GPULane["gpu lane"]
    Route -->|"UI research+CPU toggle"| CPUMark["cpu_flagged → cpu lane"]
    Route -->|"explicit body mode"| Pin["pin gpu/cpu/guardrail\n(MCP gateway verify)"]
    Route -.->|"FORCE_GPU_LANE=true\n(opt-in test flag)"| ForceAll["everything → gpu unless\nexplicit mode / cpu_flagged"]
    AgentLane & GPULane & CPUMark & Pin --> QueueCheck{"len(lane queue) < 15?"}
    ForceAll --> QueueCheck
    QueueCheck -- No --> Busy503[503 Server Busy]
    QueueCheck -- Yes --> Enqueue["append to _task_queues[lane],\ntasks[task_id] = queued,\nnotify condition"]
    Enqueue --> ReturnTask["return {task_id} —\nclient polls /api/status/:id"]
```

Tasks submitted by `MCP_USER` (the MCP service account) over `/api/chat` are
flagged `_mcp: true` at admission (api.py), so the pipeline treats them exactly
like gateway-admitted MCP tasks — most importantly the **fail-closed L3 output
judge**. The flag rides through the queue's `start` event: the RAM/thermal
pause paths rewrite queued task dicts down to a minimal placeholder, so the
queue entry — not the pre-created task dict — is the source of truth for the
flag.

### 8. Queue Workers (one per lane — gpu / cpu / guardrail)

```mermaid
graph TD
    E2["_queue_worker(mode)\ngpu / cpu / guardrail"] --> QueueLoop["queue_cond[mode].wait on empty"]
    QueueLoop --> PauseCheck{"overheated (gpu only),\nram_evacuating, or image_active?"}
    PauseCheck -- Yes --> MarkWaiting["queued tasks → status waiting"]
    MarkWaiting --> PauseWait["cond.wait 5s"] --> QueueLoop
    PauseCheck -- No --> PopTask["pop head → _current_task_ids[mode]"]
    PopTask --> PostStart["event_post start\n(session, message, image, audio, user,\nclient_timestamp, research, no_tools,\nopenai_lane, _mcp, _resumed)"]
    PostStart --> TaskDoneWait{"poll task status every 0.5s"}
    TaskDoneWait -- "done / error / requeued" --> Clear["_current_task_ids[mode]=None\nnotify_all"] --> QueueLoop

    MCP["MCP gateway batches"] --> DBQ[("mcp_tasks SQLite table")] --> MW["_mcp_db_worker\n(polls → admits to the gpu/cpu lane\nwith the _mcp flag)"]
```

The human/agent lanes never wait behind each other. A third `_queue_worker`
runs for the **guardrail lane** — it carries judge-eligible work (MCP-admitted
tasks, self-chat theme-judge rounds) that executes on the guardrail server
(:8083). MCP chat tasks arrive through the SQLite queue via `_mcp_db_worker`
(routed onto the gpu lane by default, cpu when flagged) carrying `_mcp: true`;
the **guardrail server** (:8083) is where their L2/L3 judge calls execute —
generation stays on the gpu/cpu lane.

Tasks requeued by an emergency RAM evacuation (`_evacuate_ram`) ride the same
loop with two extra pieces of state: the task status becomes the **non-terminal
`requeued`** (so the UI keeps polling instead of seeing an error) and the queue
entry carries **`_resumed: true`**. The worker releases on `requeued`, picks the
entry back up once the servers are back, and the `start` event then skips
`prepare_session` — the user message (plus any tool trail and steering turns
from the interrupted attempt) is already in the session, so a resume never
duplicates the user turn in the chat.

The same `requeued`-with-`_resumed` path survives an image render: a CPU round
killed by the render's eviction is requeued to the front of its lane and
regenerated once ComfyUI finishes (see HARDENING.md §4).

### 9. Event Loop Pipeline (features/orchestration.py)

```mermaid
graph TD
    E1["_event_loop (single dispatcher)"] --> EvDispatch{"event type?"}
    EvDispatch -- "start" --> EvStart["store task metadata"] --> Resumed{"_resumed?\n(RAM-evacuation restart)"}
    Resumed -- "no (fresh task)" --> Prep["prepare_session (features/sessions.py):\n1. mode = task_mode(task)\n2. ensure + load lane's server\n3. inject sys prompt + date + location\n4. inject user context\n5. append user msg, auto-name session"] --> Router["sampling router (llm.py):\ntiny greedy classify call →\ncreative/code/factual/chat →\nper-request temperature/top_k/top_p"]
    Router --> Round0["start_llm_round(0)"]
    Resumed -- "yes (resume)" --> Round0

    EvDispatch -- "llm_ok" --> LLMOK{"tool_calls?"}
    LLMOK -- No --> Simple{"simple turn? (≤40 chars, no\nresearch/media/_mcp/_peer_review)"}
    Simple -- Yes --> Final2["_finalize_task directly:\npattern scan only (no sampling router,\nno quality / L3 LLM judge)"]
    Simple -- No --> VGate{"research? openai_lane?\nagent-user on cpu?"}
    VGate -- "research (non-openai)" --> Critic2["run_verification_worker\n(features/critic.py):\nresearch citations / answer-quality\njudge with bounded re-runs\n→ then _finalize_task"]
    VGate -- "cpu agent-user" --> Peer["run_peer_review_worker\n(features/critic.py): full cross-agent\ncritique round — the peer (kaya↔kolpo\nmap) reviews the reply in a dedicated\nLLM round DIRECTLY on the cpu llama\nserver (bypasses the lane queue, which\nis blocked waiting on this very task)\n→ _judge_result → _finalize_task"]
    VGate -- "else (UI gpu / openai_lane)" --> Final["_finalize_task:\n1. L3 output judge (features/judge.py):\nstrict pattern block + per-user judge;\nMCP lane (_mcp / guardrail) fail-closed,\nUI lane fail-open\n2. append msg, save sessions,\nstatus done, refresh idle stamp"]
    LLMOK -- Yes --> SubmitTools["append assistant msg,\npending_tools = N,\nsubmit to lane's _tool_pools"]

    EvDispatch -- "llm_err" --> LLMErr{"cpu lane & image_active?\n(round killed by render eviction)"}
    LLMErr -- Yes --> Requeue["requeue to lane front with _resumed\n(status requeued) -> resumes after render"]
    LLMErr -- No --> SetErr["_set_task_error -> status error"]
    EvDispatch -- "tool_ok / tool_err" --> ToolOK["append tool result,\npending -= 1"]
    ToolOK --> AllDone{"pending ≤ 0?"}
    AllDone -- No --> E1
    AllDone -- Yes --> RoundCap{"round+1 < MAX_TOOL_ROUNDS\n(10 default / 50 research)?"}
    RoundCap -- Yes --> NextRound["start_llm_round(N+1)"]
    RoundCap -- No --> MaxErr["error: max tool rounds"]
```

### 10. LLM Worker (features/llm.py)

```mermaid
graph TD
    StartRound["start_llm_round (lane from task_mode)"] --> Pool["_llm_worker in _llm_pools[lane]"]
    Pool --> Payload["payload: lane's model id, messages (trimmed\nby context.py budget), tools (slim TOOLS +\nMCP tools + tool_details; empty for\nTOOL_FREE_AGENTS / no_tools),\nmax_tokens, stream:true"]
    Payload --> Req["POST lane base /v1/chat/completions\n(8081 gpu / 8079 cpu / 8083 guardrail)\ntimeout 600s"]
    Req --> Parse["SSE parse: reasoning_content delta,\ncontent delta, tool_calls deltas\nreassembled by index"]
    Parse --> Post["event llm_ok / llm_err"]
    Req -. exception .-> Err["llm_err (friendly msg on vision/OOM)"]
    subgraph KV ["KV slot checkpoints"]
        K1["on unload: POST /slots/{id}?action=save\n→ kv-slots/<session>.dat;\ncpu lane also saves periodically\n(CPU_KV_SAVE_INTERVAL_SECONDS,\ndefault 120s) so a render-killed\nround resumes from a recent prefix"]
        K2["after load: action=restore —\nonly new tokens re-prefilled"]
    end
```

**Stable prompt prefix & warm tool docs (`llm._append_turn_context`,
`features/tool_docs.py`)** — the session's `system` message (message[0]) holds
only *stable* content: system prompt base + `<user_context>` + extra prompts.
Date/location (`<current_info>`), the research directive and cached tool docs
are appended per round as a trailing `system` block at the END of the payload
copy (never stored), so the prompt prefix — system block + history — stays
byte-identical turn over turn and KV checkpoints / `--cache-reuse` actually
hit. `_prepare_session` rewrites message[0] only when its stable content
changed (prompt hot-reload, user-context update) and then invalidates the
session's KV on both chat lanes. Related machinery: `get_sys_content()`
rebuilds `SYS_CONTENT` when `sys_prompt.txt`'s mtime changes (no restart
needed for prompt edits); `read_user_context` memoizes per user keyed on
`(mtime, size)`; and `tool_docs` persists each user's `tool_details` fetches
in `tool_docs_cache/<user>.json` keyed by a content hash of the live
`TOOLS_DETAILED` entry — later sessions whose request matches the tool's
keyword gate (`WARM_TRIGGERS`) get the docs injected without an LLM round,
and stale hashes silently re-fetch. A `[music-directive]` line rides the same
tail when the current request asks for music and none has been rendered,
keeping the small chat model's `generate_music` calling deterministic.

### 10.5 Archival Compaction (Pensieve — `features/pensieve/`)

Before the payload reaches any lane, `prepare_context_for_llm`
(`features/context.py`) runs Pensieve, a **deterministic, embed-free context
archival layer**. It is not the classic LLM summarization — it moves the
oldest conversation blocks out of the active context and into SQLite, leaving
a compact `[#id: topic (n messages)]` marker the model can recall on demand.

- **Block model.** A *block* is an unbreakable fused chain: a `user` message,
  or an `assistant` message plus every immediately-following `role="tool"`
  result (one per tool call, in order), up to the next user/assistant/system
  message. The `system` message is never archived. Blocks are the atomic unit
  of archival.
- **Trigger.** When the token estimate crosses `RELEVANCE_WATERMARK_FRAC`
  (default 0.55) of the lane's prompt budget, Pensieve archives the **oldest**
  blocks beyond the keep-recent window (`RELEVANCE_KEEP_RECENT`, default 6) to
  SQLite (`~/local-ai-files/pensieve.db`) and swaps them for `[#id: …]`
  markers in the working copy. The **stored session is never mutated** — all
  work happens on copies — and the session's KV cache is invalidated when the
  effective prefix changes.
- **Recall.** The `memory_read` tool (`features/tools.py`) → `pensieve.retrieval.memory_read`,
  which takes either `memory_ids` (exact archived block ids, taken only from
  `[#id]` markers) or a `query` (keyword search), scoped to the current
  session's own archive.
- **Knobs** (`features/pensieve/distill.py`): `RELEVANCE_DISTILL` (on by
  default), `RELEVANCE_WATERMARK_FRAC`, `RELEVANCE_KEEP_RECENT`,
  `PENSIEVE_MAX_UNITS` (200), `PENSIEVE_BLOCK_MAX_CHARS` (12000),
  `PENSIEVE_TOPIC_MAX_CHARS` (120).
- **Relation to classic compaction.** Pensieve runs first; whatever remains
  below the budget is passed on. If a session still exceeds
  `AUTO_COMPACT_THRESHOLD`, the older LLM-summarization path
  (`compact_messages_copy` + `trim_messages_for_context`) still applies to the
  remainder (`features/context.py`).

### 11. Tool Worker (features/tools.py)

```mermaid
graph TD
    Submit["lane _tool_pools"] --> TW["_tool_worker"] --> Parse["parse tc.function.arguments JSON"]
    Parse --> Choose{"tool?"}
    Choose -- web_search --> Search["SearXNG :8080 (query + city when known)"]
    Choose -- fetch_page --> Fetch["SSRF-guarded GET (no private IPs),\nchunked text, PDF page_images"]
    Choose -- generate_image --> GenLimit{"already imaged\nthis task?"}
    GenLimit -- Yes --> Reject["error: image limit reached"]
    GenLimit -- No --> IQ["_enqueue_image_job → _image_worker"]
    Choose -- edit_image --> IQ
    IQ --> Img["unload GPU model → ComfyUI workflow\n(z_image / models.json style, img2img\ndenoise) → poll 120s → free VRAM →\nreload + KV restore (CPU agents unaffected)"]
    Choose -- get_user_location --> Loc["status location_needed →\nwait browser geolocation 60s"]
    Choose -- read_file --> RF["uploads/ only; PDF/DOCX/DOC/XLSX extract"]
    Choose -- read_image --> RI["vision pass over /uploads or /output image"]
    Choose -- update_user_context --> UC["append timestamped entry to contexts/<user>.txt"]
    Choose -- manage_tasks --> MT["SQLite tasks CRUD + reminders"]
    Choose -- track_theme --> TT["theme_log: log/check/stats (agent-only)"]
    Choose -- generate_music --> GMusic["features/music: score DSL → parse → MIDI →
FluidSynth (per-voice soundfonts, multi-pass
+ PCM mix) → WAV; numpy-synth fallback"]
    Choose -- tool_details --> TD["return full TOOLS_DETAILED docs
(warms tool_docs cache per user)"]
    Choose -- "<server>__<tool>\n(mcp_client.py)" --> MCT["mcp_manager.is_mcp_tool →\ndispatch_mcp_tool (asyncio bridge,\nmcp_config.json server, 8k-char cap)"]
    Choose -- unknown --> Unk["error: unknown tool"]
    Search & Fetch & Img & GMusic & Loc & RF & RI & UC & MT & TT & TD & MCT & Unk --> Post["event tool_ok / tool_err"]
```

Outbound MCP tools (`mcp_config.json` servers — e.g. `codebase-search`, the
`codebase-memory-mcp` knowledge graph over `/home/palash/git`) are merged into
the wire tool list per session (`features/llm.py`; cache keyed on
`mcp_manager._tools_version`, so a server restart invalidates all sessions).
Dispatch is namespaced `<server>__<tool>`: `MCPClientManager.is_mcp_tool()`
gates the branch above and `dispatch_mcp_tool()` bridges the call onto the
client's asyncio loop (8k-char result cap). A repo yields no search results
until indexed once via `index_repository`; the graph persists under
`~/.cache/codebase-memory-mcp/`. The OpenAI lane pins `no_tools: true`, so only
UI/agent-lane tasks ever see these tools.

### 11.6 Music generation (features/music/)

A self-contained compose→render stack; no LLM or external service required at
render time. The chat-facing surface is the `generate_music` tool (score DSL →
playable WAV), plus a non-LLM `make music` chat shortcut
(`orchestration` start branch → `music.entry`) and a bulk-audition
**showcase** (`python -m server.features.music --showcase`).

- **DSL & arrangement.** `parse.py` compiles `@tempo/@genre/@mood/@section`
  directives and lane blocks (`[MELODY piano vol=90]`, note/chord/drum-hit
  tokens) via `theory.py` (pitch↔MIDI, GM program map incl. world-instrument
  aliases mapped to nearest GM colour). `random_arrange.py` composes full
  pieces from `genres.py`/`moods.py`/`harmony.py`/`rhythm.py`/`world_scales.py`
  (genre-authentic leads, tonic-cadence endings, song-form energy curves) —
  used by the shortcut, `--genre`, and the showcase.
- **Render.** `render.py` → `midi_out.py` (stdlib Type-0 writer: per-lane
  channels, drums→ch9, CC7 lane volumes, energy→CC11, legato articulation,
  release tail) → `fluid.py` renders with the vendored FluidSynth
  (`~/local-ai-files/music/vendor/`); `synth.py` is the numpy fallback and
  owns trailing-silence trimming (`trim_wav_silence`).
- **Per-voice soundfonts** (`fluid.py`): each lane's GM program maps to one
  SF2/SF3 — base `GeneralUser-GS.sf2`, with auto-discovered overrides
  (MuseScore_General.sf3 wins real strings/choir). Lanes are grouped per
  soundfont, each group rendered as its own `--fast-render` MIDI pass, and the
  WAVs summed (normalize-on-clip). Override map: `FLUID_SOUNDFONT_MAP` env
  (JSON `{program|"drum": path}`), base via `FLUID_SOUNDFONT`. Single group =
  classic one-pass render.
- **Named drum kits.** A percussion lane may name a kit (`[RHYTHM tabla]`);
  kits in `fluid.KIT_SOUNDFONTS` render from their own soundfont as a melodic
  group (regular channel, prog 0, GM-kit→file key `note_map`), mixed via the
   multi-pass machinery. Missing registered kit ⇒ render refuses
   ("not installed") — no rock-kit faking of tabla (provenance of
   `Tabla.sf2` in `soundfonts/PROVENANCE-Tabla.md`; the syllable→key map was
   settled by spectral analysis of one-shot renders, keys sound at K−12
   semitones over 60–82 — see `music/local/tabla-audition/`).
- **Outputs & meta.** WAV/MIDI land in `music/<user>/gen_<id>.*`; the task
  carries `music_file/music_score/music_url/music_levels/music_duration`,
  which `_finalize_task` attaches to the assistant message as
  `_music_url/_music_score/_music_levels` (UI player + lane meters + score
  fold-out). `duration_s` also feeds the critic's duration-claim gate (§13).
  Music files are share-protected and cleaned on chat deletion like images.
- **Showcase.** `showcase.py` renders every genre + instrument timbre once
  into the PUBLIC `music/showcase/` dir with `index.json`; served
  unauthenticated at `/api/public/music[/showcase]` (`api.py`) as a
  self-contained player page (`showcase_page.py`).

### 12. Resource Management (features/monitoring.py)

```mermaid
graph TD
    TM["_thermal_monitor (10s)"] --> Temp["nvidia-smi temp"]
    Temp --> Hot{"≥ 90 C?"}
    Hot -- Yes --> OH["_overheated = True"]
    OH --> Run{"GPU-lane task running?"}
    Run -- No --> Unload["unload GPU model\n(or free ComfyUI VRAM)"]
    Run -- Yes --> LetFinish["let task finish"]
    Hot -- No --> Cool{"was hot and ≤ 75 C?"}
    Cool -- Yes --> Clear["_overheated = False"]
    TM --> RAM{"RAM ≥ 95%?"}
    RAM -- Yes --> Evac["_evacuate_ram:\nrequeue in-flight to lane fronts\n(status requeued — non-terminal, UI keeps polling —\nentry flagged _resumed so the restart skips\nprepare_session and never re-appends the user msg),\nkill llama-servers + ComfyUI,\nwait ≤ 70%, restart_servers()"]
    IDLE["_idle_unload_loop (10s)"] --> ICheck{"per lane: loaded, idle > timeout\n(cpu: CPU_IDLE_UNLOAD_SECONDS),\nqueue/current task empty,\nnot streaming? (global counter)"}
    ICheck -- Yes --> ISave["save KV slot → unload lane"]
    IMG["images.py: unload gpu+guardrail (VRAM),\nevict cpu model (RAM, immediate — a killed round\nrequeues), reload + KV restore"] --> REC["recycle_comfyui (background thread):\nkill + reboot ComfyUI — --lowvram\nweights never return RAM otherwise\n(~8 GB held idle → evacuation trigger);\nCOMFYUI_RECYCLE_AFTER_RENDER=0 disables"]
    CM["connection_manager\n(systemd service,\nscripts/connection-manager.service)"] --> DNS["GoDaddy DDNS AAAA update\n(when public IPv6 changes)"]
    CM --> HB["heartbeat POST to GCP VM\nover WireGuard (10s)"]
```

The idle check deliberately has **no runtime-based stuck-task watchdog** — the
earlier `TASK_STUCK_TIMEOUT` force-error was removed. A wedged lane is reclaimed
by RAM evacuation, thermal pressure, image-render takeover, or the idle gate;
long research rounds run to completion by design (HARDENING.md §5).

`connection_manager` (DDNS + GCP heartbeat) runs as its **own systemd service**
(`scripts/connection-manager.service`, `Wants=wg-quick@wg0.service`), reading
`.env`; it is **not** one of the chat-webui daemon threads listed in §4.

### 13. Moderation & Verification Pipeline

```mermaid
graph TD
    In["User / agent / MCP input"] --> L1{"L1 pattern guard\n(server/input_guard.py, patterns from\nprompts/surface_attacks/, Fernet-optional;\nmatching is lowercase + diacritic-strip only —\nno fullwidth/zero-width handling, and the\ninjection check uses raw lower() without _normalize)"}
    L1 -- "is_jailbreak_attempt /\nis_harmful_request" --> Block1["refuse (MCP gateway pre-batch;\nguardrail lane)"]
    L1 -- pass --> L2{"L2 input LLM judge\n(mcp_gateway._run_llm_verify\n→ judge.py → guardrail :8083,\nfail-closed: judge down = blocked)"}
    L2 -- harmful --> Block2["refuse before generation"]
    L2 -- pass --> Gen["generation (lanes as above)"]
    Gen --> L3{"L3 output judge\n(orchestration._finalize_task:\nis_mcp_lane = mode 'guardrail'\nor task flagged _mcp)"}
    L3 --> Strict["is_strict_output_blocked reply"]
    L3 --> Judge["features/judge.mcp_output_judge\n(guardrail :8083, per-user judge model)"]
    Strict & Judge --> Lane{"lane?"}
    Lane -- "MCP (_mcp / guardrail)" --> FC["fail-closed: mark failed, drop output,\nmcp_task_update LEVEL 3 bookkeeping"]
    Lane -- "UI" --> FO["fail-open: deliver + record note"]
    Gen -->|"research answers"| Critic["features/critic.py:\neach (Author, Venue, Year) [url] citation\nexistence-probed (direct fetch → bot-block\n→ search) + re-fetched, LLM-checked;\n<70/100 quality or missing cites →\nre-schedule ≤ 2× (judge prompts)"]
    RAM["RAM guard: every judge POST\n(judge.py _judge_completion +\nmcp_gateway._run_llm_verify) holds\nwhile a ComfyUI render is active —\njudge.wait_until_render_safe\n(600s cap + 30s cooldown) — so a\njudge model load can never collide\nwith image generation"]
    L2 -.-> RAM
    Judge -.-> RAM
```

All judge LLM calls share one choke point (`judge._judge_completion`) and one
judge server (:8083). UI-lane extras beyond L3: the answer-quality judge (⚖
confidence chip, `llm_verify_answer_quality`) and the research citation judge
run via `run_verification_worker` with bounded re-runs; the former
input-request judge (pre-generation "request NN%" chip) was removed — its
load colliding with image renders was the main trigger of emergency RAM
evacuations.

**Deterministic requirement gates (`critic._requirement_mismatch`)** —
between the quality judge and delivery, a no-LLM pass compares the ANSWER and
the TASK against what was actually produced, and forces one bounded steering
re-run per mismatch: `image_needed`/`music_needed` (the request *asks for*
one — verb-proximity regexes, so a topic mention like "what sound does a
santoor make" never triggers; anaphoric reuse like "with the same image" is
satisfied by the session's prior artifact), `image_claimed`/`music_claimed`
(answer claims generation while this task rendered nothing — catches
parse-failed lies like "The Santoor piece has been generated"), and
`duration_claimed` (answer states a length > 1.5× the tool's real
`duration_s` + 10s — catches "…about a minute if looped"), `score_errors`
(the DSL compiled but the parser dropped tokens, so the track is broken —
the retry embeds the rejected tokens verbatim), and `length_mismatch` (an
explicit user duration — digits or spelled-out "about a minute" — vs a
rendered `duration_s` outside 0.6×–1.8× of the target). The music DSL docs
enforce the same contract upstream: minimum texture (MELODY+HARMONY always,
BASS+RHYTHM when rhythm is asked, single lane only for explicit solos) and
LENGTH MATH (bars ≈ seconds × BPM ÷ 240, sections sum to the target ±20%). Verdicts from all
judges, plus the re-run history, are appended to the final answer's reasoning
block as a `### Guardrail verification` trail (`critic._verification_addendum`).
Two finalize-side companions (`orchestration`): anaphoric artifact
**carry-over** (re-attaches the referenced prior `_image_url`/`_music_url`
card onto the new message) and pasted-path **stripping** (text lines that
merely restate a path the UI already renders as a card are removed; lines
whose artifact is *not* attached are kept). Assistant messages that contain
tool calls plus scratch prose are flagged `_draft`: kept in the LLM history,
hidden by the UI, and folded into the final answer's reasoning at finalize.

**Critic call budget & reasoning fallback** — `critic._critic_completion`
issues the critic's LLM calls with `max_tokens=2048` (retry doubles to 4096).
Reasoning-capable chat models can burn the whole token budget inside
`reasoning_content` before emitting the verdict, which used to surface as
`[critic] LLM call failed … empty content in response` and silently dropped
every citation verdict (fail-open). On empty `content` the critic now judges
on the reasoning text instead — the same fallback the L2 judge implements in
`mcp_gateway._judge_call`.

**Citation existence probe** — for a URL the research run never retrieved,
`critic._citation_exists` decides "does this source exist at all?" in this
order: (1) direct fetch of the URL (any page content → exists); (2) a
bot-block in the fetch error (403/405/429/forbidden/captcha/cloudflare) →
treated as existing but unfetchable; (3) the original SearXNG same-URL search,
last resort only. The search-only probe was removed as the primary check
because search indexes rarely return deep links (PMC article pages, hospital
blogs) verbatim, which branded real sources "likely fabricated".

**L2 judge precision caveat** — the input judge is a small model and can
false-positive on imperative technical phrasing: "Debug this Rust program …
identify the bug … it misbehaves at runtime" was classified HARMFUL while
near-identical "This Rust code fails to compile … identify the exact bug"
prompts passed. The L2 verdict path itself is fail-closed and working as
designed; benign code-debugging workloads hitting `LEVEL 2 LLM VERIFICATION
FAILED` are a judge-prompt/model-precision issue, not an infra failure
(check the `[guardrail][L2] raw verdict:` log line to distinguish).

**Confidence-gated restart ("start from scratch")** — research and UI
generation answers whose verification outcome lands below the confidence bar
get a full fresh re-run: the original message is re-submitted as a **new
task** through the normal send path (a new user turn is appended and a fresh
research/tool round starts), unlike the in-task `_reschedule` retry, which
appends a hidden `_steering` turn and re-runs within the same task's bounded
counters. Open issues being worked: the restart turn is visible in the UI as
a duplicate user message, and the per-task retry counters do not carry over —
a persistently low-confidence answer can chain restarts (each costing a full
research round) with no global cap.

**Agent (Kaya/Kolpo) verification** — two more layers on the CPU lane, both
fail-open (they never block delivery):

- *Offline story pipeline* (`self-chat.py`): after the deterministic gate, the
  re-opened **editor line** grades the story against the task/genre checklist
  (`VERDICT: CLEAN|FLAGGED / CONFIDENCE: NN/100 / FLAGS:`). FLAGGED stories are
  discarded wholesale — sessions deleted, story files removed — and the
  conversation restarts from scratch (up to `SELF_CHAT_EDITOR_RESTARTS`, then
  RED with the flags). A CLEAN story below the confidence threshold
  (`editor_min_confidence` task key / `SELF_CHAT_EDITOR_MIN_CONFIDENCE`, default
  70) triggers one extra cross-critique revision + one re-review; pre/post
  confidence is recorded in `.moderation.json` (written for GREEN too, so the
  story site shows a badge with the confidence number for every story).
- *Online agent replies* (cpu lane): `run_peer_review_worker` runs a full
  cross-agent critique round (`AGENT_PEER_MAP`, default kaya↔kolpo) — the peer
  reviews the reply in a dedicated LLM round directly on the cpu llama-server
  and the PEER VERDICT / CONFIDENCE verdict becomes the reply's confidence
  chip. Both layers resolve the per-agent judge model from the `user_judges`
  table (`resolve_judge_model`), so bigger verification models can be assigned
  to kolpo/kaya out-of-band.
