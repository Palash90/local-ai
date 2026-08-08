import React from 'react';
import { test, expect } from '@playwright/experimental-ct-react';
import App from '../src/App';

function jsonResponse(status, body) {
  return { status, json: async () => body };
}

async function stubAuthFlow(page, { taskStatus, chatResult }) {
  await page.evaluate(({ taskStatus, chatResult }) => {
    window.__chatCalls = [];
    window.fetch = async (url, opts = {}) => {
      const method = opts.method || 'GET';
      const urlStr = String(url);
      window.__chatCalls.push({ url: urlStr, method, body: opts.body });
      if (urlStr === '/api/login' && method === 'POST') {
        return { status: 200, json: async () => ({ token: 'abc', username: 'alice' }) };
      }
      if (urlStr === '/api/check-auth') {
        return { status: 200, json: async () => ({ authenticated: true, username: 'alice' }) };
      }
      if (urlStr === '/api/sessions' && method === 'GET') {
        return { status: 200, json: async () => ([
          { session_id: 's1', name: 'Test Chat' },
          { session_id: 's2', name: 'Old Chat' },
        ]) };
      }
      if (urlStr === '/api/sessions/s1/messages') {
        return { status: 200, json: async () => ({
          messages: [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'yo' }],
          token_estimate: 100,
          context_compressed: false,
          raw_token_estimate: 200,
        }) };
      }
      if (urlStr === '/api/model-status') {
        return { status: 200, json: async () => ({
          model: 'loaded', overheated: false, gpu_temp: 40,
          predicted_per_second: 20, max_context: 32768, reminder_count: 0,
        }) };
      }
      if (urlStr === '/api/chat') {
        return { status: 200, json: async () => chatResult };
      }
      if (urlStr.startsWith('/api/status/')) {
        return { status: 200, json: async () => taskStatus };
      }
      return { status: 404, json: async () => ({}) };
    };
  }, { taskStatus, chatResult });
}

test('logs in, loads sessions and renders messages', async ({ page, mount }) => {
  await stubAuthFlow(page, { taskStatus: { status: 'working', message: 'Working...' }, chatResult: {} });
  await mount(<App />);
  await page.locator('input[placeholder="Username"]').fill('alice');
  await page.locator('input[placeholder="Password"]').fill('secret');
  await page.locator('button:has-text("Sign In")').click();
  await expect(page.locator('#login-overlay')).toBeHidden();
  await expect(page.locator('.msg.user')).toContainText('hi');
  await expect(page.locator('.msg.bot')).toContainText('yo');
  await expect(page.locator('.session-item')).toHaveCount(2);
});

test('restores a session from the stored token on load', async ({ page, mount }) => {
  await page.evaluate(() => {
    localStorage.setItem('auth_token', 'abc');
    localStorage.setItem('last_sid', 's1');
  });
  await stubAuthFlow(page, { taskStatus: { status: 'working', message: 'Working...' }, chatResult: {} });
  await mount(<App />);
  await expect(page.locator('#login-overlay')).toBeHidden();
  await expect(page.locator('.msg.user')).toContainText('hi');
  await expect(page.locator('.msg.bot')).toContainText('yo');
});

test('sending a message shows the pending status', async ({ page, mount }) => {
  await stubAuthFlow(page, { taskStatus: { status: 'working', message: 'Thinking...' }, chatResult: { task_id: 't9' } });
  await mount(<App />);
  await page.locator('input[placeholder="Username"]').fill('alice');
  await page.locator('input[placeholder="Password"]').fill('secret');
  await page.locator('button:has-text("Sign In")').click();
  await expect(page.locator('.msg.user')).toContainText('hi');
  await page.locator('#msg-input').fill('hello');
  await page.locator('#send-btn').click();
  await expect(page.locator('.msg.user').last()).toContainText('hello');
  await expect(page.locator('.status-box')).toHaveCount(1);
  const calls = await page.evaluate(() => window.__chatCalls);
  const chat = calls.find((c) => c.url === '/api/chat');
  expect(chat.method).toBe('POST');
  const body = JSON.parse(chat.body);
  expect(body.session_id).toBe('s1');
  expect(body.message).toBe('hello');
});

test('pending task resolves into the assistant message', async ({ page, mount }) => {
  await stubAuthFlow(page, {
    taskStatus: { status: 'done', response: 'Great!', reasoning: 'quick' },
    chatResult: { task_id: 't9' },
  });
  await mount(<App />);
  await page.locator('input[placeholder="Username"]').fill('alice');
  await page.locator('input[placeholder="Password"]').fill('secret');
  await page.locator('button:has-text("Sign In")').click();
  await expect(page.locator('.msg.user')).toContainText('hi');
  await page.locator('#msg-input').fill('hello');
  await page.locator('#send-btn').click();
  await expect(page.locator('.status-box')).toHaveCount(1);
  await expect(page.locator('.msg.bot').filter({ hasText: 'Great!' })).toBeVisible();
  await expect(page.locator('.status-box')).toHaveCount(0);
});
