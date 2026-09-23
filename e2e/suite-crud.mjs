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
  // Target the probe session's OWN row (btns[0] could rename an innocent session).
  const renamed = await page.evaluate((needle) => {
    for (const row of document.querySelectorAll('.session-item')) {
      if (!((row.innerText || '').includes(needle))) continue;
      const btn = [...row.querySelectorAll('button')]
        .find(b => (b.innerText || '').trim() === '✎');
      if (btn) { btn.click(); return 'clicked'; }
    }
    return 'row-not-found';
  }, probe);
  if (renamed !== 'clicked') throw new Error('probe session row not found for rename');
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
  const deleted = await page.evaluate((needle) => {
    for (const b of document.querySelectorAll('button')) {
      if ((b.innerText || '').trim() !== '🗑') continue;
      const row = b.closest('.session-item');
      if (row && row.innerText.includes(needle)) { b.click(); return 'clicked'; }
    }
    return 'row-not-found';
  }, probe);
  // No fallback: blindly deleting another row would nuke an innocent session
  // AND leave the probe behind (the ghost-row flake).
  if (deleted !== 'clicked') throw new Error('probe session row not found for delete');
  // Wait for the delete round-trip to actually complete (confirm accepted,
  // server delete + list refresh) instead of a fixed sleep: under load the
  // single-threaded API server can take far longer than 2.5s, and reloading
  // first aborts/reorders the DELETE → session still listed → ghost row.
  await page.waitForFunction((needle) => {
    const rows = [...document.querySelectorAll('.session-item')];
    return !rows.some(r => ((r.innerText || '').includes(needle)));
  }, probe, { timeout: 60000 });
  await page.reload();
  await page.waitForTimeout(5000);
  body = await bodyText(page);
  ctx.step('deleted session gone after reload', !body.includes(probe), '');
  await page.screenshot({ path: 'reports/shot-crud.png' }).catch(() => {});
}
