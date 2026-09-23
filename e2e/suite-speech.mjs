/** Speech end-to-end: TTS read-aloud reaches playing state + pause/resume.
 *
 * Sends a short message, waits for the reply, clicks the speak button
 * ("Read aloud") and asserts the button enters .speaking. Then pauses and
 * asserts .paused. NOTE: the TTS API returns word timings ("words") but the
 * UI does not highlight words — no word-sync UI exists to assert (covered
 * by pytest test_tts_words.py on the data side).
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'speech';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(
    'e2e speech probe: reply with exactly: The quick brown fox.');
  await clickText(page, 'Send');
  const replyDeadline = Date.now() + 5 * 60 * 1000;
  let replied = false;
  while (Date.now() < replyDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    if ((await bodyText(page)).includes('quick brown fox')) { replied = true; break; }
  }
  if (!replied) throw new Error('no reply to speak');

  // click the LAST "Read aloud" speak button (our fresh reply)
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button.speak-btn')]
      .filter(b => (b.getAttribute('aria-label') || '') === 'Read aloud');
    if (!btns.length) throw new Error('no Read aloud button');
    btns[btns.length - 1].click();
  });
  const playDeadline = Date.now() + 4 * 60 * 1000;
  let playing = false;
  while (Date.now() < playDeadline) {
    await page.waitForTimeout(5000);
    playing = await page.evaluate(() =>
      !!document.querySelector('button.speak-btn.speaking'));
    if (playing) break;
  }
  ctx.step('tts reaches speaking state', playing, '');
  await page.screenshot({ path: 'reports/shot-speech.png' }).catch(() => {});

  // pause via the same button, assert paused state
  await page.evaluate(() => {
    document.querySelector('button.speak-btn.speaking')?.click();
  });
  await page.waitForTimeout(3000);
  const paused = await page.evaluate(() =>
    !!document.querySelector('button.speak-btn.paused'));
  ctx.step('tts pauses', paused, '');
}
