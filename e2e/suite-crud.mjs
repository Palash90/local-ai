/** Session CRUD: create, chat, rename (persists), delete (gone after reload).
 *
 * Uses unmistakable `e2e-*` markers and verifies server-side via the
 * session files when E2E_SESSION_DIR is set (same-box runs).
 */
import fs from 'fs';
import path from 'path';
import { bodyText, clickText, openSidebar, waitIdle } from './harness.mjs';

export const name = 'crud';
export const long = false;

const marker = () => `e2e-${Date.now().toString(36)}`;

function sessionsWithMarker(sessionDir, text) {
  const hits = [];
  if (!sessionDir || !fs.existsSync(sessionDir)) return null;
  for (const f of fs.readdirSync(sessionDir)) {
    if (!f.endsWith('.json')) continue;
    let d;
    try { d = JSON.parse(fs.readFileSync(path.join(sessionDir, f), 'utf-8')); }
    catch { continue; }
    const sessions = (d && typeof d === 'object' && d.sessions && typeof d.sessions === 'object') ? d.sessions : {};
    for (const [sid, s] of Object.entries(sessions)) {
      if (!s || typeof s !== 'object') continue;
      const msgs = Array.isArray(s.messages) ? s.messages : [];
      for (const m of msgs) {
        if (!m || typeof m !== 'object') continue;
        const c = m.content;
        const t = typeof c === 'string' ? c : (JSON.stringify(c) || '');
        if (t.includes(text)) { hits.push({ file: f, sid, name: s.name }); break; }
      }
    }
  }
  return hits;
}

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await openSidebar(page);

  // --- create: new chat + first message ---
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  const probe = marker();
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(`${probe} say hi in one short sentence`);
  await clickText(page, 'Send');
  await page.waitForTimeout(90000);
  let body = await bodyText(page);
  ctx.step('chat round-trip completes', body.includes(probe), '');

  // --- server-side persistence of the new turn ---
  if (opts.sessionDir) {
    await new Promise(r => setTimeout(r, 5000));
    const hits = sessionsWithMarker(opts.sessionDir, probe);
    ctx.step('new turn persisted server-side', hits && hits.length > 0,
      hits ? `${hits[0].file} ${hits[0].sid.slice(0, 8)}` : 'not found');
  }

  // --- rename persists (prompt dialog accepted with marker name) ---
  const newName = `${probe}-renamed`;
  page.on('dialog', async (d) => {
    if (d.type() === 'prompt') await d.accept(newName);
    else await d.accept();
  });
  const renamed = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button')]
      .filter(b => (b.innerText || '').trim() === '✎');
    if (!btns.length) return 'no-rename-buttons';
    btns[0].click();
    return 'clicked';
  });
  await page.waitForTimeout(3000);
  body = await bodyText(page);
  ctx.step('rename visible in UI', body.includes(newName), renamed);
  if (opts.sessionDir) {
    await new Promise(r => setTimeout(r, 5000));
    const hits = sessionsWithMarker(opts.sessionDir, probe);
    const renamedHit = (hits || []).some(h => (h.name || '').includes('renamed'));
    ctx.step('rename persisted server-side', renamedHit, '');
  }

  // --- delete the probe session (row-matched), reload, assert gone ---
  await page.evaluate((needle) => {
    for (const b of document.querySelectorAll('button')) {
      if ((b.innerText || '').trim() !== '🗑') continue;
      const row = b.closest('.session-item');
      if (row && row.innerText.includes(needle)) { b.click(); return; }
    }
    // fallback: most-recent row (probe session was just created)
    const first = document.querySelector('.session-item button');
    if (first) first.click();
  }, probe);
  await page.waitForTimeout(2500);
  await page.reload();
  await page.waitForTimeout(5000);
  body = await bodyText(page);
  ctx.step('deleted session gone after reload', !body.includes(probe), '');
  await page.screenshot({ path: 'reports/shot-crud.png' }).catch(() => {});
}
