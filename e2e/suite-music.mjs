/** Music end-to-end (LONG): model-composed clip renders an <audio> card.
 *
 * Sends a tiny composition request and polls until a native audio element
 * (or a .wav link) appears in the transcript. Local synth render is fast;
 * the budget is dominated by the composition round(s).
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'music';
export const long = true;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(
    'Compose a very short calm musicbox lullaby, just a few bars, tiny file. Just do it.');
  await clickText(page, 'Send');
  ctx.step('music request sent', true, '');

  const deadline = Date.now() + 10 * 60 * 1000;
  let heard = false;
  let detail = '';
  while (Date.now() < deadline) {
    await page.waitForTimeout(30000);
    const st = await page.evaluate(() => ({
      audio: document.querySelectorAll('audio').length,
      wav: (document.body.innerText || '').match(/\.wav\b/i)?.[0] || '',
    }));
    if (st.audio > 0 || st.wav) { heard = true; detail = `${st.audio} audio, ${st.wav}`; break; }
  }
  ctx.step('music clip renders audio card', heard, detail);
  await page.screenshot({ path: 'reports/shot-music.png' }).catch(() => {});
}
