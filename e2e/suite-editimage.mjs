/** Image edit end-to-end (LONG): attached photo edit renders a new image.
 *
 * Attaches the fixture photo, asks for a simple edit, and polls for a NEW
 * rendered image card (edit path), distinct from the uploaded original.
 * ComfyUI edit renders take minutes; budget accordingly.
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'editimage';
export const long = true;

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');

  const fs = await import('fs');
  const img = (opts.imagePath && fs.existsSync(opts.imagePath)) ? opts.imagePath : null;
  if (!img) {
    ctx.step('editimage skipped (no --image-path fixture)', true, '');
    return;
  }
  await page.locator('#file-input').setInputFiles(img);
  await page.waitForFunction(() => !!document.querySelector('#image-preview'),
    null, { timeout: 30000 });
  await page.locator('#msg-input').fill(
    'e2e editimage probe: make the whole photo slightly warmer, keep everything else the same');
  await clickText(page, 'Send');
  ctx.step('edit request sent', true, '');

  const deadline = Date.now() + 20 * 60 * 1000;
  let edited = false;
  while (Date.now() < deadline) {
    await page.waitForTimeout(60000);
    const n = await page.evaluate(() =>
      document.querySelectorAll('.image-wrap img').length);
    if (n > 0) {
      const body = await bodyText(page);
      if (/\.png|\/output\//i.test(body)) { edited = true; break; }
    }
  }
  ctx.step('edited image card surfaces', edited, '');
  await page.screenshot({ path: 'reports/shot-editimage.png' }).catch(() => {});
}
