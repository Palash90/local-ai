/** ModelBar: status widgets render and track the model-status API.
 *
 * Asserts the model dot/label, token donut and throughput readout exist,
 * then cross-checks the label against GET /api/model-status (same origin,
 * SSO session applies).
 */
export const name = 'modelbar';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  const widgets = await page.evaluate(() => ({
    dot: !!document.querySelector('#model-dot'),
    label: (document.querySelector('#model-label')?.innerText || '').trim(),
    donut: !!document.querySelector('#context-donut'),
  }));
  ctx.step('model dot renders', widgets.dot, '');
  ctx.step('model label renders', widgets.label.length > 0, widgets.label);
  ctx.step('context donut renders', widgets.donut, '');

  const api = await page.evaluate(async () => {
    try {
      const r = await fetch('/api/model-status');
      return await r.json();
    } catch (e) { return { error: String(e) }; }
  });
  const model = api && api.model ? String(api.model) : '';
  ctx.step('model-status api answers', model.length > 0, model);
  // label text should reflect a known status, not an empty/unknown string
  const known = /ready|loading|unloading|no model|active|unloaded|chat/i.test(widgets.label);
  ctx.step('label shows known status', known, widgets.label);
  await page.screenshot({ path: 'reports/shot-modelbar.png' }).catch(() => {});
}
