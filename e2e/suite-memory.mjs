/** Memory: session recall across turns + MCP user-context shape.
 *
 * Turn 1 stores a nonce word, turn 2 asks for it back (short-term session
 * memory end-to-end). When the runner has an MCP bearer (opts.home), also
 * asserts get_user_context returns parseable JSON (persistent memory path).
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'memory';
export const long = false;

const marker = () => `e2e-mango-${Date.now().toString(36)}`;

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  const word = marker();

  // --- store ---
  await page.locator('#msg-input').fill(
    `Remember this exact word: ${word}. Reply with just OK.`);
  await clickText(page, 'Send');
  const storeDeadline = Date.now() + 4 * 60 * 1000;
  let stored = false;
  while (Date.now() < storeDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    const body = await bodyText(page);
    if (body.slice(body.lastIndexOf(word)).includes('OK')) { stored = true; break; }
  }
  ctx.step('memory store turn completes', stored, '');

  // --- recall in a later turn ---
  await page.locator('#msg-input').fill(
    `What was the exact word I asked you to remember? Reply with just the word.`);
  await clickText(page, 'Send');
  const recallDeadline = Date.now() + 5 * 60 * 1000;
  let recalled = false;
  while (Date.now() < recallDeadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    const body = await bodyText(page);
    const tail = body.slice(body.lastIndexOf('exact word'));
    if (tail.includes(word)) { recalled = true; break; }
  }
  ctx.step('memory recall returns the word', recalled, recalled ? word : 'missing');
  await page.screenshot({ path: 'reports/shot-memory.png' }).catch(() => {});

  // --- persistent memory path via MCP (needs bearer; skip otherwise) ---
  if (!opts.home) {
    ctx.step('mcp user-context skipped (no bearer)', true, '');
    return;
  }
  const raw = await opts.home('get_user_context', {});
  let parsed = null;
  try { parsed = typeof raw === 'string' ? JSON.parse(raw) : raw; } catch { /* fall through */ }
  ctx.step('mcp user-context is parseable JSON',
    parsed !== null && typeof parsed === 'object', typeof raw === 'string' ? `${raw.length} chars` : typeof raw);
}
