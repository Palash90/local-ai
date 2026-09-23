/** Shared harness for the local-ai browser automation suite.
 *
 * Auth model: the operator opens a headed Chromium with remote debugging
 * (`--remote-debugging-port=9333`) and logs in via SSO once; every suite
 * attaches over CDP, so no credentials ever live in the repo. The runner
 * (`run.mjs`) can launch that browser itself with `--launch`.
 *
 * Every step is logged (stdout + report file) with PASS/FAIL. Suites throw
 * on assertion failure; the runner records and continues with the next
 * suite, then exits non-zero on any failure.
 */
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { execSync } from 'child_process';

const HERE = path.dirname(fileURLToPath(import.meta.url));

function playwrightRoots() {
  const roots = [];
  if (process.env.PLAYWRIGHT_ROOT) {
    roots.push(path.join(process.env.PLAYWRIGHT_ROOT, 'index.mjs'));
  }
  try {
    const out = execSync(
      'ls -d /home/palash/.npm/_npx/*/node_modules/playwright/index.mjs 2>/dev/null',
      { encoding: 'utf-8' });
    roots.push(...out.split('\n').map(s => s.trim()).filter(Boolean));
  } catch { /* no npx cache — fall through */ }
  return roots;
}
let _pw = null;
export async function playwright() {
  if (!_pw) {
    let lastErr = new Error('no playwright found');
    for (const root of playwrightRoots()) {
      try {
        _pw = await import(root);
        break;
      } catch (e) { lastErr = e; }
    }
    if (!_pw) {
      // Fallback: resolve from node_modules if the project ever vendors it.
      _pw = await import('playwright').catch(() => { throw lastErr; });
    }
  }
  return _pw;
}

export function makeCtx(opts = {}) {
  const reportDir = opts.reportDir || path.join(HERE, 'reports');
  fs.mkdirSync(reportDir, { recursive: true });
  const reportFile = path.join(
    reportDir, `e2e-${new Date().toISOString().replace(/[:.]/g, '-')}.log`);
  const counts = { pass: 0, fail: 0 };
  const log = (s) => {
    const line = `${new Date().toISOString()} ${s}`;
    fs.appendFileSync(reportFile, line + '\n');
    console.log(s);
  };
  const step = (name, ok, detail = '') => {
    counts[ok ? 'pass' : 'fail']++;
    log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` :: ${detail}` : ''}`);
    if (!ok) throw new Error(`assertion failed: ${name} :: ${detail}`);
  };
  return { log, step, counts, reportDir, reportFile };
}

export async function connect(cdpUrl) {
  const { chromium } = await playwright();
  const browser = await chromium.connectOverCDP(cdpUrl);
  const pages = [];
  for (const c of browser.contexts()) for (const p of c.pages()) pages.push(p);
  if (!pages.length) {
    await browser.close();
    throw new Error('no open pages on CDP endpoint ' + cdpUrl);
  }
  return { browser, page: pages[0] };
}

export async function ensureLoggedIn(page, baseUrl, timeoutMs = 120000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const url = await page.url();
    const title = await page.title().catch(() => '');
    if (url.startsWith(baseUrl) && !/authentik/i.test(title) &&
        await page.locator('#msg-input').count()) return true;
    await page.waitForTimeout(3000);
  }
  return false;
}

/** Sidebar-aware click helpers: the sidebar overlays content when open. */
export async function openSidebar(page) {
  for (let i = 0; i < 3; i++) {
    const st = await page.evaluate(() => {
      const b = document.querySelector('#new-chat-btn');
      if (!b) return 'missing';
      const r = b.getBoundingClientRect();
      return r.x >= 0 && r.width > 10 ? 'open' : 'closed';
    });
    if (st === 'open') return true;
    await page.evaluate(() => document.querySelector('#sidebar-toggle')?.click());
    await page.waitForTimeout(1500);
  }
  return false;
}

export async function closeSidebar(page) {
  const st = await page.evaluate(() => {
    const b = document.querySelector('#new-chat-btn');
    if (!b) return 'missing';
    const r = b.getBoundingClientRect();
    return r.x >= 0 && r.width > 10 ? 'open' : 'closed';
  });
  if (st === 'open') {
    await page.evaluate(() => document.querySelector('#sidebar-toggle')?.click());
    await page.waitForTimeout(1200);
  }
}

/** Wait until the composer is idle (Send present, no Stop running). */
export async function waitIdle(page, timeoutMs = 300000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    const st = await page.evaluate(() => {
      const btns = [...document.querySelectorAll('button')].map(b => (b.innerText || '').trim());
      return { send: btns.includes('Send'), stop: btns.includes('Stop') };
    });
    if (st.send && !st.stop) return true;
    await page.waitForTimeout(10000);
  }
  return false;
}

export async function clickText(page, text) {
  const clicked = await page.evaluate((t) => {
    for (const b of document.querySelectorAll('button')) {
      if ((b.innerText || '').trim() === t) { b.click(); return true; }
    }
    return false;
  }, text);
  if (!clicked) throw new Error(`clickText: no button with text ${JSON.stringify(text)}`);
}

export async function bodyText(page) {
  return page.locator('body').innerText();
}

export async function apiHealth(baseApi) {
  // baseApi like http://127.0.0.1:3001 ; returns map path->status (0 = down).
  const paths = ['/', '/api/check-auth'];
  const out = {};
  for (const p of paths) {
    try {
      const r = await fetch(baseApi + p, { signal: AbortSignal.timeout(8000) });
      out[p] = r.status;
    } catch { out[p] = 0; }
  }
  return out;
}

/** Minimal MCP client over streamable HTTP (FastMCP): tools/call only.
 * Returns the parsed result object (JSON-decoded when the tool returns
 * a JSON string, as this gateway's tools do).
 */
export async function mcpTool(mcpUrl, bearer, name, args = {}) {
  const r = await fetch(mcpUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json, text/event-stream',
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
    },
    body: JSON.stringify({
      jsonrpc: '2.0', id: 1, method: 'tools/call',
      params: { name, arguments: args },
    }),
    signal: AbortSignal.timeout(120000),
  });
  const text = await r.text();
  if (!r.ok) throw new Error(`MCP HTTP ${r.status}: ${text.slice(0, 200)}`);
  // SSE frames ("data: {...}") or a bare JSON-RPC body.
  const chunks = [];
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (t.startsWith('data:')) {
      const payload = t.slice(5).trim();
      if (payload && payload !== '[DONE]') chunks.push(payload);
    }
  }
  const body = chunks.length ? chunks.map(c => JSON.parse(c)) : [JSON.parse(text)];
  // tools/call result: { result: { content: [{ type: 'text', text }] } }
  for (const frame of body) {
    const res = frame.result || frame;
    const content = res.content;
    if (Array.isArray(content)) {
      const textOut = content.map(c => c.text || JSON.stringify(c)).join('\n');
      try { return JSON.parse(textOut); } catch { return { text: textOut }; }
    }
    if (typeof res === 'object') return res;
  }
  throw new Error('unrecognized MCP response shape');
}
