/** Robustness: rapid double-send, Stop-cancel, reload recovery. */
import { bodyText, clickText, openSidebar, waitIdle } from './harness.mjs';

export const name = 'robustness';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await openSidebar(page);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  const rows = await page.evaluate(() =>
    [...document.querySelectorAll('.session-item')].length);
  if (rows < 1) throw new Error('new chat did not register a session row');
  // --- rapid double-send: second message must not vanish silently ---
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill('e2e concurrent one: say alpha');
  await clickText(page, 'Send');
  await page.waitForTimeout(1500);
  const echo = await bodyText(page);
  if (!echo.includes('e2e concurrent one')) throw new Error('first send never echoed');
  await page.locator('#msg-input').fill('e2e concurrent two: say beta');
  await clickText(page, 'Send');
  await page.waitForTimeout(4000);
  let body = await bodyText(page);
  ctx.step('concurrent sends show progress UI',
    /Stop|Queue|…|waiting/i.test(body), '');
  await page.screenshot({ path: 'reports/shot-concurrent.png' }).catch(() => {});

  // --- Stop cancels the in-flight task: output must freeze ---
  await clickText(page, 'Stop');
  await page.waitForTimeout(3000);
  const tail1 = (await bodyText(page)).slice(-400);
  await page.waitForTimeout(10000);
  const tail2 = (await bodyText(page)).slice(-400);
  ctx.step('Stop freezes streaming output', tail1 === tail2, '');
  await page.screenshot({ path: 'reports/shot-cancelled.png' }).catch(() => {});

  // --- reload: re-open the SAME session, turn must persist ---
  await page.reload();
  await page.waitForTimeout(5000);
  await openSidebar(page);
  await page.evaluate(() => {
    for (const row of document.querySelectorAll('.session-item')) {
      if ((row.innerText || '').includes('e2e concurrent one')) { row.click(); return; }
    }
  });
  await page.waitForTimeout(4000);
  body = await bodyText(page);
  ctx.step('reload rehydrates same-session turn', /e2e concurrent one/i.test(body), '');
}
