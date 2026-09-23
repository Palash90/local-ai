/** Share button: message share modal yields a working public link.
 *
 * Shares the latest bot reply, reads the URL from the modal (no clipboard
 * dependence), and fetches it unauthenticated — public links need no login.
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'share';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(
    'e2e share probe: reply with exactly: shareableParcel42');
  await clickText(page, 'Send');
  const replyDeadline = Date.now() + 5 * 60 * 1000;
  let replied = false;
  while (Date.now() < replyDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    if ((await bodyText(page)).includes('shareableParcel42')) { replied = true; break; }
  }
  if (!replied) throw new Error('no reply to share');

  // hover to reveal, click the share button of the LAST bot message
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button.share-btn')];
    if (!btns.length) throw new Error('no share button');
    btns[btns.length - 1].scrollIntoView({ block: 'center' });
  });
  await page.locator('button.share-btn').last().hover().catch(() => {});
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button.share-btn')];
    btns[btns.length - 1].click();
  });
  await page.waitForFunction(() => !!document.querySelector('.share-modal-url'),
    null, { timeout: 30000 });
  const url = await page.evaluate(() => {
    const el = document.querySelector('.share-modal-url');
    // .share-modal-url is a readonly <input>: read .value, not innerText.
    const t = ((el.value !== undefined ? el.value : (el.innerText || el.textContent)) || '').trim();
    return t.startsWith('http') ? t : window.location.origin + t;
  });
  ctx.step('share modal yields public url', /^https?:\/\/.+\/.+/.test(url), url);
  const r = await page.request.get(url).catch(() => null);
  ctx.step('public link loads unauthenticated', !!r && r.ok(), r ? `${r.status()}` : 'fetch failed');
  await page.screenshot({ path: 'reports/shot-share.png' }).catch(() => {});
}
