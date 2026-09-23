/** Reminders: due task shows the ⏰ badge in the task panel.
 *
 * Creates a task with a past-due reminder_at via the same-origin API (SSO
 * session applies), then asserts the due badge renders on its row.
 */
export const name = 'reminders';
export const long = false;

export async function run(page, ctx) {
  await page.setViewportSize({ width: 1600, height: 900 });
  await page.waitForTimeout(800);
  const title = `e2e-reminder-${Date.now().toString(36)}`;
  const past = new Date(Date.now() - 60000).toISOString().slice(0, 19);
  const created = await page.evaluate(async ({ title, past }) => {
    const r = await fetch('/api/tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, reminder_at: past }),
    });
    return { status: r.status, body: await r.json().catch(() => ({})) };
  }, { title, past });
  if (created.status !== 200) throw new Error('task create failed: ' + created.status);
  ctx.step('due reminder task created', true, title);

  await page.locator('#user-name').click();
  await page.waitForTimeout(800);
  await page.locator('.task-menu-item').click();
  await page.waitForTimeout(1500);
  const badge = await page.evaluate((t) => {
    for (const r of document.querySelectorAll('#task-list .task-item')) {
      if (!((r.innerText || '').includes(t))) continue;
      return (r.innerText || '').includes('⏰');
    }
    return 'row-missing';
  }, title);
  ctx.step('due badge renders on the row', badge === true, String(badge));

  // cleanup via UI delete
  await page.evaluate((t) => {
    for (const r of document.querySelectorAll('#task-list .task-item')) {
      if (!((r.innerText || '').includes(t))) continue;
      r.querySelector('.task-delete')?.click();
      return;
    }
  }, title);
  await page.waitForFunction((t) =>
    ![...document.querySelectorAll('#task-list .task-item')]
      .some(r => (r.innerText || '').includes(t)), title, { timeout: 30000 });
  ctx.step('reminder task cleaned up', true, '');
  await page.screenshot({ path: 'reports/shot-reminders.png' }).catch(() => {});
}
