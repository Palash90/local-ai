/** Shares panel: list, open, purge — plus purged-link 404 (U21).
 *
 * Shares the latest bot reply, asserts the row in the sidebar shares tab,
 * revokes it, and asserts the old public URL now 404s unauthenticated.
 */
import { bodyText, clickText, openSidebar, waitIdle } from './harness.mjs';

export const name = 'sharespanel';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  const marker = `e2e-sharepanel-${Date.now().toString(36)}`;
  await page.locator('#msg-input').fill(
    `Reply with exactly: ${marker}`);
  await clickText(page, 'Send');
  const replyDeadline = Date.now() + 5 * 60 * 1000;
  let replied = false;
  while (Date.now() < replyDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    if ((await bodyText(page)).includes(marker)) { replied = true; break; }
  }
  if (!replied) throw new Error('no reply to share');

  // share the reply, capture url from modal input
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button.share-btn')];
    btns[btns.length - 1].click();
  });
  await page.waitForFunction(() => !!document.querySelector('.share-modal-url'),
    null, { timeout: 30000 });
  const url = await page.evaluate(() => {
    const el = document.querySelector('.share-modal-url');
    const t = ((el.value !== undefined ? el.value : el.textContent) || '').trim();
    return t.startsWith('http') ? t : window.location.origin + t;
  });
  await page.keyboard.press('Escape');
  ctx.step('share created', /^https?:\/\/.+\/.+/.test(url), url);

  // shares tab lists the row
  await openSidebar(page);
  await page.evaluate(() => {
    document.querySelector('.sidebar-tab[title="Shared messages"]')?.click();
  });
  await page.waitForTimeout(1500);
  const listed = await page.evaluate((m) =>
    [...document.querySelectorAll('#session-list .session-item')]
      .some(r => (r.innerText || '').includes(m)), marker);
  ctx.step('shares panel lists the share', listed, '');

  // revoke (Stop sharing) → row gone (confirm dialog auto-accepted)
  page.on('dialog', async (d) => { await d.accept(); });
  await page.evaluate((m) => {
    for (const r of document.querySelectorAll('#session-list .session-item')) {
      if (!((r.innerText || '').includes(m))) continue;
      const btn = [...r.querySelectorAll('button')]
        .find(b => (b.title || '').startsWith('Stop sharing') ||
                   (b.title || '').startsWith('Unshare'));
      if (btn) { btn.click(); return; }
    }
    throw new Error('share row revoke button not found');
  }, marker);
  await page.waitForFunction((m) =>
    ![...document.querySelectorAll('#session-list .session-item')]
      .some(r => (r.innerText || '').includes(m)), marker, { timeout: 30000 });
  ctx.step('revoke removes the row', true, '');

  // purged snapshot API 404s without login (the /s/ page itself is the
  // SPA shell and always 200s — the data endpoint is the real assertion)
  const token = url.split('/').filter(Boolean).pop();
  const origin = new URL(url).origin;
  const r = await page.request.get(`${origin}/api/public/share/${token}`).catch(() => null);
  ctx.step('purged share 404s', !!r && r.status() === 404, r ? `${r.status()}` : 'fetch failed');
  await page.screenshot({ path: 'reports/shot-sharespanel.png' }).catch(() => {});
}
