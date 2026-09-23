# Browser automation suite (`e2e/`)

Drives the real UI (`/ai`) through a headed Chromium you log into once via
SSO. Covers every functionality the monkey run mapped: layout census,
session CRUD (server-verified), attachments, toggles, chat, research (+CPU),
guardrails, robustness (concurrency/cancel/reload), media renders.

## Suites

| File | Tier | What it proves |
|---|---|---|
| `suite-smoke.mjs` | smoke | 77-control census, sidebar cycle, Research↔CPU gating, all 6 nav-dock destinations, timestamps |
| `suite-crud.mjs` | smoke | create → chat → rename (UI **and** server file) → delete → gone after reload |
| `suite-attach.mjs` | smoke | PNG preview, unknown-ext confirm dialog, audio-unsupported documents current behavior, vision round-trip |
| `suite-guardrails.mjs` | smoke | UI extraction probe leaks nothing; MCP L1 declines inline; MCP dedup collapses rapid resubmits |
| `suite-robustness.mjs` | smoke | concurrent sends surface progress UI; Stop freezes output; reload rehydrates |
| `suite-research.mjs` | full | citations + verification trail + topical purity (~15 min) |
| `suite-media.mjs` | full | image card in chat + fresh valid PNG on disk (~10–20 min) |

## Run

```bash
# 1. Headed Chromium with remote debugging (log in via SSO in this window):
chromium --user-data-dir=/tmp/e2e-profile --no-sandbox \
  --remote-debugging-port=9333 --display=:1 \
  https://home.palashkantikundu.in/ai &
#    ... or: node run.mjs --launch  (starts it for you)

# 2. Fast tier (no GPU jobs):
node run.mjs --tier=smoke

# 3. Everything (research + media render):
node run.mjs --tier=full --mcp-bearer=$MCP_STATIC_SECRET

# 4. Single suite:
node run.mjs --tier=full --suites=crud,attach
```

Options: `--cdp`, `--app-url`, `--mcp-url`, `--session-dir` (enables
server-side assertions), `--image-dir`, `--image-path`, `--launch`.
Reports land in `e2e/reports/` (step log + `last-results.json` +
screenshots). Exit 0 green / 1 failure / 2 harness error.

## Notes

- Playwright resolves from the `@playwright/mcp` npx cache, overridable
  via `PLAYWRIGHT_ROOT`. Nothing is installed by this suite.
- `Logout` is inventoried but never exercised (would kill the SSO session).
- Audio attach asserts the *current* unsupported behavior; flip it if the
  UI ever gains audio input.
- Known bugs the suite guards: deleted-session ghost rows (assert gone
  **after reload**), rapid double-send drops, collapsed-sidebar button
  reachability (suites open the sidebar first and use JS clicks where the
  streaming layout churn defeats actionability checks).
