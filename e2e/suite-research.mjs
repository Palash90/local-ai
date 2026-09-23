/** Research end-to-end (LONG): citations + verification trail + purity.
 *
 * Sends a short research task and polls the transcript until a final
 * assistant message with citations appears (up to ~15 min). Asserts the
 * report carries inline citations and a Guardrail verification trail, and
 * that no off-topic marker terms leak in.
 */
import { bodyText, clickText, openSidebar } from './harness.mjs';

export const name = 'research';
export const long = true;

const MARKER = 'e2e-research-rakom';
const OFFTOPIC = ['santoor', 'todi', 'malkauns', 'gagaku', 'mental health'];

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await openSidebar(page);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  await page.evaluate(() => { document.querySelector('#research-toggle input')?.click(); });
  await page.waitForTimeout(400);
  await page.locator('#msg-input').fill(
    `${MARKER}: what is Raga Yaman, in 3 bullet points with citations`);
  await clickText(page, 'Send');
  ctx.step('research sent', true, '');

  const deadline = Date.now() + 15 * 60 * 1000;
  let done = false;
  let lastLen = 0;
  while (Date.now() < deadline) {
    await page.waitForTimeout(60000);
    const body = await bodyText(page);
    // Final when a citations-carrying answer follows our marker.
    const idx = body.lastIndexOf(MARKER);
    const tail = idx >= 0 ? body.slice(idx) : '';
    if (tail.length > 800 && /wikipedia|http/i.test(tail)) { done = true; break; }
    if (tail.length !== lastLen) ctx.step('research progressing', true, `${tail.length} chars`);
    lastLen = tail.length;
  }
  ctx.step('research completes within budget', done, '');
  const body = await bodyText(page);
  const tail = body.slice(body.lastIndexOf(MARKER));
  ctx.step('report carries citations', /wikipedia|http/i.test(tail), '');
  ctx.step('report carries verification trail', /Guardrail verification|QUALITY/i.test(tail), '');
  const leaks = OFFTOPIC.filter(k => tail.toLowerCase().includes(k));
  ctx.step('report topically pure', leaks.length === 0, JSON.stringify(leaks));
  await page.screenshot({ path: 'reports/shot-research.png' }).catch(() => {});
}
