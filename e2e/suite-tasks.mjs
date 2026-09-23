/** Tasks panel: add → complete → delete → persists after reload. */
import { bodyText } from './harness.mjs';

export const name = 'tasks';
export const long = false;

const marker = () => `e2e-task-${Date.now().toString(36)}`;

async function openTasks(page) {
  await page.locator('#user-name').click();
  await page.waitForTimeout(800);
  await page.locator('.task-menu-item').click();
  await page.waitForTimeout(800);
  const open = await page.evaluate(() => !!document.querySelector('#task-panel'));
  if (!open) throw new Error('task panel did not open');
}

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  const title = marker();
  await openTasks(page);

  // --- add: form starts hidden behind '+' ---
  const formShown = await page.evaluate(() => {
    if (document.querySelector('#task-form')) return true;
    const head = document.querySelector('#task-panel-header');
    const plus = [...head.querySelectorAll('button')]
      .find(b => (b.innerText || '').trim() === '+');
    if (plus) { plus.click(); return true; }
    return false;
  });
  if (!formShown) throw new Error('task form could not be shown');
  await page.locator('#task-form input[placeholder="New task..."]').fill(title);
  await page.locator('#task-form button[type="submit"]').click();
  await page.waitForFunction((t) => {
    return [...document.querySelectorAll('#task-list .task-item')]
      .some(r => (r.innerText || '').includes(t));
  }, title, { timeout: 30000 });
  ctx.step('task add renders row', true, title);

  // --- complete: checkbox in the marker's own row ---
  await page.evaluate((t) => {
    for (const r of document.querySelectorAll('#task-list .task-item')) {
      if (!((r.innerText || '').includes(t))) continue;
      r.querySelector('input[type="checkbox"]')?.click();
      return;
    }
  }, title);
  await page.waitForFunction((t) => {
    return [...document.querySelectorAll('#task-list .task-item')]
      .some(r => (r.innerText || '').includes(t) && r.classList.contains('done'));
  }, title, { timeout: 30000 });
  ctx.step('task complete marks row done', true, '');

  // --- delete: trash button in the marker's own row ---
  await page.evaluate((t) => {
    for (const r of document.querySelectorAll('#task-list .task-item')) {
      if (!((r.innerText || '').includes(t))) continue;
      r.querySelector('.task-delete')?.click();
      return;
    }
  }, title);
  await page.waitForFunction((t) => {
    return ![...document.querySelectorAll('#task-list .task-item')]
      .some(r => (r.innerText || '').includes(t));
  }, title, { timeout: 30000 });
  ctx.step('task delete removes row', true, '');

  // --- persists: reload, reopen panel, still gone ---
  await page.reload();
  await page.waitForTimeout(5000);
  await openTasks(page);
  const body = await bodyText(page);
  ctx.step('deleted task stays gone after reload', !body.includes(title), '');
  await page.screenshot({ path: 'reports/shot-tasks.png' }).catch(() => {});
}
