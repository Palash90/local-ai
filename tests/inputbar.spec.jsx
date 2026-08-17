import React from 'react';
import { test, expect } from './fixtures.js';
import InputBar from '../src/components/InputBar';

// Stub the /api/extract-file upload endpoint so code/doc attachments can be
// exercised in the component test without a real backend.
async function stubExtractFile(page, { name, url }) {
  await page.evaluate(({ name, url }) => {
    const realFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      const target = String(typeof input === 'string' ? input : input.url);
      if (target.includes('/api/extract-file')) {
        return { status: 200, json: async () => ({ url, name }) };
      }
      return realFetch(input, init);
    };
  }, { name, url });
}

test('send button submits typed text', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#msg-input').fill('Hello world');
  await component.locator('#send-btn').click();
  expect(sent).toBe('Hello world');
});

test('Enter key submits and shift+enter does not', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#msg-input').fill('line one');
  await component.locator('#msg-input').press('Shift+Enter');
  expect(sent).toBeNull();
  await component.locator('#msg-input').press('Enter');
  expect(sent).toBe('line one');
});

test('empty input does not send', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#send-btn').click();
  expect(sent).toBeNull();
});

test('whitespace-only input does not send', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#msg-input').fill('   ');
  await component.locator('#send-btn').click();
  expect(sent).toBeNull();
});

test('send button shows Queue when pending', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={true} />);
  await expect(component.locator('.send-text')).toHaveText('Queue');
});

test('send button shows Send when not pending', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} />);
  await expect(component.locator('.send-text')).toHaveText('Send');
});

test('clears input after sending', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#msg-input').fill('Hello');
  await component.locator('#send-btn').click();
  expect(sent).toBe('Hello');
  await expect(component.locator('#msg-input')).toHaveValue('');
});

test('code file attachment uploads, shows badge and sends URL', async ({ page, mount }) => {
  let sent = null;
  await stubExtractFile(page, { name: 'script.py', url: '/uploads/abc123.py' });
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#file-input').setInputFiles({
    name: 'script.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from('print("hi")'),
  });
  await expect(component.locator('#file-badge')).toContainText('script.py');
  await component.locator('#send-btn').click();
  expect(sent).toContain('[FILE: /uploads/abc123.py](script.py)');
  await expect(component.locator('#file-badge')).toHaveCount(0);
});

test('long file names are truncated in badge', async ({ page, mount }) => {
  const longName = 'a-very-very-very-very-very-long-code-file-name-that-gets-truncated.py';
  await stubExtractFile(page, { name: longName, url: '/uploads/long.py' });
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} />);
  await component.locator('#file-input').setInputFiles({
    name: longName,
    mimeType: 'text/x-python',
    buffer: Buffer.from('x = 1'),
  });
  await expect(component.locator('#file-badge')).toContainText('a-very-very-very-very-...');
});

test('attachment removal clears the badge', async ({ page, mount }) => {
  await stubExtractFile(page, { name: 'x.py', url: '/uploads/x.py' });
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} />);
  await component.locator('#file-input').setInputFiles({
    name: 'x.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from('x = 1'),
  });
  await expect(component.locator('#file-badge')).toHaveCount(1);
  await component.locator('#file-badge').locator('span').nth(1).click();
  await expect(component.locator('#file-badge')).toHaveCount(0);
});

test('doc file attachment uploads and sends URL', async ({ page, mount }) => {
  let sent = null;
  await stubExtractFile(page, { name: 'notes.md', url: '/uploads/notes.md' });
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} />);
  await component.locator('#file-input').setInputFiles({
    name: 'notes.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# Notes'),
  });
  await expect(component.locator('#file-badge')).toContainText('notes.md');
  await component.locator('#send-btn').click();
  expect(sent).toContain('[FILE: /uploads/notes.md](notes.md)');
});

test('cpu toggle is disabled until research is enabled', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} />);
  await expect(component.locator('#cpu-toggle input')).toBeDisabled();
  await component.locator('#research-toggle input').check();
  await expect(component.locator('#cpu-toggle input')).toBeEnabled();
});

test('unchecking research resets the cpu flag', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} />);
  await component.locator('#research-toggle input').check();
  await component.locator('#cpu-toggle input').check();
  await expect(component.locator('#cpu-toggle input')).toBeChecked();
  await component.locator('#research-toggle input').uncheck();
  await expect(component.locator('#cpu-toggle input')).not.toBeChecked();
  await expect(component.locator('#cpu-toggle input')).toBeDisabled();
});

test('sends research and cpu flags with the message', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (...args) => { sent = args; }} hasPending={false} />);
  await component.locator('#research-toggle input').check();
  await component.locator('#cpu-toggle input').check();
  await component.locator('#msg-input').fill('deep dive');
  await component.locator('#send-btn').click();
  expect(sent[2]).toBe(true);
  expect(sent[3]).toBe(true);
});
