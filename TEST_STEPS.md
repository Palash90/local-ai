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
  - **Note:** `server/config.py` ships with `FORCE_GPU_LANE = True` (test-time flag) — while set, agents also land on the GPU lane and the CPU server is not booted. Flip to `False` and restart before asserting agent→CPU.
- Repeat with `free` non-agent user → GPU lane; verify a human never lands on CPU

**B4. Queue/lane behavior**
- Issue multiple parallel `/api/chat` and confirm they serialize per lane, statuses go `queued`
- Exceed `MAX_QUEUE_SIZE` (15) → 503

**B5. Model status**
- `GET /api/model-status` → `{model, predicted_per_second, overheated, gpu_temp, ram_evacuating, max_context, reminder_count}`; assert values sane

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

Also verify **`TOOL_FREE_AGENTS`** (editor/moderator): they get empty tools + `tool_choice:none` → never call `generate_image`.
And **image-generation VRAM**: run `generate_image` while GPU chat is loaded; assert the model unloads → ComfyUI runs → model reloads (see logs `[llama]`/`[image]`), CPU agents keep running throughout.

---

## D. MCP Gateway (`:8000`, token auth)

- `POST /mcp` with `ListToolsRequest` → 200 tool list (incl. session/chat tools, currently L2/L3 verify)
- `ListResourcesRequest`, `ListPromptsRequest` → 200
- No/invalid bearer → check `EnforcementAuthMiddleware` (expect 401)
- Run an MCP chat/verify batch end-to-end; watch guardrail llama (8083) lazy-start → `LEVEL 3` verify → auto-unload after 300s idle
- Check SQLite queue drain (`mcp_tasks_db`) and `batches` worker logs

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

- `--dry-run` w/ default + a `--config` file → prints every task plan, checklist resolution, medium feasibility, missing files, unhandled placeholders; **no LLM call**
- Real short run (`--config tasklist.json` w/ 1 task, low `--turns`) → verify: agents log in (OIDC password grant via `oidc_password_grant`), sessions created, story file + moderation `.json` written to `~/local-ai-files/stories/...`, GREEN/RED verdict, auto-RED gate (duplicate/citation drop/wrong script/name leak)
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
- **DDNS/heartbeat** (logs `[ddns] GoDaddy AAAA updated`, `[heartbeat]`) — visit logs
- Then re-run §A5/A6 (OpenAI tool-calls) to confirm the whole stack + our fix is live.

---

## Suggested execution order
1. Pre-flight restart (§0)
2. A (OpenAI — validates the VS Code work; fail fast here)
3. B3 chat e2e + C tools (exercises engine under real conditions)
4. B remaining + D + E (read/API surfaces)
5. F offline pipeline, G browser, H infra
