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

  // Completed-card detection only: the transient "Generating image…"
  // status text matches /image/i and used to fake a card before the render
  // finished (then the disk check correctly found nothing). Require the
  // rendered <img> in DOM, or an /output/*.png reference in the transcript.
  const deadline = sentAt + 20 * 60 * 1000;
  let card = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(30000);
    card = await page.evaluate(() => {
      if (document.querySelectorAll('.image-wrap img').length > 0) return 'dom-img';
      const body = document.body.innerText || '';
      const idx = body.lastIndexOf('e2e media probe');
      const tail = idx >= 0 ? body.slice(idx) : '';
      if (/\/output\/[^\s)"']+\.png/i.test(tail)) return 'transcript-url';
      return '';
    });
    if (card) break;
  }
  ctx.step('image card/player surfaces in chat', card !== '', card);
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

  // --- artifact propagation: "show that image again" must re-attach the
  // SAME url without rendering a new file (finalize carry-over, not regen).
  if (!card) {
    ctx.step('reuse skipped (no render card)', true, '');
    return;
  }
  const firstUrl = (await bodyText(page)).match(/(\/output\/[^\s)"']+\.png|\/api\/[^\s)"']+)/i)?.[1] || '';
  let newestBefore = 0;
  if (opts.imageDir && fs.existsSync(opts.imageDir)) {
    newestBefore = fs.readdirSync(opts.imageDir)
      .filter(f => f.endsWith('.png'))
      .reduce((m, f) => Math.max(m, fs.statSync(path.join(opts.imageDir, f)).mtimeMs), 0);
  }
  await page.locator('#msg-input').fill(
    'e2e media reuse: show that image again, no need to regenerate');
  await clickText(page, 'Send');
  const reDeadline = Date.now() + 8 * 60 * 1000;
  let sameUrl = false;
  while (Date.now() < reDeadline) {
    await page.waitForTimeout(30000);
    const body = await bodyText(page);
    const tail = body.slice(body.lastIndexOf('e2e media reuse'));
    if (firstUrl && tail.includes(firstUrl)) { sameUrl = true; break; }
  }
  ctx.step('reuse re-attaches same artifact url', sameUrl, firstUrl || 'no url captured');
  if (opts.imageDir && fs.existsSync(opts.imageDir)) {
    const newestAfter = fs.readdirSync(opts.imageDir)
      .filter(f => f.endsWith('.png'))
      .reduce((m, f) => Math.max(m, fs.statSync(path.join(opts.imageDir, f)).mtimeMs), 0);
    ctx.step('reuse renders no new file', newestAfter <= newestBefore + 1000,
      `before=${new Date(newestBefore).toISOString()} after=${new Date(newestAfter).toISOString()}`);
  } else {
    ctx.step('reuse disk check skipped (no E2E_IMAGE_DIR)', true, '');
  }
  await page.screenshot({ path: 'reports/shot-media-reuse.png' }).catch(() => {});
}
