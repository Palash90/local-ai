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
  // Long-enough reply (~8-10s of audio) so playback is catchable AND
  // pausable: a 20-char clip ends before slow polls ever see it.
  const line = 'The quick brown fox jumps over the lazy dog near the quiet riverbank at dawn.';
  await page.locator('#msg-input').fill(
    `e2e speech probe: reply with exactly: ${line}`);
  await clickText(page, 'Send');
  const replyDeadline = Date.now() + 5 * 60 * 1000;
  let replied = false;
  while (Date.now() < replyDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    if ((await bodyText(page)).includes('quiet riverbank')) { replied = true; break; }
  }
  if (!replied) throw new Error('no reply to speak');

  // Spy Audio.play as belt-and-braces: playback of a short clip can end
  // between even fast polls, but a play() call proves fetch+decode+start.
  await page.evaluate(() => {
    window.__e2eAudio = { plays: 0 };
    const Orig = window.Audio;
    window.__e2eAudio.Orig = Orig;
    window.Audio = function (...args) {
      const el = new Orig(...args);
      const origPlay = el.play.bind(el);
      el.play = (...a) => {
        window.__e2eAudio.plays++;
        return origPlay(...a);
      };
      return el;
    };
  });
  // click the LAST "Read aloud" speak button (our fresh reply)
  await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button.speak-btn')]
      .filter(b => (b.getAttribute('aria-label') || '') === 'Read aloud');
    if (!btns.length) throw new Error('no Read aloud button');
    btns[btns.length - 1].click();
  });
  // Sub-second polling: states flash by (fetch is cache-fast, clip is short).
  const playDeadline = Date.now() + 4 * 60 * 1000;
  let playing = false;
  let played = false;
  while (Date.now() < playDeadline) {
    await page.waitForTimeout(500);
    const st = await page.evaluate(() => ({
      speaking: !!document.querySelector('button.speak-btn.speaking'),
      plays: (window.__e2eAudio && window.__e2eAudio.plays) || 0,
    }));
    if (st.speaking) { playing = true; break; }
    if (st.plays > 0) { played = true; break; }
  }
  ctx.step('tts reaches speaking state', playing || played,
    playing ? 'speaking class seen' : (played ? 'Audio.play() called' : ''));

  // Pause in the SAME breath as detection: any screenshot/delay lets a
  // short clip end first and the speaking button vanishes.
  let paused = false;
  if (playing) {
    await page.evaluate(() => {
      document.querySelector('button.speak-btn.speaking')?.click();
    });
    await page.waitForTimeout(1500);
    paused = await page.evaluate(() =>
      !!document.querySelector('button.speak-btn.paused'));
  }
  ctx.step('tts pauses', paused, paused ? '' : 'clip ended before pause click');
  await page.screenshot({ path: 'reports/shot-speech.png' }).catch(() => {});
}
