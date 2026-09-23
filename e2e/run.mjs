#!/usr/bin/env node
/** Local-AI browser automation suite runner.
 *
 * Usage:
 *   node run.mjs [--tier=smoke|full] [--cdp=http://127.0.0.1:9333]
 *     [--app-url=https://home.palashkantikundu.in/ai]
 *     [--mcp-url=http://127.0.0.1:8000/mcp] [--mcp-bearer=$TOKEN]
 *     [--session-dir=/home/palash/local-ai-files/session]
 *     [--image-dir=.../ComfyUI/output/palash] [--image-path=...png]
 *     [--launch] [--suites=smoke,crud]
 *
 * Auth: attach to a headed Chromium you already logged in (SSO) via CDP.
 * With --launch, the runner starts one (headed, own profile) and waits for
 * you to complete SSO in the opened window before running.
 * --tier=smoke skips suites marked long (research, media).
 * Exit code: 0 all green, 1 any failure, 2 harness error.
 */
import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
process.chdir(HERE);

const args = Object.fromEntries(
  process.argv.slice(2).map(a => {
    const m = a.match(/^--([^=]+)(=(.*))?$/);
    return m ? [m[1], m[3] ?? true] : [a, true];
  }));

const TIER = args.tier || 'smoke';
const CDP = args.cdp || 'http://127.0.0.1:9333';
const APP_URL = args['app-url'] || 'https://home.palashkantikundu.in/ai';
const MCP_URL = args['mcp-url'] || 'http://127.0.0.1:8000/mcp';
const MCP_BEARER = args['mcp-bearer'] || process.env.MCP_BEARER ||
  (args['mcp-bearer-file'] && fs.existsSync(args['mcp-bearer-file'])
    ? fs.readFileSync(args['mcp-bearer-file'], 'utf-8').trim() : '');
const OPENAI_KEY = args['openai-key'] || process.env.OPENAI_API_KEY ||
  (args['openai-key-file'] && fs.existsSync(args['openai-key-file'])
    ? fs.readFileSync(args['openai-key-file'], 'utf-8').trim() : '');
const SESSION_DIR = args['session-dir'] || '/home/palash/local-ai-files/session';
const IMAGE_DIR = args['image-dir'] || '/home/palash/local-ai-files/ComfyUI/output/palash';
const IMAGE_PATH = args['image-path'] || '';
const ONLY = (args.suites || '').split(',').map(s => s.trim()).filter(Boolean);

const SUITES = [
  './suite-smoke.mjs',
  './suite-crud.mjs',
  './suite-attach.mjs',
  './suite-guardrails.mjs',
  './suite-robustness.mjs',
  './suite-speech.mjs',
  './suite-tools.mjs',
  './suite-share.mjs',
  './suite-tasks.mjs',
  './suite-memory.mjs',
  './suite-research.mjs',
  './suite-media.mjs',
  './suite-music.mjs',
  './suite-concurrency.mjs',
  './suite-lightbox.mjs',
  './suite-modelbar.mjs',
  './suite-editimage.mjs',
  './suite-sharespanel.mjs',
  './suite-reminders.mjs',
  './suite-location.mjs',
  './suite-mcpbatch.mjs',
  './suite-openai.mjs',
  './suite-markdown.mjs',
  './suite-overload.mjs',
];

async function launchBrowser() {
  const cands = [
    '/home/palash/.cache/ms-playwright/chromium-1193/chrome-linux/chrome',
    '/usr/bin/chromium', '/usr/bin/google-chrome',
  ];
  const exe = cands.find(f => fs.existsSync(f));
  if (!exe) throw new Error('no chromium found for --launch');
  const profile = '/tmp/e2e-suite-profile';
  fs.mkdirSync(profile, { recursive: true });
  const disp = process.env.DISPLAY || ':1';
  const child = spawn(exe, [
    `--user-data-dir=${profile}`, '--no-sandbox', '--disable-dev-shm-usage',
    '--remote-debugging-port=9333', `--display=${disp}`,
    '--no-first-run', '--start-maximized', APP_URL,
  ], { detached: true, stdio: 'ignore', env: { ...process.env, DISPLAY: disp } });
  child.unref();
  console.log(`[run] headed browser launched (profile ${profile}) — LOG IN NOW`);
}

async function main() {
  if (args.launch) await launchBrowser();
  const { connect, ensureLoggedIn, makeCtx, mcpTool } =
    await import('./harness.mjs');
  const ctx = makeCtx({});
  ctx.log(`[run] tier=${TIER} app=${APP_URL} cdp=${CDP} suites=${ONLY.length ? ONLY.join(',') : 'all'}`);

  const withHome = MCP_BEARER ? async (tool, params) =>
    mcpTool(MCP_URL, MCP_BEARER, tool, params) : null;

  const opts = {
    sessionDir: fs.existsSync(SESSION_DIR) ? SESSION_DIR : '',
    imageDir: fs.existsSync(IMAGE_DIR) ? IMAGE_DIR : '',
    imagePath: IMAGE_PATH && fs.existsSync(IMAGE_PATH) ? IMAGE_PATH : '',
    home: withHome,
    // Secrets stay in files/env — logged only as present/absent, never values.
    hasMcpBearer: MCP_BEARER.length > 0,
    openaiKey: OPENAI_KEY,
    hasOpenaiKey: OPENAI_KEY.length > 0,
  };

  const results = [];
  for (const file of SUITES) {
    const mod = await import(file);
    if (ONLY.length && !ONLY.includes(mod.name)) continue;
    if (TIER === 'smoke' && mod.long) {
      ctx.log(`[run] SKIP ${mod.name} (long, tier=smoke)`);
      results.push({ suite: mod.name, status: 'skipped' });
      continue;
    }
    // Fresh CDP attach per suite + return to the app + login gate.
    let browser = null;
    try {
      const conn = await connect(CDP);
      browser = conn.browser;
      const page = conn.page;
      await page.goto(APP_URL, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
      await page.waitForTimeout(3000);
      const okLogin = await ensureLoggedIn(page, APP_URL, 180000);
      if (!okLogin) throw new Error('not logged in (SSO) — log in via the headed window');
      await mod.run(page, ctx, opts);
      results.push({ suite: mod.name, status: 'pass' });
      ctx.log(`[run] SUITE ${mod.name}: PASS`);
    } catch (e) {
      results.push({ suite: mod.name, status: 'fail', error: String(e && e.message || e).slice(0, 300) });
      ctx.log(`[run] SUITE ${mod.name}: FAIL :: ${String(e && e.message || e).slice(0, 300)}`);
      try {
        const c2 = await connect(CDP);
        await c2.page.screenshot({ path: `reports/shot-fail-${mod.name}.png` }).catch(() => {});
        await c2.browser.close().catch(() => {});
      } catch { /* best effort */ }
    } finally {
      try { await browser?.close()?.catch(() => {}); } catch { /* noop */ }
    }
  }

  const fails = results.filter(r => r.status === 'fail');
  ctx.log(`[run] SUMMARY ${results.length - fails.length}/${results.length} suites green` +
    (fails.length ? ` :: FAILED: ${fails.map(f => f.suite).join(', ')}` : ''));
  fs.writeFileSync('reports/last-results.json', JSON.stringify({ results, at: new Date().toISOString() }, null, 1));
  process.exit(fails.length ? 1 : 0);
}

main().catch(e => {
  console.error('[run] HARNESS ERROR:', e?.message || e);
  process.exit(2);
});
