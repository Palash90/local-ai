# Local AI — Architecture

Self-hosted LLM + image generation stack on a single laptop (RTX 3050, 4 GB VRAM, 16 GB RAM).

## Quick Start

```bash
# 1. Clone and build
git clone <this-repo> ~/git/local-ai
cd ~/git/local-ai
bash setup.sh

# 2. Edit config files (required before first run)
nano ~/local-ai-files/model.txt        # set your LLM model name
nano ~/local-ai-files/models.json      # set ComfyUI model filenames
nano ~/local-ai-files/users.json       # set passwords

# 3. Download models into:
#    LLMs:     ~/local-ai-files/my-models/
#    ComfyUI:  ~/local-ai/ComfyUI/models/{checkpoints,clip,vae,unet,...}

# 4. Start services (in order)
~/local-ai/llama.cpp/build/bin/llama-server \
    --host 0.0.0.0 --port 8081 \
    --models-dir ~/local-ai-files/my-models/ \
    --n-gpu-layers 99 --no-kv-offload --ctx-size 32768 \
    --reasoning-budget 1120

cd ~/local-ai/ComfyUI && source venv/bin/activate && python main.py \
    --lowvram \
    --input-directory ~/local-ai-files/ComfyUI/input \
    --output-directory ~/local-ai-files/ComfyUI/output

cd ~/git/local-ai && python chat-webui.py
```

Access at `http://chat.local` or `http://localhost:3001`.

## 1. Infrastructure & Network

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
        Docker --> SearXNG["SearXNG Container\nlocalhost:8080\nRestart: unless-stopped"]
        Nginx["Nginx Reverse Proxy\nchat.local:80 → localhost:3001"]
        Avahi["avahi-daemon\nmDNS: chat.local"]
    end

    subgraph Network ["Network Topology"]
        LAN["LAN Devices"] -->|"http://chat.local"| Nginx
        Nginx -->|"proxy_pass\nUpgrade + X-Real-IP"| HTTPServer["chat-webui.py\n0.0.0.0:3001"]
        HTTPServer -->|"localhost:8081"| LLamaServer["llama-server"]
        HTTPServer -->|"localhost:8188"| ComfyUIRuntime["ComfyUI"]
        HTTPServer -->|"localhost:8080/search"| SearXNG
        HTTPServer -->|"nominatim.openstreetmap.org"| Nominatim["Reverse Geocoding"]
    end

    subgraph StartupOrder ["Startup Order (manual)"]
        SO1["1. llama-server"] --> SO2["2. ComfyUI\nvenv → python main.py --lowvram"]
        SO2 --> SO3["3. chat-webui.py\npython chat-webui.py"]
        SO4["chat-webui.py auto-checks\nllama-server /health on boot\n→ restart_servers if dead"]
    end
```

## 2. File Layout & Build

```mermaid
graph TD
    subgraph CodeRepo ["Code (~/local-ai/)"]
        CR1["chat-webui.py\nMain server — Python 3"]
        CR2["setup.sh\nBootstrap: installs deps,\nclones repos, creates templates"]
        CR3["dist/\nVite-built SPA frontend"]
        CR4["llama.cpp/ git clone\nBuilt: cmake -DGGML_CUDA=ON\nBinary: build/bin/llama-server"]
        CR5["ComfyUI/ git clone\nvenv: ComfyUI/venv/\nRequires: requirements.txt"]
    end

    subgraph DataDir ["Data (~/local-ai-files/)"]
        DF1["model.txt\nLLM model name"]
        DF2["models.json\nComfyUI image model defs\nz_image, sd3_5_medium"]
        DF3["users.json\nUser credentials + context paths"]
        DF4["sys_prompt.txt\nSystem prompt template\n%model_list% %current_time%\n%current_location%"]
        DF5["sessions.json\nPersisted chat sessions"]
        DF6["my-models/\nLLM GGUF model files"]
        DF7["ComfyUI/input/\nTemp files for image editing"]
        DF8["ComfyUI/output/\nGenerated/edited images"]
        DF9["contexts/\nPer-user persistent context"]
        DF10["searxng/\nSearXNG config volume"]
    end

    subgraph BuildFlags ["Build Flags"]
        BF1["llama.cpp\ncmake -DGGML_CUDA=ON\n-DCMAKE_BUILD_TYPE=Release\n-j nproc"]
        BF2["ComfyUI\npip install requirements.txt\nin Python venv"]
        BF3["Frontend\nnpm install && npm run build\nVite to dist/"]
        BF4["System deps\ngit python3 cmake avahi-daemon\npdftotext catdoc antiword\nnginx docker.io"]
    end
```

## 3. Runtime Constants & Locks

```mermaid
graph TD
    subgraph RCNetwork ["Service URLs"]
        RC1["LLAMA_BASE = localhost:8081\nLLAMA_URL = /v1/chat/completions"]
        RC2["COMFYUI_URL = localhost:8188"]
        RC3["SEARXNG_URL = localhost:8080/search"]
        RC4["HOST = 0.0.0.0  PORT = 3001"]
    end

    subgraph RCLlama ["llama-server Args"]
        RCL1["--host 0.0.0.0 --port 8081"]
        RCL2["--models-dir ~/local-ai-files/my-models/"]
        RCL3["--n-gpu-layers 99"]
        RCL4["--no-kv-offload"]
        RCL5["--ctx-size 32768"]
        RCL6["--reasoning-budget 1120"]
        RCL7["Qwen: --cache-type-k q8_0 --cache-type-v q8_0"]
        RCL8["LLAMA_QWEN_NGL=12 LLAMA_GEMMA_NGL=99"]
    end

    subgraph RCThermal ["Thermal and RAM Thresholds"]
        RT1["TEMP_THRESHOLD_ON = 85 C"]
        RT2["TEMP_THRESHOLD_OFF = 65 C"]
        RT3["RAM_EVAC_THRESHOLD = 95%"]
        RT4["RAM_RESUME_THRESHOLD = 70%"]
    end

    subgraph RCLimits ["Limits and Pools"]
        RL1["MAX_QUEUE_SIZE = 5"]
        RL2["MAX_INPUT_TOKENS = 4096"]
        RL3["_llm_pool = 1 worker"]
        RL4["_tool_pool = 2 workers"]
        RL5["Max tool rounds = 10"]
        RL6["Idle unload = 300s"]
        RL7["LLM timeout = 600s"]
        RL8["ComfyUI poll = 120s"]
        RL9["REASONING_BUDGET = 1120"]
    end

    subgraph RCThreads ["Thread Pools and Locks"]
        LK1["_llm_pool: ThreadPoolExecutor 1\nSingle LLM call at a time"]
        LK2["_tool_pool: ThreadPoolExecutor 2\n2 concurrent tool executions"]
        LK3["_event_queue: queue.Queue\nDecouples dequeue from dispatch"]
        LK4["_data_lock: threading.Lock\nGuards sessions, tasks, model_status"]
        LK5["_model_transition_lock\nSerializes load/unload of LLM"]
        LK6["_tokens_lock\nGuards _active_tokens dict"]
        LK7["_queue_lock + _queue_cond\nTask queue + Condition variable"]
    end
```

## 4. Server Startup

```mermaid
graph TD
    A["python chat-webui.py"] --> A1["Load configs at import:\nmodel.txt, models.json,\nsys_prompt.txt, users.json"]
    A --> B["load_sessions()\nLoad sessions.json"]
    A1 & B --> C{"llama-server /health\nHTTP GET?"}
    C -- "200 OK" --> E["Start 4 Daemon Threads"]
    C -- "Dead" --> D["restart_servers:\n1. kill_llama_server pkill -9\n2. kill_comfyui pkill main.py\n3. Spawn llama-server Popen\n4. Spawn ComfyUI Popen\n5. Poll /health 2s up to 120s\n6. Kill [...]"]
    D --> E

    subgraph Daemons ["Background Daemon Threads"]
        E1["_event_loop\nSingle-threaded event dispatcher"]
        E2["_queue_worker\nSequential task dequeuer"]
        E3["_idle_unload_loop\nPolls every 10s"]
        E4["_thermal_monitor\nPolls every 10s"]
    end
    E --> E1 & E2 & E3 & E4
    E --> F["HTTPServer.serve_forever\n0.0.0.0:3001"]
```

## 5. Model State Machine

```mermaid
stateDiagram-v2
    [*] --> unloaded
    unloaded --> loading : load_llama_model
    loading --> chat_loaded : 200 from /models/load\n+ health check passes
    loading --> unloaded : failed
    chat_loaded --> unloading : unload_llama_model
    unloading --> unloaded : 200 from /models/unload
    unloading --> chat_loaded : failed but health OK
    chat_loaded --> image_active : generate_image\nor edit_image starts
    image_active --> chat_loaded : free_comfyui_vram\n+ load_llama_model
```

## 6. REST API Endpoints

```mermaid
graph TD
    Client([User Client])

    subgraph AuthEndpoints ["Auth"]
        Client -->|"POST /api/login\n{username, password}"| Login["Validate against users.json\nIssue UUID token to _active_tokens"]
        Client -->|"POST /api/logout\nX-Auth-Token"| Logout["Remove token"]
        Client -->|"GET /api/check-auth"| CheckAuth{"Token valid?"}
        CheckAuth -- Yes --> AuthOK["{authenticated: true, username}"]
        CheckAuth -- No --> AuthNO["{authenticated: false}"]
    end

    subgraph SessionEndpoints ["Session Management"]
        Client -->|"POST /api/sessions"| NewSession["Create UUID session\nStore in sessions_meta"]
        Client -->|"GET /api/sessions"| ListSessions["List user sessions\nSorted by updated desc"]
        Client -->|"GET /api/sessions/:id/messages"| GetMessages["Return messages\n+ token_estimate"]
        Client -->|"PUT /api/sessions/:id"| RenameSession["Rename session"]
        Client -->|"DELETE /api/sessions/:id"| DeleteSession["Delete session + cleanup\nassociated output images"]
    end

    subgraph UtilityEndpoints ["Utility"]
        Client -->|"GET /api/model-status"| ModelStatus["model_status, _last_tps\n_overheated, _gpu_temp"]
        Client -->|"POST /api/extract-file"| ExtractFile["PDF/DOCX/DOC/XLSX to text\nvia fitz/catdoc/antiword/openpyxl"]
        Client -->|"POST /api/location"| SetLocation["Reverse geocode via Nominatim\nstore _client_location"]
        Client -->|"GET /api/user-context"| GetUserCtx["Read user context file"]
        Client -->|"POST /api/user-context"| SetUserCtx["write / overwrite / read"]
        Client -->|"GET /output/:filename"| ServeImage["Serve generated images\nfrom ComfyUI output dir"]
    end

    subgraph SPA ["Static / SPA Serving"]
        Client -->|"GET /"| SPAIndex["Serve dist/index.html"]
        Client -->|"GET /*"| SPAAssets["Serve dist/ assets\nor SPA fallback to index.html"]
    end

    subgraph StatusPolling ["Status Polling"]
        Client -->|"GET /api/status/:task_id"| PollStatus["Return tasks id:\nstatus message response\n tools_used image etc"]
    end
```

## 7. Chat Ingress Flow

```mermaid
graph TD
    Client([User Client]) -->|"POST /api/chat\nX-Auth-Token"| EndpointChat

    EndpointChat["Handler.do_POST: /api/chat"]
    EndpointChat --> AuthCheck{"get_current_user\nvia X-Auth-Token"}
    AuthCheck -- No --> AuthErr[401 Unauthorized]
    AuthCheck -- Yes --> SessionCheck{"Session exists\nand owned by user?"}
    SessionCheck -- No --> SessionErr[404 Session not found]
    SessionCheck -- Yes --> TempCheck{"_overheated?"}
    TempCheck -- Yes --> ThermalErr["503 Server overloaded\nmessage queued on cooldown"]
    TempCheck -- No --> QueueCheck{"len task_queue\n< MAX_QUEUE_SIZE 5?"}
    QueueCheck -- No --> QueueBusy[503 Server Busy]

    QueueCheck -- Yes --> EnqueueTask["task_queue.append\nqueue_cond.notify"]
    EnqueueTask --> TaskInit["tasks task_id =\nstatus queued"]
    TaskInit --> ReturnTaskID["Return task_id to Client"]
```

## 8. Queue Worker

```mermaid
graph TD
    E2["_queue_worker"] --> QueueLoop["queue_cond.wait\nblock on empty queue"]
    QueueLoop --> SafetyCheck{"overheated\nor ram_evacuating?"}
    SafetyCheck -- Yes --> MarkWaiting["Set all queued tasks to\nstatus waiting\npause label"]
    MarkWaiting --> PauseWait["queue_cond.wait 5s"] --> QueueLoop
    SafetyCheck -- No --> PopTask["item = task_queue.pop 0\ncurrent_task_id = item.task_id"]
    PopTask --> PostStart["event_post start\nsession_id message image\naudio user client_timestamp"]
    PostStart --> TaskDoneWait{"Poll tasks id.status\nevery 0.5s"}
    TaskDoneWait -- "done or error" --> ClearTask["current_task_id = None\nqueue_cond.notify_all"]
    ClearTask --> QueueLoop
```

## 9. Event Loop Pipeline

```mermaid
graph TD
    E1["_event_loop"] --> EvLoop["Loop: event_queue.get\nev_type task_id data"]
    EvLoop --> EvDispatch{"ev_type?"}

    EvDispatch -- "start" --> EvStart["Store task metadata:\n_tools_used, _search_details\n_original_message, _original_image\n_audio, _user, _client_timestamp"]
    EvStart --> PrepSession["prepare_session:\n1. Inject sys prompt + date + location\n2. Inject user context\n3. Append user msg to session\n4. Auto-name session from message\n5. save_sessions\n[...]" ]
    PrepSession --> StartRound0["start_llm_round round 0"]

    EvDispatch -- "llm_ok" --> LLMOK{"state == llm_waiting\nand has tool_calls?"}
    LLMOK -- "No tools" --> Finalize["_finalize_task:\n1. Build msg_entry with reasoning\n   tools_used, image_url etc\n2. Append to session\n3. save_sessions\n4. tasks id = status done\n5. Reset[...]"]
    LLMOK -- "Has tools" --> SubmitTools["1. Append assistant msg\n2. state = tools_running\n3. pending_tools = count\n4. save_sessions\n5. Submit to tool_pool"]

    EvDispatch -- "llm_err" --> LLMERR{"state == llm_waiting?"}
    LLMERR -- Yes --> LLMErrAction["_set_task_error:\ntasks id = status error"]
    LLMERR -- No --> EvLoop

    EvDispatch -- "tool_ok" --> ToolOK["1. Append tool result to session\n2. pending_tools minus 1\n3. save_sessions"]
    ToolOK --> AllToolsDone{"pending_tools <= 0?"}
    AllToolsDone -- No --> EvLoop
    AllToolsDone -- Yes --> NextRoundCheck{"round+1 < 10?"}
    NextRoundCheck -- Yes --> NextRound["start_llm_round round N+1\nFeed tool results back to LLM"]
    NextRoundCheck -- No --> MaxRoundsErr["_set_task_error:\nMax tool rounds exceeded"]

    EvDispatch -- "tool_err" --> ToolERR["1. Append error as tool result\n2. pending_tools minus 1\n3. save_sessions\n4. Same round-limit logic"]
```

## 10. LLM Worker

```mermaid
graph TD
    StartRound0["start_llm_round"] --> LLMWorker["_llm_worker\nin _llm_pool 1 worker"]
    LLMWorker --> PayloadBuild["Build payload:\nmodel messages tools\ntool_choice auto\nmax_tokens 4096\nreasoning_budget 1120\nstream true"]
    PayloadBuild --> StreamReq["POST llama-server\nv1/chat/completions\nstream=True timeout=600s"]
    StreamReq --> StreamParse["Parse SSE stream:\n- reasoning_content delta\n  accumulate in reasoning_buf\n- content delta\n  accumulate in content_buf\n- tool_calls delta\n  reassemble by index[...]" ]
    StreamParse --> BuildAssistantMsg["Build assistant msg:\nrole assistant content\nreasoning_content tool_calls"]
    BuildAssistantMsg --> LLMOKPost["event_post llm_ok\nbody choices message"]
    LLMOKPost --> EvLoop["Back to _event_loop"]

    StreamReq -. "exception" .-> LLMException["event_post llm_err\nif image or vision in error\nuser-friendly message"]
```

## 11. Tool Worker

```mermaid
graph TD
    SubmitTools["Submit to _tool_pool"] --> ToolWorker["_tool_worker\nin _tool_pool 2 workers"]
    ToolWorker --> ParseArgs["Parse tc.function.arguments\nfrom JSON string"]
    ParseArgs --> ChooseTool{"tc.function.name?"}

    ChooseTool -- "web_search" --> ExecSearch["1. set_status Searching\n2. Get _client_timestamp\n3. web_search query client_ts:\n   Append city to query\n   GET SearXNG search json\n   Return to[...]" ]
    ExecSearch --> ToolPost["event_post tool_ok"]

    ChooseTool -- "generate_image" --> GenGuard{"already generated\nimage this task?"}
    GenGuard -- Yes --> GenReject["Return error:\nImage generation limit reached"]
    GenGuard -- No --> GenImage["1. unload_llama_model\n2. Build ComfyUI workflow:\n   z_image 8 steps res_multistep\n   or sd3_5_medium 20 steps euler\n3. ensure_comfyui_running\n4. POST /prompt[...]" ]
    GenImage --> ToolPost

    ChooseTool -- "edit_image" --> EditImage["1. Find source image:\n   Check _image_url in session\n   Check base64 in user messages\n2. unload_llama_model\n3. Write input to ComfyUI/input\n4. e[...]" ]
    EditImage --> ToolPost

    ChooseTool -- "update_user_context" --> ExecContext["write_user_context:\nAppend timestamped entry\nto user context file"]
    ExecContext --> ToolPost

    ChooseTool -- "unknown" --> ToolUnknown["Return error:\nUnknown tool"]
    ToolUnknown --> ToolPost
```

## 12. Resource Management

```mermaid
graph TD
    E4["_thermal_monitor"] --> ThermalLoop["Loop every 10s"]
    ThermalLoop --> CheckGPU["nvidia-smi GPU temp"]
    CheckGPU --> GPUTempCheck{"Temp >= 85 C?"}
    GPUTempCheck -- Yes --> SetOverheat["_overheated = True"]
    GPUTempCheck -- No --> CheckCool{"_overheated\nand Temp <= 65 C?"}
    CheckCool -- Yes --> UnsetOverheat["_overheated = False"]
    SetOverheat --> ThermalAction{"Is task running?"}
    ThermalAction -- No --> ThermalUnload["model_status?"]
    ThermalUnload -- "chat_loaded" --> UnloadModel["unload_llama_model"]
    ThermalUnload -- "image_active" --> FreeVRAM["free_comfyui_vram"]
    ThermalAction -- Yes --> ThermalSkip["Skip let task finish"]
    UnsetOverheat --> RAMCheck1

    ThermalLoop --> RAMCheck1{"not evacuating\nand RAM >= 95%?"}
    RAMCheck1 -- Yes --> EvacuateRAM["_evacuate_ram:\n1. ram_evacuating = True\n2. Requeue current task to front\n3. Set task status error\n4. kill_llama_server\n5. kill_comfyui\n6. Wait until RA[...]" ]
    RAMCheck1 -- No --> ThermalLoop

    E3["_idle_unload_loop"] --> IdleLoop["Loop every 10s"]
    IdleLoop --> IdleCheck{"chat_loaded\nidle > 300s\nno queue tasks?"}
    IdleCheck -- Yes --> UnloadModel2["unload_llama_model\nRelease VRAM weights"]
    IdleCheck -- No --> IdleLoop
```
