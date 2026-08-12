import { test, expect } from '@playwright/test';
import { pngFile, codeFile, pdfFile, docxFile, xlsxFile } from './test-assets.js';

const USER = process.env.E2E_USER || 'e2e';
const PASSWORD = process.env.E2E_PASSWORD || 'e2e-pass';

async function openLoginForm(page) {
  await page.goto('/');
  await expect(page.locator('#login-overlay')).toBeVisible();
  await page.locator('input[placeholder="Username"]').fill(USER);
  await page.locator('input[placeholder="Password"]').fill(PASSWORD);
}

async function submitLogin(page) {
  await page.locator('button:has-text("Sign In")').click();
  await expect(page.locator('#login-overlay')).toBeHidden();
}

async function login(page) {
  await openLoginForm(page);
  await submitLogin(page);
}

async function openSidebar(page) {
  await page.locator('#sidebar-toggle').click();
  await expect(page.locator('#new-chat-btn')).toBeVisible();
}

async function newChat(page) {
  await openSidebar(page);
  const before = await page.locator('.session-item').count();
  await page.locator('#new-chat-btn').click();
  await expect(page.locator('.session-item')).toHaveCount(before + 1);
  await expect(page.locator('.session-name').first()).toHaveText('New Chat');
}

test('logs in against the real backend (POST /api/login, GET /api/sessions, /api/model-status)', async ({ page }) => {
  await openLoginForm(page);
  const loginResp = page.waitForResponse((r) => r.url().includes('/api/login') && r.request().method() === 'POST');
  const sessionsResp = page.waitForResponse((r) => r.url().includes('/api/sessions') && r.request().method() === 'GET');
  const statusResp = page.waitForResponse((r) => r.url().includes('/api/model-status'));
  await submitLogin(page);
  expect((await loginResp).ok()).toBe(true);
  expect((await sessionsResp).ok()).toBe(true);
  expect((await statusResp).ok()).toBe(true);
  await expect(page.locator('#model-bar')).toBeVisible();
  await expect(page.locator('#msg-input')).toBeVisible();
});

test('restores a session from the stored token via /api/check-auth on reload', async ({ page }) => {
  await login(page);
  const checkAuth = page.waitForResponse((r) => r.url().includes('/api/check-auth'));
  await page.reload();
  await checkAuth;
  await expect(page.locator('#login-overlay')).toBeHidden();
  await expect(page.locator('#model-bar')).toBeVisible();
});

test('creates a new chat through the UI (POST/GET /api/sessions)', async ({ page }) => {
  await login(page);
  await newChat(page);
});

test('sends a message and gets a real streamed reply (POST /api/chat + status polling)', async ({ page }) => {
  await login(page);
  await newChat(page);
  const chatResp = page.waitForResponse((r) => r.url().includes('/api/chat') && r.request().method() === 'POST');
  await page.locator('#msg-input').fill('Hello from the E2E suite');
  await page.locator('#send-btn').click();
  expect((await chatResp).ok()).toBe(true);
  await expect(page.locator('.msg.user').last()).toContainText('Hello from the E2E suite');
  await expect(page.locator('.msg.bot').last()).toContainText("I'm the E2E stub assistant");
  await expect(page.locator('.msg.bot').last()).toContainText('You said:');
  await expect(page.locator('.status-box')).toHaveCount(0);
});

test('renames a session (PUT /api/sessions/<id>)', async ({ page }) => {
  await login(page);
  await newChat(page);
  page.on('dialog', (d) => d.accept('Project Plan'));
  await openSidebar(page);
  const renameResp = page.waitForResponse((r) => r.url().includes('/api/sessions/') && r.request().method() === 'PUT');
  await page.locator('.session-item').first().locator('button[title="Rename"]').click();
  await renameResp;
  await expect(page.locator('.session-name').first()).toHaveText('Project Plan');
});

test('deletes a session (DELETE /api/sessions/<id>)', async ({ page }) => {
  await login(page);
  await newChat(page);
  page.on('dialog', (d) => d.accept());
  await openSidebar(page);
  const before = await page.locator('.session-item').count();
  const delResp = page.waitForResponse((r) => r.url().includes('/api/sessions/') && r.request().method() === 'DELETE');
  await page.locator('.session-item').first().locator('button[title="Delete"]').click();
  await delResp;
  await expect(page.locator('.session-item')).toHaveCount(before - 1);
});

test('manages tasks end-to-end (POST/GET/DELETE /api/tasks)', async ({ page }) => {
  await login(page);
  await page.locator('#user-name').click();
  await page.locator('button[title="Tasks"]').click();
  await expect(page.locator('#task-panel')).toBeVisible();
  await page.locator('#task-panel-header button').first().click();
  await page.locator('input[placeholder="New task..."]').fill('Buy groceries');
  const created = page.waitForResponse((r) => r.url().includes('/api/tasks') && r.request().method() === 'POST');
  await page.locator('#task-form button[type="submit"]').click();
  expect((await created).ok()).toBe(true);
  await expect(page.locator('.task-title').first()).toHaveText('Buy groceries');
  const deleted = page.waitForResponse((r) => r.url().includes('/api/tasks/') && r.request().method() === 'DELETE');
  await page.locator('.task-delete').click();
  expect((await deleted).ok()).toBe(true);
  await expect(page.locator('.task-item')).toHaveCount(0);
});

test('user context persists through the real backend (POST+GET /api/user-context)', async ({ page, context }) => {
  await login(page);
  const token = await page.evaluate(() => localStorage.getItem('auth_token'));
  const auth = { 'X-Auth-Token': token };
  const before = await context.request.get('/api/user-context', { headers: auth });
  expect(before.ok()).toBe(true);
  await context.request.post('/api/user-context', {
    headers: auth,
    data: { action: 'overwrite', context: 'E2E favourite colour is blue.' },
  });
  const after = await context.request.get('/api/user-context', { headers: auth });
  expect(after.ok()).toBe(true);
  const body = await after.json();
  expect(body.context).toContain('E2E favourite colour is blue.');
});

// ---- Sidebar & account ----

test('opens and closes the side panel', async ({ page }) => {
  await login(page);
  await expect(page.locator('#sidebar')).not.toHaveClass(/open/);
  await page.locator('#sidebar-toggle').click();
  await expect(page.locator('#sidebar')).toHaveClass(/open/);
  await expect(page.locator('#session-list')).toBeVisible();
  await page.locator('#sidebar-toggle').click();
  await expect(page.locator('#sidebar')).not.toHaveClass(/open/);
});

test('logs out (POST /api/logout, login overlay returns)', async ({ page }) => {
  await login(page);
  await page.locator('#user-name').click();
  const logoutResp = page.waitForResponse((r) => r.url().includes('/api/logout') && r.request().method() === 'POST');
  await page.locator('#user-dropdown button:has-text("Logout")').click();
  expect((await logoutResp).ok()).toBe(true);
  await expect(page.locator('#login-overlay')).toBeVisible();
});

// ---- File uploads ----

async function uploadFileAndReply(page, asset) {
  await newChat(page);
  const extractResp = page.waitForResponse(
    (r) => r.url().includes('/api/extract-file') && r.request().method() === 'POST'
  );
  await page.locator('#file-input').setInputFiles(asset);
  expect((await extractResp).ok()).toBe(true);
  await expect(page.locator('#file-badge')).toContainText(asset.name);
  const chatResp = page.waitForResponse((r) => r.url().includes('/api/chat') && r.request().method() === 'POST');
  await page.locator('#msg-input').fill('What do you think of this file?');
  await page.locator('#send-btn').click();
  const chatRes = await chatResp;
  expect(chatRes.ok()).toBe(true);
  expect(chatRes.request().postDataJSON().message).toContain('[FILE: /uploads/');
  await expect(page.locator('.msg.user').last()).toContainText(asset.name);
  await expect(page.locator('.msg.bot').last()).toContainText("I'm the E2E stub assistant");
  await expect(page.locator('.status-box')).toHaveCount(0);
}

test('uploads an image and gets a reply (POST /api/chat carries the image)', async ({ page }) => {
  await login(page);
  await newChat(page);
  await page.locator('#file-input').setInputFiles(pngFile());
  await expect(page.locator('#image-preview')).toBeVisible();
  const chatResp = page.waitForResponse((r) => r.url().includes('/api/chat') && r.request().method() === 'POST');
  await page.locator('#msg-input').fill('What is in this image?');
  await page.locator('#send-btn').click();
  const chatRes = await chatResp;
  expect(chatRes.ok()).toBe(true);
  const body = chatRes.request().postDataJSON();
  expect(typeof body.image).toBe('string');
  expect(body.image.length).toBeGreaterThan(100);
  await expect(page.locator('.msg.bot').last()).toContainText("I'm the E2E stub assistant");
  await expect(page.locator('.status-box')).toHaveCount(0);
});

test('uploads a code file and gets a reply (POST /api/extract-file → [FILE: link])', async ({ page }) => {
  await login(page);
  await uploadFileAndReply(page, codeFile());
});

test('uploads a dummy PDF and gets a reply', async ({ page }) => {
  await login(page);
  await uploadFileAndReply(page, pdfFile());
});

test('uploads an image-filled PDF and gets a reply', async ({ page }) => {
  await login(page);
  await uploadFileAndReply(page, pdfFile({ embedImage: true }));
});

test('uploads a DOCX and gets a reply', async ({ page }) => {
  await login(page);
  await uploadFileAndReply(page, docxFile());
});

test('uploads an XLSX and gets a reply', async ({ page }) => {
  await login(page);
  await uploadFileAndReply(page, xlsxFile());
});

// ---- Task creation via the LLM tool-calling pipeline ----

test('prompts the backend to create + delete a task via the manage_tasks tool', async ({ page }) => {
  await login(page);
  await newChat(page);
  const chatResp = page.waitForResponse((r) => r.url().includes('/api/chat') && r.request().method() === 'POST');
  await page.locator('#msg-input').fill('Please add a reminder: E2E_CREATE_TASK Buy milk');
  await page.locator('#send-btn').click();
  expect((await chatResp).ok()).toBe(true);
  await expect(page.locator('.msg.bot').last()).toContainText('created the task');

  await page.locator('#user-name').click();
  await page.locator('button[title="Tasks"]').click();
  await expect(page.locator('#task-panel')).toBeVisible();
  const task = page.locator('.task-item').filter({ hasText: 'Buy milk' });
  await expect(task).toHaveCount(1);

  const deleted = page.waitForResponse((r) => r.url().includes('/api/tasks/') && r.request().method() === 'DELETE');
  await task.locator('.task-delete').click();
  expect((await deleted).ok()).toBe(true);
  await expect(page.locator('.task-item').filter({ hasText: 'Buy milk' })).toHaveCount(0);
});
