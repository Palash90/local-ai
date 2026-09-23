/** Tool status tags: web-search rounds surface the 🔍 search tag.
 *
 * Sends a question that forces a web search and asserts a .status-box with
 * data-state="search" appears while the round works.
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'tools';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(
    'e2e tools probe: search the web for the current price of silver per ounce today');
  await clickText(page, 'Send');
  // The "Searching…" tag only lives for the seconds the web_search tool
  // call runs — poll fast or the window is missed between 10s samples.
  const deadline = Date.now() + 6 * 60 * 1000;
  let tag = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(2000);
    tag = await page.evaluate(() => {
      const el = document.querySelector('.status-box[data-state="search"]');
      return el ? (el.innerText || '').slice(0, 80) : '';
    });
    if (tag) break;
    // round may already be done — stop polling once idle again AND searched
    const body = await bodyText(page);
    if ((await waitIdle(page, 15000)) && /silver/i.test(body)) break;
  }
  // innerText leads with the 🔍 icon span — match the text it carries
  ctx.step('search status tag surfaces', tag.includes('Searching'), tag);
  await page.screenshot({ path: 'reports/shot-tools.png' }).catch(() => {});
}
