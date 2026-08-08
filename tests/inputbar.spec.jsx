import React from 'react';
import { test, expect } from '@playwright/experimental-ct-react';
import InputBar from '../src/components/InputBar';

test('send button submits typed text', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#msg-input').fill('Hello world');
  await component.locator('#send-btn').click();
  expect(sent).toBe('Hello world');
});

test('Enter key submits and shift+enter does not', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#msg-input').fill('line one');
  await component.locator('#msg-input').press('Shift+Enter');
  expect(sent).toBeNull();
  await component.locator('#msg-input').press('Enter');
  expect(sent).toBe('line one');
});

test('empty input does not send', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#send-btn').click();
  expect(sent).toBeNull();
});

test('whitespace-only input does not send', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#msg-input').fill('   ');
  await component.locator('#send-btn').click();
  expect(sent).toBeNull();
});

test('send button shows Queue when pending', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={true} micRecording={false} onMicToggle={() => {}} />);
  await expect(component.locator('.send-text')).toHaveText('Queue');
});

test('send button shows Send when not pending', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await expect(component.locator('.send-text')).toHaveText('Send');
});

test('mic button reflects recording state', async ({ mount }) => {
  let toggled = false;
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} micRecording={true} onMicToggle={() => { toggled = true; }} />);
  await expect(component.locator('#mic-btn')).toHaveClass(/recording/);
  await component.locator('#mic-btn').click();
  expect(toggled).toBe(true);
});

test('clears input after sending', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#msg-input').fill('Hello');
  await component.locator('#send-btn').click();
  expect(sent).toBe('Hello');
  await expect(component.locator('#msg-input')).toHaveValue('');
});

test('code file attachment shows badge and wraps content on send', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#file-input').setInputFiles({
    name: 'script.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from('print("hi")'),
  });
  await expect(component.locator('#file-badge')).toContainText('script.py');
  await component.locator('#send-btn').click();
  expect(sent).toContain('[FILE: script.py]');
  expect(sent).toContain('print("hi")');
  await expect(component.locator('#file-badge')).toHaveCount(0);
});

test('long file names are truncated in badge', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#file-input').setInputFiles({
    name: 'a-very-very-very-very-very-long-code-file-name-that-gets-truncated.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from('x = 1'),
  });
  await expect(component.locator('#file-badge')).toContainText('a-very-very-very-very-...');
});

test('attachment removal clears the badge', async ({ mount }) => {
  const component = await mount(<InputBar onSend={async () => {}} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#file-input').setInputFiles({
    name: 'x.py',
    mimeType: 'text/x-python',
    buffer: Buffer.from('x = 1'),
  });
  await expect(component.locator('#file-badge')).toHaveCount(1);
  await component.locator('#file-badge').locator('span').nth(1).click();
  await expect(component.locator('#file-badge')).toHaveCount(0);
});

test('empty code file attachment still sends file text', async ({ mount }) => {
  let sent = null;
  const component = await mount(<InputBar onSend={async (t) => { sent = t; }} hasPending={false} micRecording={false} onMicToggle={() => {}} />);
  await component.locator('#file-input').setInputFiles({
    name: 'notes.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('# Notes'),
  });
  await component.locator('#send-btn').click();
  expect(sent).toContain('[FILE: notes.md]');
  expect(sent).toContain('# Notes');
  expect(sent).toContain('[END FILE]');
});
