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
- [ ] U02 new chat + poll — message sends, streams, completes
- [ ] U03 link targets — external links open `_blank`
- [ ] U04 upload — image attaches to composer
- [ ] U05 image lightbox — attachment opens/closes
- [ ] U06 location prompt — geolocation request flow
- [ ] U07 ModelBar + OverloadWarning — status widgets render
- [ ] U08 shares panel — snapshot list, open, purge
- [ ] U09 TaskPanel — tasks/reminders visible and operable
- [ ] U10 Stop/Queue buttons — cancel mid-stream, queued follow-up
- [ ] U11 image edit e2e — photo edit renders and attaches (slow: ~10 min)
- [ ] U12 TTS speak buttons + word sync

## Shares / public (`/s/`, mixed auth)

- [ ] U20 public share view renders without login
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
