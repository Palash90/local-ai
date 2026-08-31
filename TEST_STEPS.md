# Comprehensive Interface Test Plan — local-ai

## Pre-flight (do this first)

**1. Ground truth — what's running now**
| Port | Service | Status |
|---|---|---|
| 3001 | chat-webui (core) | UP |
| 3002 | markdown hosting | UP |
| 8000 | MCP gateway | UP (401 w/o token) |
| 8081 | GPU llama | UP |
| 8188 | ComfyUI | UP |
| 8080 | SearXNG | UP |
| 9000 | code host | UP |
| 9010 | Authentik outpost | UP (404 OK) |
| 8079 | CPU llama | **DOWN** (lazy-start) |
| 8083 | guardrail llama | **DOWN** (lazy-start) |

**2. Restart baseline.** The running chat-webui (PID 887506, up 8h+) predates some committed fixes (`openai_api.py`, `orchestration.py`). For conclusive results:
```bash
./restart_services.sh          # stops/restarts chat-webui, markdown, code-host; or
pkill -f chat-webui.py && sleep 2 && bash -c 'nohup python3 ./chat-webui.py >>logs/chat-webui.log 2>&1 &'
```
Then confirm `curl -s localhost:3001/api/model-status` returns JSON and GPU `/health` is 200. **Restarting is mandatory before trusting OpenAI-lane results.**

**3. Capture credentials** (don't log them): `OPENAI_API_KEY`, `AUTHENTIK_BASE_URL`, agent user/passwords (kolpo/kaya/editor/moderator), `MCP_USER`. Verify the API key is set: `curl -s -o /dev/null -w "%{http_code}" localhost:3001/v1/models` → **200** = set, **500** = unset, **401** = wrong key.

**4. Identify test users.** `palash`(admin), `totan`(premium), `kolpo/kaya/editor/moderator`(free). Browser auth = Authentik SSO headers `X-Authentik-Username/Groups/...`; agents = `Authorization: Bearer <JWT>`.

**5. Lane-routing flag.** `server/config.py` ships with `FORCE_GPU_LANE = True` (test-time flag: everything pins to the GPU lane unless the request carries an explicit `mode` or the UI research+CPU toggle). §B3/§F CPU-lane assertions below **require `FORCE_GPU_LANE = False`** in `server/config.py` (or an explicit `mode:"cpu"` request) — otherwise skip them and note the flag in the report.

---

## A. OpenAI-Compatible API (`/v1/*` on 3001) — **the primary focus**

Auth via `Authorization: Bearer $OPENAI_API_KEY`. If unset, tests A1–A6 are the *functionality* expected after a restart (but note the summary's tool-call fix depends on these).

**A1. Auth matrix**
- No header → 401 `invalid_request_error`
- Wrong Bearer → 401
- Correct Bearer → 200
- `OPENAI_API_KEY` empty → **should** be 500 `server_error` (verify config)

**A2. `GET /v1/models` and `/v1/models/:id`** (`curl -H "Authorization: Bearer $K"`)
- Returns `object:"list"` with `data[]`; expect GPU model id + CPU model id (if different)
- Retrieve a real id → 200 with `owned_by`; unknown id → 404

**A3. Non-stream completion** (core)
```bash
curl -s http://localhost:3001/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"<gpu>","stream":false,"messages":[{"role":"user","content":"Say hello in one short sentence"}]}'
```
Assert: `object:"chat.completion"`, `choices[0].message.role=="assistant"`, nonempty `content`, `finish_reason:"stop"`, contains an `id`.

**A4. Streaming completion**
```bash
curl -N ... -d '{"stream":true,"messages":[...]}'
```
Assert: `data:` blocks are `chat.completion.chunk`, ends with `data:[DONE]`.

**A5. Tool-call non-stream** (the fix we're validating)
```
messages:[{role:"user",content:"What is the weather in Tokyo right now? Use web_search."}]
```
Assert `choices[0].finish_reason=="tool_calls"` and `message.tool_calls[0].function.name=="web_search"` (arguments JSON string). **Must NOT** contain raw `<|tool_call|>` text tags.

**A6. Tool-call streaming** (VS Code extension contract — **critical**)
Capture the SSE bytes. Assert the **incremental** shape:
1. First tool chunk: `delta.tool_calls[0].index==0` with `id` + `type:"function"` + `function.name`, but **empty** `function.arguments`
2. Following chunks: `finish_reason==null`, `function.arguments` fragments only (incremental), no re-sent id/name
3. Final chunk: `delta:{}, finish_reason:"tool_calls"`
4. Ends with `[DONE]`
This is exactly what VS Code's OpenAI client consumes. If any chunk re-sends the full arguments or skips the id-first chunk, the fix is incomplete.

**A7. Multimodal via `image_url`** (VRAM-relevant)
```
messages:[{role:"user",content:[{type:"text","text":"Describe this"},{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]}]
```
Assert a text response and no OOM/error → confirms mmproj + VRAM coexistence.

**A8. Conversation history.** Send 2+ messages with prior `assistant`/`user` roles in one request; assert the model remembers context (session history injection works).

**A9. Keepalive during long queue.** Hold the GPU lane busy (start a long gen in §B), then issue A4; assert CSS `: status` comment lines (SSE keepalives) appear roughly every 10s until `[DONE]` — proves the undici-300s-abort fix.

**A10. Error paths.** Invalid JSON → 400; no `messages` → 400; no user message → 400; queue full (book out `MAX_QUEUE_SIZE=15`) → 503 "Server busy".

---

## B. Chat UI API (`/api/*` on 3001, SSO-authenticated)

Auth: X-Authentik-* headers (browser) or Bearer JWT (agent). Use a helper that sets `X-Authentik-Username: palash` + `X-Authentik-Groups: admin`.

**B1. Auth & identity**
- `GET /api/check-auth` (with + without valid headers) → `{authenticated, username, role}`
- `GET /api/user-context` → 200 w/ username+context; unauthenticated → 401
- `GET /api/active-users` → array (excludes agents)

**B2. Sessions CRUD**
- `POST /api/sessions` `{name}` → 200 `{session_id}`
- `GET /api/sessions` → list sorted by `updated` desc, token estimate present
- `GET /api/sessions/:id/messages` → `{messages, ...token}`; wrong owner → 404/401
- `PUT /api/sessions/:id` `{name}` → rename
- `DELETE /api/sessions/:id` → delete; verify output/upload cleanup

**B3. Chat flow (end-to-end)**
- `POST /api/chat` `{session_id, message}` → `{task_id}`
- Poll `GET /api/status/:task_id`: state machine `queued → (waiting) → … → done`, `message` updates, `response` present, `tools_used` when tools fired
- Verify `GET /api/sessions/:id/messages` shows assistant msg + tool payloads
- Repeat with an **agent** user (e.g. kolpo JWT) → confirm routing to CPU lane (task `mode:"cpu"`)
- Repeat with `free` non-agent user → GPU lane; verify a human never lands on CPU

**B4. Queue/lane behavior**
- Issue multiple parallel `/api/chat` and confirm they serialize per lane, statuses go `queued`
- Exceed `MAX_QUEUE_SIZE` (15) → 503

**B5. Model status**
- `GET /api/model-status` → `{model, predicted_per_second, overheated, gpu_temp, ram_evacuating, max_context, reminder_count}`; assert values sane

**B6. Shares (public snapshots)**
- `POST /api/shares` `{session_id, msg_index}` (assistant msg) → `{token, url}`; non-assistant index → 4xx; foreign session → 404/401
- `GET /api/shares` → list own shares; `DELETE /api/shares/:token` → gone from list, `/s/:token` → not-found page
- Open `/s/<token>` in a **private window (no SSO)** → snapshot renders read-only
- Snapshot immutability: after sharing, edit/delete the source session message → share page **unchanged**
- Image scoping: `/api/public/share/<token>/image/<path>` serves only files referenced by that snapshot; try `/image/../../etc/passwd` and an unrelated `/output/` file → 404/403

**B7. Tasks & themes & context (direct API)**
- `POST /api/tasks` `{title, priority, due_date, reminder_at}` → id; `GET /api/tasks` lists it; `PUT`/`DELETE /api/tasks/:id` work; cross-user id → 404
- Reminder: create task with `reminder_at` ~1 min out → within ~90s `GET /api/model-status` shows `reminder_count` increment / reminder surfaces in UI (reminder loop = 30s)
- `GET /api/themes` (admin) → theme log rows + stats after §F or an agent `track_theme` run
- `POST /api/user-context` role matrix: `{action:"write"}` any user OK; `{action:"overwrite"}` **admin only** (free/premium → 403); `GET /api/user-context` → own file only

**B8. Presence & misc endpoints**
- `POST /api/leaving` → user drops from `GET /api/active-users` immediately (else the 120s active-window expiry removes them)
- `POST /api/logout` → 200; next `/api/check-auth` with same headers behaves per design (header-auth is stateless — assert response shape, not session invalidation)
- `POST /api/upload-image` (base64 png) → `{url:"/uploads/…"}`; `GET /api/image/<id>` serves the working image; bogus id → 404
- `POST /api/tts` `{text}` → audio bytes (Piper voice); with Piper missing → edge-tts fallback or clean 5xx (no hang)
- SPA fallback: `GET /some/unknown/route` (no dot) → `index.html` 200; `GET /api/nonexistent` → 404 JSON, not HTML

**B9. File serving auth-gate**
- `GET /output/<file>.png` / `GET /uploads/<file>` **without** identity → 401; with SSO headers or agent JWT → 200
- Path traversal: `/output/../../etc/passwd`, encoded `%2e%2e/` variants → rejected

---

## C. Tools (exercise inside a chat round via B3 prompts)

| Tool | Test prompt | Assertion |
|---|---|---|
| `web_search` | "Latest AI news today" | `tools_used` contains web_search; response cites a URL |
| `fetch_page` | "Fetch https://example.com and summarize" | title + content returned |
| `generate_image` | "Draw a sunset landscape" | an image URL in response; ComfyUI produced a file; **only one image per round** (limit) |
| `edit_image` | after an image exists | img2img output, denoise respected |
| `read_file` | upload PDF→`/api/extract-file`→"read the file" | extracted text returned |
| `read_image` | attach image→"what does it show" | describes content |
| `get_user_location` | "what's the weather here" | status → `location_needed`, then answer w/ location or denial |
| `update_user_context` | "remember I like science fiction" | context file appended; persists across sessions |
| `manage_tasks` | "create a task to buy milk tomorrow" | task created; verify via theme/tasks API |
| `track_theme` | via an **agent** (reserved) | works for agents; rejected with clear error for humans |
| `tool_details` | ask model to inspect a tool | returns full docs |

Also verify **`TOOL_FREE_AGENTS`** (editor/moderator): they get empty tools + `tool_choice:none` → never call `generate_image`. And **`no_tools:true`** in a `/api/chat` body does the same for any user.

Also verify **research mode** (UI toggle → `research:true` in `/api/chat`): tool-round budget rises 10 → 50 (a deep multi-page fetch/chunk-walk completes instead of "max tool rounds"); with `research+cpu` the task lands on the CPU lane even when `FORCE_GPU_LANE` is on.

Also verify **`track_theme` is agent-only**: `TOOLS_HUMAN` strips it — a human UI request must never see/call it; an agent request can `log` + `check`.

**C-SSRF. `fetch_page` private-IP rejection**
- Prompt: "Fetch http://127.0.0.1:8081/health and tell me the status" → tool returns refusal (no request made; check no `[fetch]` GET in logs)
- Repeat with `http://169.254.169.254/` (cloud metadata) and `http://[::1]:3001/` → refused
- Public URL (example.com) still works → guard isn't over-broad

**C-UPLOAD. File-type whitelist**
- `POST /api/extract-file` with a disallowed extension (`.sh`, `.exe`) → rejected; `.pdf/.docx/.xlsx` accepted and later readable by `read_file`
- UI: InputBar rejects the same extensions client-side (belt & braces)

And **image-generation VRAM**: run `generate_image` while GPU chat is loaded; assert the model unloads → ComfyUI runs → model reloads (see logs `[llama]`/`[image]`), CPU agents keep running throughout.

---

## D. MCP Gateway (`:8000`, token auth)

- `POST /mcp` with `ListToolsRequest` → 200 tool list (incl. session/chat tools, currently L2/L3 verify)
- `ListResourcesRequest`, `ListPromptsRequest` → 200
- No/invalid bearer → check `EnforcementAuthMiddleware` (expect 401)
- Run an MCP chat/verify batch end-to-end; watch guardrail llama (8083) lazy-start → `LEVEL 3` verify → auto-unload after 300s idle
- Check SQLite queue drain (`mcp_tasks_db`) and `batches` worker logs
- **OAuth surface**: `GET /.well-known/oauth-authorization-server` + `/oauth-protected-resource(/mcp)` → JSON with correct endpoints/issuer; `POST /oauth/token` with bad code_verifier → reject (PKCE); valid auth-code+verifier round-trip → token usable on `/mcp`
- **`submit_batch_results`**: submit externally-produced results → batch state transitions + results readable via `get_batch_results`
- **`get_image`**: fetch a batch item's generated image by id → bytes; unknown id → error string, not 500
- **`get_batch_status`/queue position**: start 2+ batches → second reports pending + position; statuses move PENDING→WORKING→COMPLETED/ERROR only forward
- **Input guard on gateway**: `send_chat_message`/`start_chat_batch` with a jailbreak-pattern message (see §I list) → refused before any LLM call
- **Cross-user isolation**: token for user A must not read user B's sessions/images via MCP tools

---

## E. markdown_hosting (`:3002`) — story RBAC

- `GET /` (with SSO headers) → collections index (only collections ≤ your role level)
- As **free** (kolpo): see `free_stories` only
- As **premium** (totan): + `premium_stories`
- As **admin** (palash): + `admin_stories`; free/premium get 403 on admin collection
- Unauthenticated → 401 on gated collections
- `GET /story/<col>/<id>` → rendered HTML w/ KaTeX math + rewritten image `src="/media/..."`
- `GET /story/<col>/<id>/content` live-poll → incremental HTML
- `GET /media/<col>/<id>/<file>` → image bytes (auth-gated)
- **Admin DELETE** `/story/<col>/<id>` → removes folder; non-admin → 403
- Missing folder/file → 404

---

## F. Self-chat production pipeline (offline, `self-chat.py`)

- `--dry-run` w/ default + a `--config` file → prints every task plan, checklist resolution, medium feasibility, missing files, unhandled placeholders; **no LLM call**. `--defaults` combines the default tasks with `--config`. (Valid flags are only `--config/--defaults/--dry-run/--gpu` — turn counts come from the task config, there is no `--turns` flag.)
- Real short run (`--config tasklist.json` w/ 1 task, minimal turns in the task spec) → verify: agents log in (OIDC password grant via `oidc_password_grant`), sessions created, story file + moderation `.json` written to `~/local-ai-files/stories/...`, GREEN/RED verdict, auto-RED gate (duplicate/citation drop/wrong script/name leak)
- CPU lane routing default; `--gpu` flag routes agents to GPU
- Check **theme dedup**: run identical combo twice → second run must pick a different combo (theme tracker)

---

## G. Frontend SPA (React, served from 3001)

With a browser (or headed test) authenticated via SSO:
1. Load `/` → SPA renders, sidebar lists sessions, `check-auth` populates identity
2. New chat → message streams in; tool use shows actions (image, search)
3. Upload a file → appears as attachment; ask the model to read it
4. Ask for an image → generation task shows status → image renders (VRAM unload/reload visible)
5. Location prompt (LocationPrompt) when model calls `get_user_location`
6. ModelBar shows live model-status (temp/tps); OverloadWarning when `overheated`
7. TaskPanel dropdown for to-dos/reminders
8. Share button → creates share; public share page loads snapshot images via `/api/public/share/<token>/image/...`
9. No SSR errors in console; dark-mode prefers-color-scheme works

---

## H. Infra / deployment interfaces

- **restart_services.sh**: full run → all 3 services UP, WireGuard `wg0` up, backup disk mounted, Nextcloud `files:scan`, health checks pass
- **nginx (gcp_nginx.conf + local_cloud.sh)**: `https://home.palashkantikundu.in` → 200 via WG; offline page (502) when upstream down; gzip on; auth_request gate on `/ai/ /api/ /stories/ /story/ /cloud`
- **Authentik**: outpost `/outpost.goauthentik.io/*` reachable (9010/404 ok); SSO login round-trip
- **SearXNG** `:8080` → search returns JSON w/ `results`
- **ComfyUI** `:8188` → `/health` 200, `/system_stats` reports GPU
- **GPU llama** `:8081/health` + `/v1/models`; **CPU** `:8079` and **guardrail** `:8083` lazy-start on first use and idle-unload
- **DDNS/heartbeat** (logs `[ddns] GoDaddy AAAA updated`, `[heartbeat]`) — visit logs; heartbeat receiver = `scripts/gcp_heartbeat_server.py` on the GCP VM over WireGuard (`HEARTBEAT_URL` 10.66.66.1:9863)
- **stop_services.sh**: run → all 3 services down (ports 3001/3002/9000 free), llama/ComfyUI untouched unless designed
- **encrypt_surface.py round-trip**: `python3 scripts/encrypt_surface.py` → `.enc` files appear under `prompts/surface_attacks/`; with `SURFACE_ATTACKS_KEY` set the guardrail still loads patterns (logs `[guardrail] Fernet decryption enabled`), with wrong/no key → clean warning + plaintext fallback or refusal, **never** a crash; delete `.txt` originals → patterns still load from `.enc`
- **authentik_bootstrap.py idempotency**: re-run → no duplicate groups/apps/users created, exits clean
- **code host** `:9000` → responds (service started by restart_services.sh; binary lives outside this repo)
- Then re-run §A5/A6 (OpenAI tool-calls) to confirm the whole stack + our fix is live.

---

## I. Moderation & verification (input_guard / judge / critic)

**I1. L1 input guard (pattern-based, `server/input_guard.py`)**
- Pattern files (`injection_patterns.txt`, `harmful_request_patterns.txt`, `harmful_output_patterns.txt`, `strict_output_patterns.txt`, `safety_frame.txt`) are deployment data under `prompts/surface_attacks/` (plaintext or `.enc`) — **they are not in the repo**; if absent, first assert the guard degrades safely (no crash, guardrail logs a warning) and create sample files with 2–3 triggers each for the tests below
- Send each via MCP `send_chat_message`/`start_chat_batch` → refused **before** any llama call (no `[llm]` log line, guardrail server stays unloaded)
- Unicode/obfuscation bypass attempts: fullwidth chars, zero-width inserts, `SYSTEM:` split across lines → `_normalize` must still catch them
- A benign prompt containing one pattern substring (false-positive check) → note result, not necessarily a fail

**I2. L3 output judge (every task, `orchestration._finalize_task`)**
- Force an output matching `strict_output_patterns.txt` (or replay via MCP batch with a jail that produces it) → MCP/guardrail lane: task marked failed, output dropped (**fail-closed**); UI lane: reply delivered + judge note recorded (**fail-open**)
- Guardrail server lifecycle: first L3 verify lazy-starts :8083, `GET :8083/health` 200, after 300s idle → model unloaded (`[verify] idle` logs), RAM freed
- Per-user judge: `resolve_judge_model` for a user with a custom judge env/config vs default user → correct model id in `[L3] ... judge=` log lines
- Judge outage: kill :8083 mid-batch → MCP batch items error (fail-closed) but UI chat replies still deliver
- **MCP over HTTP flag**: chat as `mcp-service-account` (`MCP_USER`) via `/api/chat` (JWT) → `[L3] verifying output ... lane=guardrail/MCP` in `logs/chat-webui.log` (pre-fix these read `lane=UI`); a blocked reply marks the task failed (**fail-closed**) even though it never went through the MCP gateway tool, and passes record `LEVEL 3 OUTPUT VERIFICATION PASSED` bookkeeping for DB-backed tasks
- **Judge render gate**: fire a `generate_image` chat and a judge-eligible chat concurrently → logs show `[judge] image render active — holding judge call until it finishes`; the judge proceeds after the render (+30s cooldown) and the judge model load triggers **no** `[ram] Emergency RAM evacuation`

**I3. Critic citation pass (research answers)**
- Ask a research-mode question that yields `(Author, Venue, Year) [url]` citations → logs show per-citation re-search/re-fetch, `VERIFY_FETCH_CHARS`-bounded excerpts
- Fabricated citation (prompt a specific fake source) → verdict flags it; quality < `VERIFY_QUALITY_GATE` (70) or missing cite → re-scheduled ≤ `VERIFY_MAX_RETRIES` (2), then declined/corrected — never silently delivered as verified
- One URL cited for > `VERIFY_MAX_CITES_PER_URL` (3) distinct claims → over-reliance flagged

---

## J. Resource management (thermal / RAM / idle / KV)

**J1. Idle unload (300s, per lane)**
- Load GPU model, stop chatting → after ~300s `GET /api/model-status` shows unloaded + `[llama]` idle log; **before** unloading, a KV snapshot is written to `~/local-ai-files/kv-slots/` (check file mtime)
- Reload + resume the same session → prompt tokens for that round ≪ total context (KV restored, only new tokens prefilled — compare `tokens`/`prompt_eval_count` in logs)
- CPU lane idles independently: keep CPU agents busy while GPU idles → only GPU model unloads (needs `FORCE_GPU_LANE=False`)

**J2. Thermal hysteresis (90 °C ON / 75 °C OFF)**
- Observe real load or temporarily lower `TEMP_THRESHOLD_ON` in `features/state.py` + restart → `_overheated=True` in `/api/model-status`, GPU lane shows tasks stuck `waiting`, GPU model unloaded, CPU agents unaffected
- Cool ≤ 75 °C → flag clears, queue drains; assert **no** unload while a GPU task is mid-stream (`_chat_generating` gate) and no flip-flopping between 75–90 (hysteresis)

**J3. RAM evacuation (≥95 %)**
- Induce RAM pressure (e.g. `python3 -c 'x=bytearray(6*2**30)'` sized to cross 95 %) → `[ram]` logs: in-flight tasks requeued to lane fronts with status error, llama-servers + ComfyUI killed, wait until ≤70 %, `restart_servers()`; then clients can resubmit successfully

**J4. Image VRAM choreography (gate + serialization)**
- Fire 2 concurrent `generate_image` chats → `_image_queue` serializes them (one `image_active` at a time); during the render a normal chat request must NOT reload the GPU model into VRAM (`_image_active` gate) — assert no cudaMalloc OOM in logs
- Judge calls during the render hold (`[judge] image render active`) and fire after ComfyUI finishes — a judge model load must never overlap a render (RAM-evacuation guard, see §I2)
- After render: ComfyUI VRAM freed, GPU model reloaded, KV restored, ~5s cooldown observed

---

## K. Android client (Capacitor wrapper, `android/`)

Smoke only — the scaffold is currently untracked/minimal:
- `cd android && ./gradlew assembleDebug` → APK builds
- Install, point WebView at `https://<host>/ai/` (or the LAN origin), complete SSO login in the WebView
- Send a chat, receive streamed reply; app kill + reopen → session list persisted (localStorage)
- Back button: from chat → sidebar → exits app (no WebView history trap)

---

## Suggested execution order
1. Pre-flight restart (§0, incl. FORCE_GPU_LANE check)
2. A (OpenAI — validates the VS Code work; fail fast here)
3. B3 chat e2e + C tools (exercises engine under real conditions)
4. B remaining (incl. B6–B9) + I (moderation) + D + E (read/API surfaces)
5. F offline pipeline, G browser, H infra, J resource management (slow loops last), K android (optional)
