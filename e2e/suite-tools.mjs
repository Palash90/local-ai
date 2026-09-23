/** Tool status tags: web-search rounds surface the 🔍 search tag.
 *
 * Sends a question that forces a web search and asserts a .status-box with
 * data-state="search" appears while the round works.
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'tools';
export const long = false;

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(
    'e2e tools probe: search the web for the current price of silver per ounce today');
  await clickText(page, 'Send');
  // The "Searching…" tag lives only for the seconds the web_search tool
  // call runs (local SearXNG answers fast) — sub-second polling or the
  // window is missed between samples.
  const deadline = Date.now() + 6 * 60 * 1000;
  let tag = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(500);
    tag = await page.evaluate(() => {
      const el = document.querySelector('.status-box[data-state="search"]');
      return el ? (el.innerText || '').slice(0, 80) : '';
    });
    if (tag) break;
  }
  // innerText leads with the 🔍 icon span — match the text it carries
  ctx.step('search status tag surfaces', tag.includes('Searching'), tag);
  // the round itself must still complete with real search evidence.
  // Check the DOM and the server-side session file: a mid-poll GPU unload
  // (idle eviction / concurrent render) can stall DOM updates while the
  // answer already landed server-side.
  const fs = await import('fs');
  const path = await import('path');
  const doneDeadline = Date.now() + 8 * 60 * 1000;
  let searched = false;
  const hasEvidence = (t) => /silver/i.test(t) && /https?:\/\//i.test(t);
  while (Date.now() < doneDeadline) {
    await page.waitForTimeout(15000);
    if (hasEvidence(await bodyText(page))) { searched = true; break; }
    const dir = opts.sessionDir;
    if (dir && fs.existsSync(dir)) {
      try {
        const files = fs.readdirSync(dir).filter(f => f.endsWith('.json'));
        const newest = files
          .map(f => ({ f, m: fs.statSync(path.join(dir, f)).mtimeMs }))
          .sort((a, b) => b.m - a.m)[0];
        if (newest) {
          const txt = fs.readFileSync(path.join(dir, newest.f), 'utf-8');
          if (hasEvidence(txt)) { searched = true; break; }
        }
      } catch { /* keep polling */ }
    }
    if (await waitIdle(page, 10000)) {
      if (hasEvidence(await bodyText(page))) { searched = true; break; }
    }
  }
  ctx.step('search round completes with citations', searched, '');
  await page.screenshot({ path: 'reports/shot-tools.png' }).catch(() => {});
}
