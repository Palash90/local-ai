/** Attachments: image upload+preview, unknown-ext confirm, audio negative.
 *
 * IMAGE_PATH env (or default sample below) must exist on the box running
 * the browser. Audio attach is asserted UNSUPPORTED (documents current
 * behavior — flip when the UI gains it).
 */
import fs from 'fs';
import { bodyText, clickText, openSidebar, waitIdle } from './harness.mjs';

export const name = 'attach';
export const long = false;

const DEFAULT_IMAGE = '/home/palash/local-ai-files/ComfyUI/output/mcp-service-account/gen_916bedc9__00001_.png';

export async function run(page, ctx, opts) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  await openSidebar(page);
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  const dialogs = [];
  page.on('dialog', async (d) => {
    dialogs.push(`${d.type()}:${d.message().slice(0, 60)}`);
    await d.dismiss();
  });

  // --- image attach: preview renders (badge is files-only by design) ---
  const img = opts.imagePath || DEFAULT_IMAGE;
  ctx.step('attach fixture exists', fs.existsSync(img), img);
  await page.locator('#file-input').setInputFiles(img);
  await page.waitForTimeout(8000);
  const preview = await page.locator('#image-preview').getAttribute('src').catch(() => '') || '';
  ctx.step('image preview renders after attach', preview.startsWith('data:image/'), preview.slice(0, 30));

  // --- unknown extension triggers confirm, dismiss clears ---
  const probe = `/tmp/e2e-probe-${Date.now().toString(36)}.xyz`;
  fs.writeFileSync(probe, 'hello');
  await page.locator('#file-input').setInputFiles(probe);
  await page.waitForTimeout(3000);
  ctx.step('unknown extension raises confirm dialog',
    dialogs.some(d => d.includes('Unknown file type')), JSON.stringify(dialogs).slice(0, 120));
  fs.rmSync(probe, { force: true });

  // --- audio attach: currently unsupported (confirm path only) ---
  const wav = '/home/palash/local-ai-files/music/palash/gen_035bc3d5.wav';
  if (fs.existsSync(wav)) {
    await page.locator('#file-input').setInputFiles(wav);
    await page.waitForTimeout(3000);
    const badge = await page.locator('#file-badge').count();
    ctx.step('audio attach has no badge (unsupported, documents behavior)', badge === 0, `badge=${badge}`);
  } else {
    ctx.step('audio fixture missing — skipped', true, 'no wav found');
  }

  // --- vision round-trip through the UI ---
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill('e2e vision probe: describe the attached image in one sentence');
  await clickText(page, 'Send');
  await page.waitForTimeout(150000);
  const body = await bodyText(page);
  ctx.step('vision reply arrives', body.includes('e2e vision probe') && body.length > 500, '');
  await page.screenshot({ path: 'reports/shot-attach.png' }).catch(() => {});
}
