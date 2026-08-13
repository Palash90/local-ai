import { test, expect } from './fixtures.js';
import * as api from '../src/api.js';

let stored;
let listeners;

function stubGlobals() {
  stored = {};
  listeners = {};
  globalThis.localStorage = {
    getItem: (k) => (k in stored ? stored[k] : null),
    setItem: (k, v) => { stored[k] = String(v); },
    removeItem: (k) => { delete stored[k]; },
    clear: () => { stored = {}; },
  };
  globalThis.window = {
    dispatchEvent: (e) => { (listeners[e.type] || []).forEach((fn) => fn(e)); },
    addEventListener: (t, fn) => { (listeners[t] ||= []).push(fn); },
    location: { origin: 'http://localhost:3000' },
    open: () => {},
  };
  globalThis.__unauthorizedFired = false;
  globalThis.window.addEventListener('auth:unauthorized', () => { globalThis.__unauthorizedFired = true; });
}

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

test.beforeEach(() => {
  stubGlobals();
  globalThis.fetch = async () => jsonResponse({}, 200);
});

test('login posts credentials and stores token', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, opts });
    return jsonResponse({ token: 'tok123' }, 200);
  };
  const data = await api.login('palash', 'secret');
  expect(data.token).toBe('tok123');
  expect(stored.auth_token).toBe('tok123');
  expect(calls[0].url).toBe('/api/login');
  expect(calls[0].opts.method).toBe('POST');
  expect(JSON.parse(calls[0].opts.body)).toEqual({ username: 'palash', password: 'secret' });
});

test('login without token does not store one', async () => {
  globalThis.fetch = async () => jsonResponse({ error: 'bad' }, 401);
  const data = await api.login('palash', 'wrong');
  expect(stored.auth_token).toBeUndefined();
  expect(data.error).toBe('bad');
});

test('authFetch adds X-Auth-Token header when present', async () => {
  stored.auth_token = 'secret-token';
  let headers;
  globalThis.fetch = async (_url, opts) => {
    headers = opts.headers;
    return jsonResponse({ ok: true }, 200);
  };
  await api.fetchSessions();
  expect(headers['X-Auth-Token']).toBe('secret-token');
});

test('authFetch 401 clears token and dispatches auth:unauthorized', async () => {
  stored.auth_token = 'expired-token';
  globalThis.fetch = async () => jsonResponse({ error: 'unauthorized' }, 401);
  await api.fetchSessions();
  expect(stored.auth_token).toBeUndefined();
  expect(globalThis.__unauthorizedFired).toBe(true);
});

test('logout posts then clears token even on network failure', async () => {
  stored.auth_token = 'tok';
  let posted = false;
  globalThis.fetch = async () => {
    posted = true;
    throw new Error('network down');
  };
  await api.logout();
  expect(stored.auth_token).toBeUndefined();
  expect(posted).toBe(true);
});

test('sendMessage includes session_id, message and client timestamp', async () => {
  let captured;
  globalThis.fetch = async (url, opts) => {
    captured = { url, opts };
    return jsonResponse({ ok: true }, 200);
  };
  await api.sendMessage('sess-1', 'hello', null, null, '2026-08-08T12:00:00+05:30');
  expect(captured.url).toBe('/api/chat');
  expect(captured.opts.method).toBe('POST');
  const body = JSON.parse(captured.opts.body);
  expect(body.session_id).toBe('sess-1');
  expect(body.message).toBe('hello');
  expect(body.client_timestamp).toBe('2026-08-08T12:00:00+05:30');
});

test('sendMessage adds image and audio payloads', async () => {
  let body;
  globalThis.fetch = async (_url, opts) => {
    body = JSON.parse(opts.body);
    return jsonResponse({ ok: true }, 200);
  };
  await api.sendMessage('sess-1', 'hi', 'data:image/png;base64,AAA', 'data:audio/wav;base64,BBB');
  expect(body.image).toBe('data:image/png;base64,AAA');
  expect(body.audio).toBe('data:audio/wav;base64,BBB');
});

test('sendMessage uses a local timestamp when none given', async () => {
  let body;
  globalThis.fetch = async (_url, opts) => {
    body = JSON.parse(opts.body);
    return jsonResponse({ ok: true }, 200);
  };
  await api.sendMessage('sess-1', 'hi');
  expect(body.client_timestamp).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$/);
});

test('sendMessage throws with server error on 503', async () => {
  globalThis.fetch = async () => jsonResponse({ error: 'queue full' }, 503);
  await expect(api.sendMessage('sess-1', 'hi')).rejects.toThrow('queue full');
});

test('sendMessage throws generic message on 503 without error body', async () => {
  globalThis.fetch = async () => jsonResponse({}, 503);
  await expect(api.sendMessage('sess-1', 'hi')).rejects.toThrow('Server is busy');
});

test('createSession posts New Chat body', async () => {
  let captured;
  globalThis.fetch = async (url, opts) => {
    captured = { url, opts };
    return jsonResponse({ session: { id: 'new' } }, 200);
  };
  const data = await api.createSession();
  expect(captured.url).toBe('/api/sessions');
  expect(JSON.parse(captured.opts.body)).toEqual({ name: 'New Chat' });
  expect(data.session.id).toBe('new');
});

test('renameSession PUTs the new name', async () => {
  let captured;
  globalThis.fetch = async (url, opts) => {
    captured = { url, opts };
    return jsonResponse({}, 200);
  };
  await api.renameSession('sess-2', 'Renamed');
  expect(captured.url).toBe('/api/sessions/sess-2');
  expect(captured.opts.method).toBe('PUT');
  expect(JSON.parse(captured.opts.body)).toEqual({ name: 'Renamed' });
});

test('deleteSession issues DELETE request', async () => {
  let captured;
  globalThis.fetch = async (url, opts) => {
    captured = { url, opts };
    return jsonResponse({}, 200);
  };
  await api.deleteSession('sess-3');
  expect(captured.url).toBe('/api/sessions/sess-3');
  expect(captured.opts.method).toBe('DELETE');
});

test('getTaskStatus fetches status without auth header', async () => {
  let capturedUrl;
  let optsSeen;
  globalThis.fetch = async (url, opts) => {
    capturedUrl = url;
    optsSeen = opts;
    return jsonResponse({ status: 'pending' }, 200);
  };
  const data = await api.getTaskStatus('task-9');
  expect(capturedUrl).toBe('/api/status/task-9');
  expect(optsSeen).toBeUndefined();
  expect(data.status).toBe('pending');
});

test('getModelStatus sends auth token if present', async () => {
  stored.auth_token = 'tok';
  let headers;
  globalThis.fetch = async (_url, opts) => {
    headers = opts.headers;
    return jsonResponse({ model: 'qwen' }, 200);
  };
  const data = await api.getModelStatus();
  expect(headers['X-Auth-Token']).toBe('tok');
  expect(data.model).toBe('qwen');
});

test('getModelStatus sends no header without token', async () => {
  let headers;
  globalThis.fetch = async (_url, opts) => {
    headers = opts.headers;
    return jsonResponse({}, 200);
  };
  await api.getModelStatus();
  expect(headers).toEqual({});
});

test('sendLocation and denyLocation post task payloads', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return jsonResponse({}, 200);
  };
  await api.sendLocation(23.8, 90.4, 'task-1');
  await api.denyLocation('task-1');
  expect(calls[0]).toEqual({ url: '/api/location', body: { latitude: 23.8, longitude: 90.4, task_id: 'task-1' } });
  expect(calls[1]).toEqual({ url: '/api/location', body: { denied: true, task_id: 'task-1' } });
});

test('speak posts text and voice', async () => {
  let captured;
  globalThis.fetch = async (url, opts) => {
    captured = { url, body: JSON.parse(opts.body) };
    return jsonResponse({ audio: 'wav' }, 200);
  };
  const data = await api.speak('hello', 'bengali');
  expect(captured).toEqual({ url: '/api/tts', body: { text: 'hello', voice: 'bengali' } });
  expect(data.audio).toBe('wav');
});

test('speak defaults voice when omitted', async () => {
  let body;
  globalThis.fetch = async (_url, opts) => {
    body = JSON.parse(opts.body);
    return jsonResponse({}, 200);
  };
  await api.speak('hello');
  expect(body.voice).toBeUndefined();
  expect(body.text).toBe('hello');
});

test('extractFile posts name and data', async () => {
  let captured;
  globalThis.fetch = async (url, opts) => {
    captured = { url, body: JSON.parse(opts.body) };
    return jsonResponse({ text: 'extracted' }, 200);
  };
  const data = await api.extractFile('doc.pdf', 'b64data');
  expect(captured).toEqual({ url: '/api/extract-file', body: { name: 'doc.pdf', data: 'b64data' } });
  expect(data.text).toBe('extracted');
});

test('task CRUD hits the right endpoints', async () => {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, method: opts.method, body: opts.body });
    return jsonResponse({ tasks: [] }, 200);
  };
  await api.fetchTasks();
  await api.createTask({ title: 'Write report' });
  await api.updateTask('t1', { done: true });
  await api.deleteTask('t1');
  expect(calls[0].url).toBe('/api/tasks');
  expect(calls[0].method).toBeUndefined();
  expect(calls[1].url).toBe('/api/tasks');
  expect(calls[1].method).toBe('POST');
  expect(JSON.parse(calls[1].body)).toEqual({ title: 'Write report' });
  expect(calls[2].url).toBe('/api/tasks/t1');
  expect(calls[2].method).toBe('PUT');
  expect(JSON.parse(calls[2].body)).toEqual({ done: true });
  expect(calls[3].url).toBe('/api/tasks/t1');
  expect(calls[3].method).toBe('DELETE');
});

test('fetchMessages and checkAuth hit their endpoints', async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    return jsonResponse({ messages: [] }, 200);
  };
  await api.fetchMessages('sess-5');
  await api.checkAuth();
  expect(calls[0]).toBe('/api/sessions/sess-5/messages');
  expect(calls[1]).toBe('/api/check-auth');
});
