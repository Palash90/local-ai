/** Lightbox: attached image opens fullscreen overlay and dismisses.
 *
 * Attaches the fixture, sends it, clicks the in-transcript user image
 * (img[alt="Uploaded"]) and asserts #image-overlay/#fullscreen-img appear.
 * The lightbox is history-backed, so dismissal goes through history.back().
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'lightbox';
export const long = false;

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');

  const fs = await import('fs');
  const img = (opts.imagePath && fs.existsSync(opts.imagePath)) ? opts.imagePath : null;
  if (!img) {
    ctx.step('lightbox skipped (no --image-path fixture)', true, '');
    return;
  }
  await page.locator('#file-input').setInputFiles(img);
  await page.waitForFunction(() => !!document.querySelector('#image-preview'),
    null, { timeout: 30000 });
  ctx.step('composer preview renders', true, '');

  await page.locator('#msg-input').fill('e2e lightbox probe: describe this shape in five words');
  await clickText(page, 'Send');
  const replyDeadline = Date.now() + 6 * 60 * 1000;
  let replied = false;
  while (Date.now() < replyDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    const body = await bodyText(page);
    if (/shape|square|circle|triangle|red|blue|image/i.test(body)) { replied = true; break; }
  }
  if (!replied) throw new Error('no reply to the image message');

  await page.evaluate(() => {
    const el = document.querySelector('img[alt="Uploaded"]');
    if (!el) throw new Error('no uploaded image in transcript');
    el.scrollIntoView({ block: 'center' });
    el.click();
  });
  await page.waitForFunction(() =>
    !!document.querySelector('#image-overlay.open #fullscreen-img'),
    null, { timeout: 15000 });
  ctx.step('lightbox overlay opens', true, '');
  await page.screenshot({ path: 'reports/shot-lightbox.png' }).catch(() => {});

  await page.evaluate(() => window.history.back());
  await page.waitForTimeout(1500);
  const gone = await page.evaluate(() => !document.querySelector('#image-overlay.open'));
  ctx.step('lightbox dismisses via history', gone, '');
}
