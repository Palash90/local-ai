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
- [ ] U06 location prompt — geolocation request flow
  (note 2026-09-23: Allow-path hardened — cached fix, 20s timeout, retry once,
  deny relabeled "Continue without location"; desktop Chromium geolocate can
  400 with no WiFi data, popup now degrades instead of stranding)
- [ ] U07 ModelBar + OverloadWarning — status widgets render
- [ ] U08 shares panel — snapshot list, open, purge
- [x] U09 TaskPanel — tasks/reminders visible and operable
  (evidence: e2e suite-tasks live PASS 2026-09-23 — add → complete → delete
  → gone after reload, reports/shot-tasks.png)
- [x] U10 Stop/Queue buttons — cancel mid-stream, queued follow-up
  (evidence: e2e suite-robustness live PASS 2026-09-23 — Stop freeze +
  Queue second-send, reports/shot-cancelled.png)
- [ ] U11 image edit e2e — photo edit renders and attaches (slow: ~10 min)
- [~] U12 TTS speak buttons + word sync
  (e2e suite-speech written, pending live run; confirmed 2026-09-23: word
  timings are API-only, the UI has no highlight surface — pytest
  test_tts_words.py covers the data side)
- [x] U13 session memory recall — nonce word stored + recalled across turns
  (evidence: e2e suite-memory live PASS 2026-09-23, reports/shot-memory.png)
- [x] U14 music clip card — composed track renders `<audio>` player
  (evidence: e2e suite-music live PASS 2026-09-23, reports/shot-music.png)
- [x] U15 concurrency — B completes while A streams; chat survives render
  (evidence: e2e suite-concurrency live PASS 2026-09-23,
  reports/shot-concurrency.png)
- [ ] U16 artifact reuse — "show that image again" re-attaches same URL
  (e2e appended to suite-media, pending live run)
- [~] U17 tool status tags — search 🔍 tag during web rounds
  (e2e suite-tools written, pending live run)

## Shares / public (`/s/`, mixed auth)

- [~] U20 public share view renders without login
  (e2e suite-share written — shares a reply, fetches the link
  unauthenticated; pending live run)
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
