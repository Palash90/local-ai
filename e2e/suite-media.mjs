/** Media end-to-end (LONG): image render lands a valid file + player card.
 *
 * Sends a tiny-icon request, waits up to ~20 min, then asserts via the
 * page (image element / player text) AND the overdue-file scan the runner
 * performs with E2E_IMAGE_DIR (same-box runs): newest png under the output
 * dir must be a valid PNG newer than the send timestamp.
 */
import fs from 'fs';
import path from 'path';
import { bodyText, clickText, openSidebar } from './harness.mjs';

export const name = 'media';
export const long = true;

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await openSidebar(page);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  await page.locator('#msg-input').fill(
    'e2e media probe: draw a tiny red sailboat icon, simple flat style');
  await clickText(page, 'Send');
  const sentAt = Date.now();
  ctx.step('image request sent', true, '');

  const deadline = sentAt + 20 * 60 * 1000;
  let card = false;
  while (Date.now() < deadline) {
    await page.waitForTimeout(60000);
    const body = await bodyText(page);
    const idx = body.lastIndexOf('e2e media probe');
    const tail = idx >= 0 ? body.slice(idx) : '';
    if (/\.png|\/output\/|player|image/i.test(tail) && tail.length > 400) { card = true; break; }
  }
  ctx.step('image card/player surfaces in chat', card, '');
  await page.screenshot({ path: 'reports/shot-media.png' }).catch(() => {});

  if (opts.imageDir && fs.existsSync(opts.imageDir)) {
    const pngs = fs.readdirSync(opts.imageDir)
      .filter(f => f.endsWith('.png'))
      .map(f => ({ f, m: fs.statSync(path.join(opts.imageDir, f)).mtimeMs }))
      .sort((a, b) => b.m - a.m);
    const fresh = pngs.length && pngs[0].m > sentAt - 60000;
    let valid = false;
    if (fresh) {
      const buf = fs.readFileSync(path.join(opts.imageDir, pngs[0].f));
      valid = buf.subarray(1, 4).toString() === 'PNG';
    }
    ctx.step('fresh valid PNG landed on disk', !!(fresh && valid),
      fresh ? `${pngs[0].f} valid=${valid}` : 'none fresh');
  } else {
    ctx.step('disk check skipped (no E2E_IMAGE_DIR)', true, '');
  }
}
