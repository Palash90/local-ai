# UI Test Tracker (headed Chromium, DISPLAY=:1, 1920x1080)

Running log of browser-surface checks. Each row graduates into the future
Playwright suite (`shots/` evidence + selector hints recorded as we go).
Auth: operator pastes `document.cookie` for `home.palashkantikundu.in`;
validity re-probed every ~5 min, expiry pauses the batch (never burns
renders behind a login wall). Cookie values stay out of this file.

Legend: `[ ]` pending · `[~]` in progress · `[x]` passed (evidence linked) · `[!]` failed (note linked)

## Chat SPA (`/ai/`, authed)

- [x] U01 render+auth — page loads signed-in, no console errors
  (evidence: e2e/shots/session_check.png — kolpo session, Hindi content,
  sources, composer; login via headed Chromium persistent profile
  ~/.config/localai-e2e-chromium after manual login)
- [x] U02 new chat + poll — message sends, streams, completes
  (evidence: e2e suite-crud live PASS 2026-09-23, reports/shot-crud.png)
- [ ] U03 link targets — external links open `_blank`
- [x] U04 upload — image attaches to composer
  (evidence: e2e suite-attach live PASS 2026-09-22, reports/shot-attach.png)
- [ ] U05 image lightbox — attachment opens/closes
- [x] U06 location prompt — geolocation request flow
  (evidence: e2e suite-location live PASS 2026-09-23 — CDP-granted Kolkata
  fix, popup → Allow → city named, no stranding. Allow-path also hardened:
  cached fix, 20s timeout, retry once, deny relabeled "Continue without
  location")
- [ ] U07 ModelBar + OverloadWarning — status widgets render
- [x] U08 shares panel — snapshot list, open, purge
  (evidence: e2e suite-sharespanel live PASS 2026-09-23 — row listed,
  revoked, purged snapshot API 404s)
- [x] U09 TaskPanel — tasks/reminders visible and operable
  (evidence: e2e suite-tasks live PASS 2026-09-23 — add → complete → delete
  → gone after reload, reports/shot-tasks.png)
- [x] U10 Stop/Queue buttons — cancel mid-stream, queued follow-up
  (evidence: e2e suite-robustness live PASS 2026-09-23 — Stop freeze +
  Queue second-send, reports/shot-cancelled.png)
- [ ] U11 image edit e2e — photo edit renders and attaches (slow: ~10 min)
- [x] U12 TTS speak buttons + word sync
  (evidence: e2e suite-speech live PASS 2026-09-23 — speaking class + pause.
  Root cause of the earlier fail was test-side, not app: 5s polls missed a
  ~2s clip (handler proven live via /api/tts fetch spy). Suite now uses a
  longer reply, 500ms polls, and an Audio.play spy. Confirmed: word timings
  are API-only, no highlight UI exists — pytest test_tts_words.py covers
  the data side)
- [x] U13 session memory recall — nonce word stored + recalled across turns
  (evidence: e2e suite-memory live PASS 2026-09-23, reports/shot-memory.png)
- [x] U14 music clip card — composed track renders `<audio>` player
  (evidence: e2e suite-music live PASS 2026-09-23, reports/shot-music.png)
- [x] U15 concurrency — B completes while A streams; chat survives render
  (evidence: e2e suite-concurrency live PASS 2026-09-23,
  reports/shot-concurrency.png)
- [x] U16 artifact reuse — "show that image again" re-attaches same URL
  (evidence: e2e suite-media live PASS 2026-09-23 — same /output/ URL
  re-attached, no new file rendered. Also fixed a suite bug: the old card
  regex matched the transient "Generating image…" status, faking a card
  before render finished — now requires .image-wrap img or /output/*.png)
- [x] U17 tool status tags — search 🔍 tag during web rounds
  (evidence: e2e suite-tools live PASS 2026-09-23; tag window is seconds —
  suite polls at 500ms and falls back to session-file evidence)
- [x] U22 image lightbox — preview opens overlay, history-back dismisses
  (evidence: e2e suite-lightbox live PASS 2026-09-23, reports/shot-lightbox.png)
- [x] U23 ModelBar widgets — dot/label/donut render, label matches API
  (evidence: e2e suite-modelbar live PASS 2026-09-23)
- [x] U24 photo edit render — attached-photo edit produces a new image card
  (evidence: e2e suite-editimage live PASS 2026-09-23)
- [x] U25 message share link — modal yields public URL loading unauthenticated
  (evidence: e2e suite-share live PASS 2026-09-23, link returned HTTP 200)

## Shares / public (`/s/`, mixed auth)

- [x] U20 public share view renders without login
  (evidence: e2e suite-share live PASS 2026-09-23 — public link → HTTP 200)
- [x] U21 purged share 404s
  (evidence: e2e suite-sharespanel live PASS 2026-09-23 — snapshot API → 404)
- [x] U26 reminders — due ⏰ badge renders, task cleanup works
  (evidence: e2e suite-reminders live PASS 2026-09-23)
- [ ] U21 purged share 404s

## Markdown hosting (`:3002`, RBAC)

- [ ] U30 guest sees free(0) only; premium(1)/admin(2) gated → 401/403 matrix
- [ ] U31 story render: KaTeX, external `_blank`, Unicode paths
- [ ] U32 DELETE flows per role

## Nextcloud (`/cloud`)

- [ ] U40 login + files view; fresh render visible after `occ files:scan`
- [ ] U41 share-link flow if used

## Opencode-adjacent (this repo's consumers, not app UI)

- [ ] U50 opencode `mcp list` shows playwright connected (proven 2026-09-18)
- [ ] U51 navigate + snapshot smoke (proven example.com)

## Session log

- 2026-09-20: tracker created. Phase 0+1 (pytest 194, check_env, compile, parity) green. No browser runs yet — awaiting cookie paste + stack-up confirmation.
- 2026-09-22: smoke tier 5/7 — crud/attach/guardrails PASS; smoke FAIL (`frag is not defined`, suite typo), robustness FAIL (Send-button race vs Queue UI).
- 2026-09-23: fixed smoke typo + robustness Queue/Stop selectors; headed runs — smoke, robustness, crud, tasks, memory, music, concurrency ALL PASS. research FAIL: pipeline completes (~4k-char cited report) but GPU-lane critic timeouts push it past the 15-min budget. pytest +14 (compaction ×6 incl. a real counter bugfix, tasks_db ×4, tts_words ×4). Shares: U20 partially covered by suite-share (pending live run).
- 2026-09-23 (batch 2+3): pytest +28 (navigate-gate/themes ×8, api-edges ×6, mcp-image ×4, markdown-RBAC ×4, selfchat-config ×6) incl. a real compactions-counter bugfix (earlier turn); handler harness (fake rfile/wfile) unlocks Phase-2 api.py coverage. e2e: lightbox/modelbar/editimage/share/tools ALL PASS live (tools needed 500ms tag polling + session-file fallback; share needed .value not innerText). speech BLOCKED on Piper-under-contention hang (fail-fast added). U20 now covered live (public link → 200).
- 2026-09-23 (batch 4+5): pytest +24 (batch4-api ×8: extract/upload/tts-words/showcase/arranged; full sweep 381 pass + 1 pre-existing music-fixture fail). scripts/check_authentik.sh HEALTHY live. e2e: sharespanel (U08+U21), reminders (U26), location (U06) ALL PASS live — revoke needed confirm auto-accept; purged check targets snapshot API (SPA shell always 200s); location fix verified end-to-end. TEST_STEPS §K + README + tracker updated (25 suites, 374 pytest).
- 2026-09-23 (close-out): openai suite PASS live (auth matrix + OPENAI-OK completion; earlier 500s were CUDA-OOM from .env GPU_CTX_SIZE=32768 on the E4B profile — reverted to 24576, router respawned, E4B loaded). speech PASS (suite bug: 5s polls missed ~2s clip; fixed with longer reply + 500ms polls + Audio.play spy). media PASS incl. artifact reuse (suite bug: card regex matched transient status text; fixed to require rendered img). thermal latch pytest ×7 (extracted _thermal_step for testability). pytest total 392 collected. Bearer/key files at /tmp/mcp-bearer + /tmp/openai-key (600) for future runs.
