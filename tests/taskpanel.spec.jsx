import React from 'react';
import { test, expect } from '@playwright/experimental-ct-react';
import TaskPanel from '../src/components/TaskPanel';

const sampleTasks = [
  { id: 't1', title: 'Write report', priority: 'high', status: 'pending' },
  { id: 't2', title: 'Water plants', priority: 'low', status: 'completed', due_date: '2026-08-10' },
];

function stubFetch(tasks) {
  return async () => ({
    status: 200,
    json: async () => ({ tasks }),
  });
}

test('loads and renders tasks on mount', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async () => ({
      status: 200,
      json: async () => ({
        tasks: [
          { id: 't1', title: 'Write report', priority: 'high', status: 'pending' },
          { id: 't2', title: 'Water plants', priority: 'low', status: 'completed', due_date: '2026-08-10' },
        ],
      }),
    });
  });
  const component = await mount(<TaskPanel onClose={() => {}} />);
  await expect(component.locator('.task-item')).toHaveCount(2);
  await expect(component.locator('.task-title').nth(0)).toHaveText('Write report');
  await expect(component.locator('.task-title').nth(0)).toHaveCSS('color', 'rgb(248, 113, 113)');
  await expect(component.locator('.task-status').nth(1)).toHaveText('completed');
  await expect(component.locator('.task-item').nth(1)).toHaveClass(/done/);
});

test('toggles pending task to completed', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.__calls = [];
    window.fetch = async (url, opts) => {
      window.__calls.push({ url, method: opts && opts.method, body: opts && opts.body });
      return {
        status: 200,
        json: async () => ({ tasks: [
          { id: 't1', title: 'Write report', priority: 'high', status: 'pending' },
          { id: 't2', title: 'Water plants', priority: 'low', status: 'completed' },
        ] }),
      };
    };
  });
  const component = await mount(<TaskPanel onClose={() => {}} />);
  await component.locator('.task-item').first().locator('input[type=checkbox]').click();
  const calls = await page.evaluate(() => window.__calls);
  const put = calls.find((c) => c.method === 'PUT');
  expect(put.url).toBe('/api/tasks/t1');
  expect(JSON.parse(put.body)).toEqual({ status: 'completed' });
});

test('creates a task from the form', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.__calls = [];
    window.fetch = async (url, opts) => {
      window.__calls.push({ url, method: opts && opts.method, body: opts && opts.body });
      return {
        status: 200,
        json: async () => ({ tasks: [
          { id: 't1', title: 'Write report', priority: 'high', status: 'pending' },
        ] }),
      };
    };
  });
  const component = await mount(<TaskPanel onClose={() => {}} />);
  await component.locator('#task-panel-header button').nth(0).click();
  await component.locator('#task-form input').fill('Buy groceries');
  await component.locator('#task-form select').selectOption('high');
  await component.locator('#task-form button[type=submit]').click();
  const calls = await page.evaluate(() => window.__calls);
  const post = calls.find((c) => c.method === 'POST');
  expect(post.url).toBe('/api/tasks');
  expect(JSON.parse(post.body)).toEqual({ title: 'Buy groceries', priority: 'high' });
});

test('deletes a task', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.__calls = [];
    window.fetch = async (url, opts) => {
      window.__calls.push({ url, method: opts && opts.method });
      return { status: 200, json: async () => ({ tasks: [{ id: 't1', title: 'Write report', priority: 'high', status: 'pending' }] }) };
    };
  });
  const component = await mount(<TaskPanel onClose={() => {}} />);
  await component.locator('.task-item').first().locator('.task-delete').click();
  const calls = await page.evaluate(() => window.__calls);
  const del = calls.find((c) => c.method === 'DELETE');
  expect(del.url).toBe('/api/tasks/t1');
});

test('renders due dates with toLocaleDateString', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async () => ({
      status: 200,
      json: async () => ({ tasks: [{ id: 't1', title: 'X', priority: 'low', status: 'pending', due_date: '2026-08-10' }] }),
    });
  });
  const component = await mount(<TaskPanel onClose={() => {}} />);
  await expect(component.locator('.task-due')).toHaveText(new Date('2026-08-10').toLocaleDateString());
});

test('close button fires onClose', async ({ page, mount }) => {
  let closed = false;
  await page.evaluate(() => {
    window.fetch = async () => ({ status: 200, json: async () => ({ tasks: [] }) });
  });
  const component = await mount(<TaskPanel onClose={() => { closed = true; }} />);
  await component.locator('#task-panel-close').click();
  expect(closed).toBe(true);
});
