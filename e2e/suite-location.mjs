/** Location flow: granted geolocation resolves the popup, no stranding.
 *
 * Pre-grants browser geolocation (Kolkata) via CDP, asks a location-needing
 * question, and asserts the #location-overlay appears and then closes via
 * Allow with no error — the exact path that used to strand on the
 * googleapis-geolocate failure (now cached-fix + retry hardened).
 */
import { bodyText, clickText, waitIdle } from './harness.mjs';

export const name = 'location';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  try {
    await page.context().grantPermissions(['geolocation']);
    await page.context().setGeolocation({ latitude: 22.5726, longitude: 88.3639 });
  } catch (e) {
    ctx.step('geolocation pre-grant skipped', true, String(e).slice(0, 100));
  }
  await page.evaluate(() => document.querySelector('#new-chat-btn')?.click());
  await page.waitForTimeout(1500);
  if (!(await waitIdle(page))) throw new Error('composer never idled');
  await page.locator('#msg-input').fill(
    'e2e location probe: use my current location — which city am I in or near? Reply with just the city name.');
  await clickText(page, 'Send');

  // popup must arrive (server asked for location)...
  try {
    await page.waitForFunction(() => !!document.querySelector('#location-overlay'),
      null, { timeout: 5 * 60 * 1000 });
  } catch {
    throw new Error('location popup never appeared (model did not request location?)');
  }
  ctx.step('location popup appears', true, '');

  // ...Allow resolves it without stranding on an error...
  await page.locator('#location-allow-btn').click();
  await page.waitForFunction(() => !document.querySelector('#location-overlay'),
    null, { timeout: 60000 });
  ctx.step('allow dismisses popup', true, '');
  const errText = await page.evaluate(() =>
    (document.querySelector('#location-dialog')?.innerText || ''));
  ctx.step('no error surfaced', !/could not get location|blocked/i.test(errText), '');

  // ...and the answer eventually names the stubbed city.
  const deadline = Date.now() + 6 * 60 * 1000;
  let named = false;
  while (Date.now() < deadline) {
    await page.waitForTimeout(15000);
    if (!(await waitIdle(page))) continue;
    if (/kolkata|howrah|west bengal/i.test(await bodyText(page))) { named = true; break; }
  }
  ctx.step('answer names stubbed city', named, '');
  await page.screenshot({ path: 'reports/shot-location.png' }).catch(() => {});
  // leave permissions clean for later suites
  await page.context().clearPermissions().catch(() => {});
}
