/** Markdown hosting (guest, no login): index, story page, words API.
 *
 * Drives :3002 directly — no SSO. Asserts the free collection lists,
 * a story page renders prose, and the word-timing endpoint answers
 * (200 with a words array, or a documented skip when internal TTS
 * is unconfigured).
 */
export const name = 'markdown';
export const long = false;

const BASE = 'http://127.0.0.1:3002';

export async function run(page, ctx) {
  await page.goto(BASE + '/', { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(2000);
  // index is sparse (~100 chars text) — assert structure, not length
  const href = await page.evaluate(() => {
    const a = [...document.querySelectorAll('a')]
      .find(x => (x.getAttribute('href') || '').startsWith('/story/free_stories/'));
    return a ? a.getAttribute('href') : '';
  });
  ctx.step('guest index lists free stories', href.length > 0, href);
  if (!href) throw new Error('no free_stories link on index');
  await page.goto(BASE + href, { waitUntil: 'domcontentloaded', timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(2000);
  const proseLen = await page.evaluate(() => (document.body.innerText || '').length);
  ctx.step('story page renders prose', proseLen > 500, `${proseLen} chars`);
  await page.screenshot({ path: 'reports/shot-markdown.png' }).catch(() => {});

  const words = await page.evaluate(async (h) => {
    try {
      const r = await fetch(`${h}/words?segment_idx=0`);
      return { status: r.status, json: await r.json().catch(() => null) };
    } catch (e) { return { status: 0, err: String(e).slice(0, 80) }; }
  }, BASE + href);
  // get_tts_words returns timings only for previously-synthesized text
  // (cache lookup by design) — assert the contract: 200 + words array.
  if (words.status === 200 && Array.isArray(words.json?.words)) {
    const w0 = words.json.words[0] || {};
    const shaped = words.json.words.length === 0 ||
      (typeof w0.w === 'string' && typeof w0.s === 'number');
    ctx.step('words endpoint answers with timings shape', shaped,
      `${words.json.words.length} words (empty = segment never synthesized)`);
  } else {
    ctx.step('words endpoint answers', false, `status=${words.status}`);
  }
}
