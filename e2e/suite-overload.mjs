/** OverloadWarning: cool-state contract (hidden + flag false).
 *
 * True-overheat rendering is proven by pytest test_thermal_overheat.py
 * (latch, hysteresis, idle unload) — cooking the real GPU in e2e would
 * evict the live model. Here: warning absent when cool, and the
 * model-status overheated flag agrees with the UI.
 */
export const name = 'overload';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  const api = await page.evaluate(async () => {
    try {
      const r = await fetch('/api/model-status');
      return await r.json();
    } catch (e) { return { error: String(e) }; }
  });
  const flag = api && api.overheated === true;
  const temp = api && api.gpu_temp;
  ctx.step('model-status overheated flag reads', api && api.overheated !== undefined, `overheated=${api && api.overheated} temp=${temp}`);

  const warnVisible = await page.evaluate(() => {
    const el = document.querySelector('#overload-warn');
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!flag) {
    ctx.step('no overload warning when cool', !warnVisible, '');
  } else {
    ctx.step('overload warning shows when hot', warnVisible, `temp=${temp}`);
  }
  await page.screenshot({ path: 'reports/shot-overload.png' }).catch(() => {});
}
