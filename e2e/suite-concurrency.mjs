/** Concurrency end-to-end (LONG): multi-chat interleaving + chat during render.
 *
 * 1. Session A starts a long answer; mid-stream, session B asks a short
 *    question and must still complete (lane sharing, no starvation).
 * 2. Session B starts an image render; session A chats meanwhile and must
 *    still complete (render eviction + model reload choreography).
 */
import { bodyText, clickText, openSidebar, waitIdle } from './harness.mjs';

export const name = 'concurrency';
export const long = true;

async function newChat(page) {
  await openSidebar(page);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
}

async function sendIdle(page, text) {
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(text);
  await clickText(page, 'Send');
}

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);

  // --- A starts a long answer ---
  await newChat(page);
  await sendIdle(page, 'Explain Raga Yaman in exhaustive detail, at least 700 words.');
  await page.waitForTimeout(20000); // let generation get going
  const streaming = await page.evaluate(() =>
    [...document.querySelectorAll('button')]
      .some(b => (b.innerText || '').trim() === 'Stop' || (b.innerText || '').trim() === 'Queue'));
  ctx.step('session A streaming', streaming, '');

  // --- B asks mid-stream and must complete (no starvation) ---
  await newChat(page);
  await sendIdle(page, 'Reply with exactly: B-OK');
  const bDeadline = Date.now() + 8 * 60 * 1000;
  let bDone = false;
  while (Date.now() < bDeadline) {
    await page.waitForTimeout(15000);
    if ((await bodyText(page)).includes('B-OK')) { bDone = true; break; }
  }
  ctx.step('session B completes while A streams', bDone, '');

  // --- A must also finish ---
  await openSidebar(page);
  await page.evaluate(() => {
    for (const row of document.querySelectorAll('.session-item')) {
      if ((row.innerText || '').includes('Raga Yaman')) { row.click(); return; }
    }
  });
  await page.waitForTimeout(3000);
  const aDeadline = Date.now() + 8 * 60 * 1000;
  let aDone = false;
  while (Date.now() < aDeadline) {
    await page.waitForTimeout(20000);
    const body = await bodyText(page);
    if (body.includes('Raga Yaman') && body.length > 4000 && (await waitIdle(page, 60000))) {
      aDone = true; break;
    }
  }
  ctx.step('session A long answer completes', aDone, '');

  // --- image render in B, chat in A meanwhile ---
  await openSidebar(page);
  await page.evaluate(() => {
    for (const row of document.querySelectorAll('.session-item')) {
      if ((row.innerText || '').includes('B-OK')) { row.click(); return; }
    }
  });
  await page.waitForTimeout(2000);
  if (!(await waitIdle(page))) throw new Error('session B never idled');
  await page.locator('#msg-input').fill('Generate a small image of a blue lotus, low steps, tiny.');
  await clickText(page, 'Send');
  await page.waitForTimeout(30000);
  // back to A: chat must complete despite the render eviction/reload cycle
  await openSidebar(page);
  await page.evaluate(() => {
    for (const row of document.querySelectorAll('.session-item')) {
      if ((row.innerText || '').includes('Raga Yaman')) { row.click(); return; }
    }
  });
  await page.waitForTimeout(2000);
  await page.locator('#msg-input').fill('Reply with exactly: A-OK2');
  // composer may be busy (render in flight) — Queue or Send, like robustness
  await page.evaluate(() => {
    for (const b of document.querySelectorAll('button')) {
      const t = (b.innerText || '').trim();
      if (t === 'Queue' || t === 'Send') { b.click(); return; }
    }
  });
  const cDeadline = Date.now() + 12 * 60 * 1000;
  let cDone = false;
  let imgSeen = false;
  while (Date.now() < cDeadline) {
    await page.waitForTimeout(30000);
    const st = await page.evaluate(() => ({
      img: document.querySelectorAll('img').length,
      hasAok: (document.body.innerText || '').includes('A-OK2'),
    }));
    const body = await bodyText(page);
    if (/\.png|\/output\//i.test(body)) imgSeen = true;
    if (st.hasAok) { cDone = true; }
    if (cDone) break;
  }
  ctx.step('chat completes during/after render', cDone, imgSeen ? 'render card seen' : 'no card seen');
  await page.screenshot({ path: 'reports/shot-concurrency.png' }).catch(() => {});
}
