import React from 'react';
import { test, expect } from './fixtures.js';
import OverloadWarning from '../src/components/OverloadWarning';
import LocationPrompt from '../src/components/LocationPrompt';
import Sidebar from '../src/components/Sidebar';
import ModelBar from '../src/components/ModelBar';
import LoginScreen from '../src/components/LoginScreen';

// ---------- OverloadWarning ----------

test('OverloadWarning renders nothing when not overheated', async ({ mount }) => {
  const component = await mount(<OverloadWarning overheated={false} gpuTemp={null} />);
  await expect(component.locator('#overload-warn')).toHaveCount(0);
});

test('OverloadWarning shows GPU temperature when overheated', async ({ mount }) => {
  const component = await mount(<OverloadWarning overheated gpuTemp={87} />);
  await expect(component).toContainText('Server overloaded');
  await expect(component).toContainText('(GPU: 87\u00B0C)');
});

test('OverloadWarning hides GPU temperature when unknown', async ({ mount }) => {
  const component = await mount(<OverloadWarning overheated gpuTemp={null} />);
  await expect(component).toContainText('Server overloaded');
  await expect(component).not.toContainText('GPU:');
});

// ---------- LocationPrompt ----------

test('LocationPrompt renders default description and buttons', async ({ mount }) => {
  const component = await mount(<LocationPrompt onAllow={() => {}} onDeny={() => {}} />);
  await expect(component.locator('#location-title')).toContainText('Share your location?');
  await expect(component.locator('#location-desc')).toContainText('Local AI can use your location');
  await expect(component.locator('#location-allow-btn')).toContainText('Allow');
  await expect(component.locator('#location-deny-btn')).toContainText('Deny');
});

test('LocationPrompt shows custom error message', async ({ mount }) => {
  const component = await mount(<LocationPrompt onAllow={() => {}} onDeny={() => {}} error="custom error text" />);
  await expect(component.locator('#location-desc')).toContainText('custom error text');
});

test('LocationPrompt disables Allow when blocked', async ({ mount }) => {
  const component = await mount(<LocationPrompt onAllow={() => {}} onDeny={() => {}} error="Location access is blocked" />);
  await expect(component.locator('#location-allow-btn')).toBeDisabled();
});

test('LocationPrompt enables Allow for non-blocking errors', async ({ mount }) => {
  const component = await mount(<LocationPrompt onAllow={() => {}} onDeny={() => {}} error="Could not get location" />);
  await expect(component.locator('#location-allow-btn')).toBeEnabled();
});

test('LocationPrompt Allow button triggers onAllow', async ({ mount }) => {
  let allowed = false;
  const component = await mount(<LocationPrompt onAllow={() => { allowed = true; }} onDeny={() => {}} />);
  await component.locator('#location-allow-btn').click();
  expect(allowed).toBe(true);
});

test('LocationPrompt Deny button triggers onDeny', async ({ mount }) => {
  let denied = false;
  const component = await mount(<LocationPrompt onAllow={() => {}} onDeny={() => { denied = true; }} />);
  await component.locator('#location-deny-btn').click();
  expect(denied).toBe(true);
});

test('LocationPrompt overlay click denies, dialog click does not', async ({ mount }) => {
  let denied = 0;
  const component = await mount(<LocationPrompt onAllow={() => {}} onDeny={() => { denied += 1; }} />);
  await component.locator('#location-dialog').click();
  expect(denied).toBe(0);
  await component.evaluate((el) => el.click());
  expect(denied).toBe(1);
});

// ---------- Sidebar ----------

const sessions = [
  { session_id: 'a', name: 'Short name' },
  { session_id: 'b', name: 'This session name is longer than thirty characters total' },
];

test('Sidebar renders sessions and truncates long names', async ({ mount }) => {
  const component = await mount(
    <Sidebar sessions={sessions} currentSessionId="a" onSwitchSession={() => {}} onNewChat={() => {}} onRenameSession={() => {}} onDeleteSession={() => {}} onClose={() => {}} open />
  );
  await expect(component.locator('.session-item')).toHaveCount(2);
  await expect(component.locator('.session-item').nth(0)).toContainText('Short name');
  await expect(component.locator('.session-item').nth(1)).toContainText('This session name is longer th...');
});

test('Sidebar marks active session', async ({ mount }) => {
  const component = await mount(
    <Sidebar sessions={sessions} currentSessionId="b" onSwitchSession={() => {}} onNewChat={() => {}} onRenameSession={() => {}} onDeleteSession={() => {}} onClose={() => {}} open />
  );
  await expect(component.locator('.session-item').nth(1)).toHaveClass(/active/);
  await expect(component.locator('.session-item').nth(0)).not.toHaveClass(/active/);
});

test('Sidebar switches session on click', async ({ mount }) => {
  let switched = null;
  const component = await mount(
    <Sidebar sessions={sessions} currentSessionId="a" onSwitchSession={(id) => { switched = id; }} onNewChat={() => {}} onRenameSession={() => {}} onDeleteSession={() => {}} onClose={() => {}} open />
  );
  await component.locator('.session-item').nth(1).click();
  expect(switched).toBe('b');
});

test('Sidebar new chat button fires onNewChat', async ({ mount }) => {
  let created = false;
  const component = await mount(
    <Sidebar sessions={[]} currentSessionId={null} onSwitchSession={() => {}} onNewChat={() => { created = true; }} onRenameSession={() => {}} onDeleteSession={() => {}} onClose={() => {}} open />
  );
  await component.locator('#new-chat-btn').click();
  expect(created).toBe(true);
});

test('Sidebar rename and delete call their handlers with session id', async ({ mount }) => {
  let renamed = null;
  let deleted = null;
  const component = await mount(
    <Sidebar sessions={sessions} currentSessionId="a" onSwitchSession={() => {}} onNewChat={() => {}} onRenameSession={(id) => { renamed = id; }} onDeleteSession={(id) => { deleted = id; }} onClose={() => {}} open />
  );
  await component.locator('.session-item').first().getByTitle('Rename').click();
  expect(renamed).toBe('a');
  await component.locator('.session-item').first().getByTitle('Delete').click();
  expect(deleted).toBe('a');
});

test('Sidebar overlay click triggers onClose', async ({ mount }) => {
  let closed = false;
  const component = await mount(
    <Sidebar sessions={[]} currentSessionId={null} onSwitchSession={() => {}} onNewChat={() => {}} onRenameSession={() => {}} onDeleteSession={() => {}} onClose={() => { closed = true; }} open />
  );
  await component.locator('#sidebar-overlay').evaluate((el) => el.click());
  expect(closed).toBe(true);
});

// ---------- ModelBar ----------

const baseProps = {
  modelStatus: 'chat_loaded',
  modelTps: null,
  tokenEstimate: 0,
  contextCompressed: false,
  rawTokenEstimate: 0,
  maxContext: 24576,
  onToggleSidebar: () => {},
  username: 'palash',
  onLogout: () => {},
  reminderCount: 0,
  onToggleTasks: () => {},
};

test('ModelBar shows status label', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} modelStatus="loading" />);
  await expect(component.locator('#model-dot')).toHaveClass('loading');
  await expect(component.locator('#model-label')).toHaveText('Loading model...');
});

test('ModelBar shows raw status when unmapped', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} modelStatus="weird_state" />);
  await expect(component.locator('#model-label')).toHaveText('weird_state');
});

test('ModelBar warns when tps is slow', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} modelTps={3.2} />);
  await expect(component.locator('#model-tps')).toContainText('3.2 t/s');
  await expect(component.locator('#model-tps')).toContainText('\u26A0');
});

test('ModelBar shows tps without warning when fast', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} modelTps={12.5} />);
  await expect(component.locator('#model-tps')).toContainText('12.5 t/s');
  await expect(component.locator('#model-tps')).not.toContainText('\u26A0');
});

test('ModelBar formats token estimate in k', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} tokenEstimate={1500} />);
  await expect(component.locator('.token-text')).toContainText('1.5k');
  await expect(component.locator('.token-text')).toContainText('25k');
});

test('ModelBar shows compressed context marker', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} tokenEstimate={1000} rawTokenEstimate={12000} contextCompressed maxContext={8000} />);
  await expect(component.locator('.token-compressed')).toContainText('(12.0k)');
});

test('ModelBar hides token text when estimate is zero', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} />);
  await expect(component.locator('.token-text')).toHaveCount(0);
});

test('ModelBar donut turns red at high usage', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} tokenEstimate={30000} />);
  await expect(component.locator('#context-donut circle').nth(1)).toHaveAttribute('stroke', '#f87171');
});

test('ModelBar donut stays green at low usage', async ({ mount }) => {
  const component = await mount(<ModelBar {...baseProps} tokenEstimate={1000} />);
  await expect(component.locator('#context-donut circle').nth(1)).toHaveAttribute('stroke', '#4ade80');
});

test('ModelBar sidebar toggle fires', async ({ mount }) => {
  let toggled = false;
  const component = await mount(<ModelBar {...baseProps} onToggleSidebar={() => { toggled = true; }} />);
  await component.locator('#sidebar-toggle').click();
  expect(toggled).toBe(true);
});

test('ModelBar user menu opens dropdown and logout works', async ({ mount }) => {
  let loggedOut = false;
  const component = await mount(<ModelBar {...baseProps} onLogout={() => { loggedOut = true; }} />);
  await component.locator('#user-name').click();
  await expect(component.locator('#user-dropdown')).toHaveClass(/open/);
  await component.locator('#user-dropdown').getByText('Logout').click();
  expect(loggedOut).toBe(true);
});

test('ModelBar tasks button toggles tasks and shows reminder badge', async ({ mount }) => {
  let toggled = false;
  const component = await mount(<ModelBar {...baseProps} reminderCount={3} onToggleTasks={() => { toggled = true; }} />);
  await component.locator('#user-name').click();
  await component.locator('.task-menu-item').click();
  expect(toggled).toBe(true);
  await expect(component.locator('#reminder-badge')).toHaveText('3');
});

// ---------- LoginScreen ----------

test('LoginScreen does not submit with empty fields', async ({ mount }) => {
  let submitted = false;
  const component = await mount(<LoginScreen onLogin={async () => { submitted = true; }} />);
  await component.getByRole('button', { name: 'Sign In' }).click();
  expect(submitted).toBe(false);
});

test('LoginScreen submits trimmed credentials', async ({ mount }) => {
  let received = null;
  const component = await mount(<LoginScreen onLogin={async (u, p) => { received = { u, p }; }} />);
  await component.getByPlaceholder('Username').fill('  palash  ');
  await component.getByPlaceholder('Password').fill('  secret  ');
  await component.getByRole('button', { name: 'Sign In' }).click();
  expect(received).toEqual({ u: 'palash', p: 'secret' });
});

test('LoginScreen submits on Enter key', async ({ mount }) => {
  let submitted = false;
  const component = await mount(<LoginScreen onLogin={async () => { submitted = true; }} />);
  await component.getByPlaceholder('Username').fill('palash');
  await component.getByPlaceholder('Password').fill('secret');
  await component.getByPlaceholder('Password').press('Enter');
  expect(submitted).toBe(true);
});

test('LoginScreen keeps error hidden after successful login', async ({ mount }) => {
  const component = await mount(<LoginScreen onLogin={async () => {}} />);
  await expect(component.locator('.login-error')).not.toHaveClass(/show/);
  await component.getByPlaceholder('Username').fill('palash');
  await component.getByPlaceholder('Password').fill('secret');
  await component.getByRole('button', { name: 'Sign In' }).click();
  await expect(component.locator('.login-error')).not.toHaveClass(/show/);
});

test('LoginScreen opens a shared message from a pasted link', async ({ mount }) => {
  let opened = null;
  const component = await mount(<LoginScreen onLogin={async () => {}} onOpenShare={(t) => { opened = t; }} />);
  await component.getByPlaceholder('Shared message link').fill('http://192.168.1.10:3001/s/abc123');
  await component.getByRole('button', { name: 'View shared message' }).click();
  expect(opened).toBe('abc123');
});

test('LoginScreen opens a shared message from a bare token', async ({ mount }) => {
  let opened = null;
  const component = await mount(<LoginScreen onLogin={async () => {}} onOpenShare={(t) => { opened = t; }} />);
  await component.getByPlaceholder('Shared message link').fill('deadbeef1234');
  await component.getByRole('button', { name: 'View shared message' }).click();
  expect(opened).toBe('deadbeef1234');
});

test('LoginScreen ignores invalid share input', async ({ mount }) => {
  let opened = false;
  const component = await mount(<LoginScreen onLogin={async () => {}} onOpenShare={() => { opened = true; }} />);
  await component.getByPlaceholder('Shared message link').fill('  ');
  await component.getByRole('button', { name: 'View shared message' }).click();
  expect(opened).toBe(false);
});
