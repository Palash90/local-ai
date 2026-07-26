const TOKEN_KEY = 'auth_token';

function authToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}

async function authFetch(url, options = {}) {
  options.headers = options.headers || {};
  const token = authToken();
  if (token) {
    options.headers['X-Auth-Token'] = token;
  }
  const r = await fetch(url, options);
  return r;
}

export async function login(username, password) {
  console.log('[api.login] POST /api/login', username);
  const r = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  });
  const data = await r.json();
  console.log('[api.login] response', data);
  if (data.token) {
    localStorage.setItem(TOKEN_KEY, data.token);
    console.log('[api.login] token stored in localStorage');
  }
  return data;
}

export async function logout() {
  await authFetch('/api/logout', { method: 'POST' }).catch(() => {});
  localStorage.removeItem(TOKEN_KEY);
}

export async function checkAuth() {
  const r = await authFetch('/api/check-auth');
  return r.json();
}

export async function fetchSessions() {
  const token = localStorage.getItem('opencode_token')
  const res = await fetch('/api/sessions', {
    headers: {
      'X-Auth-Token': token
    }
  })
  if (res.status === 401) {
    console.log("Fetching session failed");
  }
  return res.json();
}

export async function fetchMessages(sessionId) {
  const r = await authFetch(`/api/sessions/${sessionId}/messages`);
  return r.json();
}

export async function createSession() {
  const r = await authFetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: 'New Chat' }),
  });
  return r.json();
}

export async function deleteSession(sessionId) {
  await authFetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
}

export async function renameSession(sessionId, name) {
  await authFetch(`/api/sessions/${sessionId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  });
}

export async function sendMessage(sessionId, message, image, audio) {
  const body = { session_id: sessionId, message };
  if (image) body.image = image;
  if (audio) body.audio = audio;
  const r = await authFetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (r.status === 503) {
    const err = await r.json();
    throw new Error(err.error || 'Server is busy');
  }
  return r.json();
}

export async function getTaskStatus(taskId) {
  const r = await fetch(`/api/status/${taskId}`);
  return r.json();
}

export async function getModelStatus() {
  const r = await fetch('/api/model-status');
  return r.json();
}

export async function extractFile(name, dataB64) {
  const r = await authFetch('/api/extract-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, data: dataB64 }),
  });
  return r.json();
}
