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
        Docker --> Authentik["Authentik IdP\n(SSO portal + JWKS)"]
        Docker --> Outpost["Authentik proxy outpost\n127.0.0.1:9010"]
        Nginx["Nginx Reverse Proxy + TLS\nauth_request SSO gate"]
        Avahi["avahi-daemon\nmDNS: chat.local"]
        GCP["GCP VM\nheartbeat + DDNS target\n(scripts/gcp_heartbeat_server.py\nvia WireGuard 10.66.66.1:9863)"]
    end

    subgraph Network ["Network Topology"]
        LAN["LAN Devices"] -->|"https://chat.local"| Nginx
        Nginx -.->|"auth_request"| Outpost
        Nginx -->|"proxy_pass"| HTTPServer["chat-webui.py\n127.0.0.1:3001\n(API + SPA + /v1 + MCP thread)"]
        Nginx -->|"proxy_pass"| MDHost["markdown_hosting.py\n127.0.0.1:3002"]
        Nginx -->|"proxy_pass"| CodeHost["code host\n127.0.0.1:9000"]
        HTTPServer -->|"localhost:8081"| LLamaGPU["llama-server (GPU)\ninteractive UI users"]
        HTTPServer -->|"localhost:8079"| LLamaCPU["llama-server (CPU)\nself-chat agents"]
        HTTPServer -->|"localhost:8083"| LLamaGuard["llama-server (guardrail)\njudge / L2-L3 verify"]
        HTTPServer -->|"localhost:8188"| ComfyUIRuntime["ComfyUI"]
        HTTPServer -->|"localhost:8080"| SearXNG
        HTTPServer -->|"nominatim.openstreetmap.org"| Nominatim["Reverse Geocoding"]
        SelfChat["self-chat.py"] -->|"Bearer JWT /api/chat"| HTTPServer
        MCPClient["MCP clients (Claude, …)"] -->|"https …/mcp"| Nginx --> HTTPServer
        HTTPServer -->|"heartbeat POST"| GCP
    end
```

### 2. Code Layout & Build

```mermaid
graph TD
    subgraph CodeRepo ["Code (~/git/local-ai/)"]
        CR0["chat-webui.py\nEntrypoint — owns shared state,\nimports server.* + features.*"]
        CR0 --> CRa["server/\napi.py (HTTP layer), auth.py (SSO),\nconfig.py (constants+tools),\ndb.py (SQLite), input_guard.py,\nopenai_api.py, mcp_client/gateway"]
        CR0 --> CRb["server/features/\nllm, orchestration, tools,\nimages, sessions, context, shares,\njudge, critic, monitoring, tasks/themes_db"]
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
        DF12["local_ai.db — SQLite: tasks, theme_log, MCP batches"]
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
        RC2["LLAMA_BASE_CPU = localhost:8079 (CPU)\nctx 65536, reasoning-budget 2048"]
        RC3["LLAMA_BASE_GUARDRAIL = localhost:8083\n(VERIFY_PORT, gemma E2B, ctx 16K)"]
        RC4["COMFYUI_URL = localhost:8188\nSEARXNG_URL = 127.0.0.1:8080"]
        RC5["HOST = 127.0.0.1 (CHAT_HOST)\nPORT = 3001 · MCP gateway :8000"]
    end

    subgraph RCLimits ["Limits and Pools (features/state.py)"]
        RL1["MAX_QUEUE_SIZE = 15 (per lane)"]
        RL2["MAX_INPUT_TOKENS = 24576\nAUTO_COMPACT_THRESHOLD = 70%"]
        RL3["MAX_TOOL_ROUNDS = default 10 /\nresearch 50 (UI research toggle)"]
        RL4["_llm_pools: gpu 1 / cpu 4 / guardrail 1\n(CPU_PARALLEL_SLOTS = 4)"]
        RL5["_tool_pools: gpu 2 / cpu 2 / guardrail 2"]
        RL6["Idle unload = 300s per lane\nVERIFY_IDLE_TIMEOUT = 300s"]
        RL7["LLM timeout = 600s · ComfyUI poll = 120s"]
        RL8["ACTIVE_WINDOW_SECONDS = 120 (presence)"]
    end

    subgraph RCThermal ["Thermal and RAM Thresholds"]
        RT1["TEMP_THRESHOLD_ON = 90 C"]
        RT2["TEMP_THRESHOLD_OFF = 75 C"]
        RT3["RAM_EVAC_THRESHOLD = 95%"]
        RT4["RAM_RESUME_THRESHOLD = 70%"]
        RT5["FORCE_GPU_LANE = True (test flag:\npins all traffic to GPU lane)"]
    end

    subgraph RCVerify ["Verification Budgets (critic/judge)"]
        RV1["VERIFY_RETRIES = 2 per citation"]
        RV2["VERIFY_FETCH_CHARS = 6000"]
        RV3["VERIFY_MAX_CITES_PER_URL = 3"]
        RV4["VERIFY_QUALITY_GATE = 70/100\nVERIFY_MAX_RETRIES = 2"]
        RV5["SAMPLING_BUCKETS: creative / code /\nfactual / chat (router picks per task)"]
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
    A1 --> A2["Import features/* (they resolve state via M later)\nregister_entrypoint(chat-webui)\n_init_tasks_db + _init_themes_db (SQLite)\nbuild_sys_content\nset_app_state(...) → server/api"]
    A2 --> B["__main__: makedirs uploads,\nload_sessions, load_shares"]
    B --> C{"GPU llama-server /health\nlocalhost:8081?"}
    C -- "200 OK" --> D{"SearXNG reachable\n:8080?"}
    C -- "Dead" --> Restart["restart_servers:\nkill both llama-servers + ComfyUI,\nspawn ComfyUI + GPU + CPU llama-servers,\npoll /health up to 120s, kill on timeout"]
    Restart --> D
    D -- "No" --> Exit["print ERROR & sys.exit(1)\n(web search is mandatory)"]
    D -- "Yes" --> E["Start 11 Daemon Threads"]
    E --> E1["_event_loop"] & E2["_queue_worker gpu"] & E3["_queue_worker cpu"] & E13["_mcp_db_worker\n(SQLite MCP task queue)"] & E4["_image_worker"] & E5["_idle_unload_loop 10s"] & E6["_thermal_monitor 10s"] & E7["_reminder_loop 30s"] & E8["_connection_manager\n(DDNS + GCP heartbeat)"] & E9["run_mcp\n(MCP gateway :8000)"] & E10["start_mcp_client\n(outbound MCP servers)"]
    E --> F["ThreadingHTTPServer.serve_forever\n127.0.0.1:3001"]
```

CPU (8079) and guardrail (8083) llama-servers are **lazy**: `ensure_llama_server`
/ the MCP gateway start them on first use; `_idle_unload_loop` unloads their
models after 300s idle.

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

Image generation unloads **only** the GPU server; CPU agents keep serving.
Per-lane idle timestamps (`_last_llm_use`, `_cpu_last_llm_use`,
`_guardrail_last_llm_use`) drive independent unloads. Before any unload the KV
cache is checkpointed to `kv-slots/` (`--slot-save-path`, per-session `_session_kv`)
and restored after reload, so long conversations don't re-prefill.

### 6. REST API Endpoints (`chat-webui` :3001)

```mermaid
graph TD
    Client([User Client])

    subgraph AuthEndpoints ["Auth (SSO)"]
        Client -->|"Browser: nginx auth_request → SSO portal"| SSO["X-Authentik-* forwarded upstream"]
        Client -->|"GET /api/check-auth"| CheckAuth["identity_from_headers →\n{authenticated, username, role}"]
        Client -->|"Agents: Authorization: Bearer <JWT>"| Bearer["Verify vs Authentik JWKS\n(server/auth.py)"]
        Client -->|"POST /api/register-agent · POST /api/logout"| AgentTok["Agent token / session end"]
    end

    subgraph SessionEndpoints ["Sessions"]
        Client -->|"POST /api/sessions"| NewSession["Create UUID session"]
        Client -->|"GET /api/sessions"| ListSessions["List user sessions (updated desc)"]
        Client -->|"GET /api/sessions/:id/messages"| GetMessages["Messages + token_estimate"]
        Client -->|"PUT /api/sessions/:id"| RenameSession
        Client -->|"DELETE /api/sessions/:id"| DeleteSession["Delete + cleanup output/upload images"]
    end

    subgraph ChatEndpoints ["Chat & Tasks"]
        Client -->|"POST /api/chat\n{session_id, message, image?, audio?,\nresearch?, cpu?, mode?, no_tools?}"| Chat["→ {task_id}, queued per lane"]
        Client -->|"GET /api/status/:task_id"| Poll["status/message/response/tools_used/image"]
        Client -->|"GET/POST/PUT/DELETE /api/tasks"| Tasks["To-dos + reminders (SQLite)"]
        Client -->|"GET /api/themes"| Themes["Theme log + stats"]
        Client -->|"GET /api/model-status"| ModelStatus["model, tps, overheated, gpu_temp,\nram_evacuating, max_context, reminders"]
    end

    subgraph ShareEndpoints ["Shares"]
        Client -->|"POST /api/shares · GET /api/shares\nDELETE /api/shares/:token"| Shares["Snapshot a message"]
        Client -->|"GET /s/:token (SPA page)"| SharePage["Public read-only view"]
        Client -->|"GET /api/public/share/:token\n+ /image/:path"| ShareAPI["Snapshot JSON + scoped image serving"]
    end

    subgraph UtilityEndpoints ["Utility"]
        Client -->|"POST /api/extract-file · POST /api/upload-image"| Uploads["Save to uploads/ → {url,name}"]
        Client -->|"GET /api/image/:id"| ImgEdit["Serve working image"]
        Client -->|"POST /api/location"| SetLocation["Nominatim reverse geocode"]
        Client -->|"GET/POST /api/user-context"| UserCtx["read/append; overwrite = admin only"]
        Client -->|"POST /api/tts"| TTS["Piper local, edge-tts fallback"]
        Client -->|"GET /api/active-users · POST /api/leaving"| Presence["Active window tracking"]
        Client -->|"GET /output/… · GET /uploads/…"| ServeFiles["Identity-gated file serving"]
    end

    subgraph OpenAIEndpoints ["OpenAI-compatible /v1/* (Bearer OPENAI_API_KEY)"]
        Client -->|"GET /v1/models · /v1/models/:id"| V1M["Model list/retrieve"]
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
    Route -.->|"FORCE_GPU_LANE=True\n(test flag)"| ForceAll["everything → gpu unless\nexplicit mode / cpu_flagged"]
    AgentLane & GPULane & CPUMark & Pin --> QueueCheck{"len(lane queue) < 15?"}
    ForceAll --> QueueCheck
    QueueCheck -- No --> Busy503[503 Server Busy]
    QueueCheck -- Yes --> Enqueue["append to _task_queues[lane],\ntasks[task_id] = queued,\nnotify condition"]
    Enqueue --> ReturnTask["return {task_id} —\nclient polls /api/status/:id"]
```

### 8. Queue Workers (one per lane)

```mermaid
graph TD
    E2["_queue_worker(mode)\ngpu / cpu"] --> QueueLoop["queue_cond[mode].wait on empty"]
    QueueLoop --> PauseCheck{"overheated and mode=gpu\nor ram_evacuating?"}
    PauseCheck -- Yes --> MarkWaiting["queued tasks → status waiting"]
    MarkWaiting --> PauseWait["cond.wait 5s"] --> QueueLoop
    PauseCheck -- No --> PopTask["pop head → _current_task_ids[mode]"]
    PopTask --> PostStart["event_post start\n(session, message, image, audio,\nuser, client_timestamp, research, no_tools)"]
    PostStart --> TaskDoneWait{"poll task status every 0.5s"}
    TaskDoneWait -- "done or error" --> Clear["_current_task_ids[mode]=None\nnotify_all"] --> QueueLoop

    MCP["MCP gateway batches"] --> DBQ[("mcp_tasks SQLite table")] --> MW["_mcp_db_worker\n(polls → admits to guardrail lane)"]
```

The two human/agent lanes never wait behind each other. Guardrail-lane tasks
arrive through the SQLite queue via `_mcp_db_worker`, not an in-memory list.

### 9. Event Loop Pipeline (features/orchestration.py)

```mermaid
graph TD
    E1["_event_loop (single dispatcher)"] --> EvDispatch{"event type?"}
    EvDispatch -- "start" --> EvStart["store task metadata"] --> Prep["prepare_session (features/sessions.py):\n1. mode = task_mode(task)\n2. ensure + load lane's server\n3. inject sys prompt + date + location\n4. inject user context\n5. append user msg, auto-name session"] --> Router["sampling router (llm.py):\ntiny greedy classify call →\ncreative/code/factual/chat →\nper-request temperature/top_k/top_p"]
    Router --> Round0["start_llm_round(0)"]

    EvDispatch -- "llm_ok" --> LLMOK{"tool_calls?"}
    LLMOK -- No --> Final["_finalize_task:\n1. research answers → critic pass\n(citation re-verify, features/critic.py)\n2. L3 output judge (features/judge.py):\nstrict pattern block + per-user judge;\nMCP lane fail-closed, UI lane fail-open\n3. append msg, save sessions,\nstatus done, refresh idle stamp"]
    LLMOK -- Yes --> SubmitTools["append assistant msg,\npending_tools = N,\nsubmit to lane's _tool_pools"]

    EvDispatch -- "llm_err" --> LLMErr["_set_task_error → status error"]
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
        K1["before unload: POST /slots/{id}?action=save\n→ kv-slots/<session>.dat"]
        K2["after load: action=restore —\nonly new tokens re-prefilled"]
    end
```

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
    Choose -- tool_details --> TD["return full TOOLS_DETAILED docs"]
    Choose -- "mcp:* (mcp_client.py)" --> MCT["call external MCP server tool\n(mcp_config.json)"]
    Choose -- unknown --> Unk["error: unknown tool"]
    Search & Fetch & Img & Loc & RF & RI & UC & MT & TT & TD & MCT & Unk --> Post["event tool_ok / tool_err"]
```

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
    RAM -- Yes --> Evac["_evacuate_ram:\nrequeue in-flight to lane fronts,\nkill llama-servers + ComfyUI,\nwait ≤ 70%, restart_servers()"]
    IDLE["_idle_unload_loop (10s)"] --> ICheck{"per lane: loaded,\nidle > 300s, queue empty,\nnot _chat_generating?"}
    ICheck -- Yes --> ISave["save KV slot → unload lane"]
    CM["_connection_manager"] --> DNS["GoDaddy DDNS AAAA update\n(when public IPv6 changes)"]
    CM --> HB["heartbeat POST to GCP VM\nover WireGuard (10s)"]
```

### 13. Moderation & Verification Pipeline

```mermaid
graph TD
    In["User / agent / MCP input"] --> L1{"L1 pattern guard\n(server/input_guard.py, patterns from\nprompts/surface_attacks/, Fernet-optional)"}
    L1 -- "is_jailbreak_attempt /\nis_harmful_request" --> Block1["refuse (MCP gateway pre-batch;\nguardrail lane)"]
    L1 -- pass --> Gen["generation (lanes as above)"]
    Gen --> L3{"L3 output judge (orchestration._finalize_task)"}
    L3 --> Strict["is_strict_output_blocked reply"]
    L3 --> Judge["features/judge.mcp_output_judge\n(guardrail :8083, per-user judge model)"]
    Strict & Judge --> Lane{"lane?"}
    Lane -- "MCP/guardrail" --> FC["fail-closed: mark failed, drop output"]
    Lane -- "UI" --> FO["fail-open: deliver + record note"]
    Gen -->|"research answers"| Critic["features/critic.py:\neach (Author, Venue, Year) [url] citation\nre-searched + re-fetched, LLM-checked;\n<70/100 quality or missing cites →\nre-schedule ≤ 2× (judge prompts)"]
```
