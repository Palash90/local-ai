// Authentication is handled by nginx + Authentik SSO (the X-Authentik-*
// claim headers are injected upstream by nginx's auth_request). The browser
// never holds a token; on 401 nginx redirects to the SSO portal.
async function authFetch(url, options = {}) {
  options.headers = options.headers || {};
  const r = await fetch(url, options);
  if (r.status === 401) {
    window.dispatchEvent(new CustomEvent('auth:unauthorized'));
  }
  return r;
}

export async function logout() {
  window.location.assign('/outpost.goauthentik.io/sign_out?rd=/');
}

export async function checkAuth() {
  const r = await authFetch('/api/check-auth');
  return r.json();
}

export async function fetchSessions() {
  const r = await authFetch('/api/sessions');
  return r.json();
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

function localISOString() {
  const d = new Date()
  const tz = -d.getTimezoneOffset()
  const sign = tz >= 0 ? '+' : '-'
  const pad = n => String(Math.abs(n)).padStart(2, '0')
  return d.getFullYear() + '-' + pad(d.getMonth()+1) + '-' + pad(d.getDate()) + 'T' +
    pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) +
    sign + pad(Math.floor(Math.abs(tz)/60)) + ':' + pad(Math.abs(tz)%60)
}

export async function sendMessage(sessionId, message, image, audio, clientTimestamp, research, cpu) {
  const body = { session_id: sessionId, message, client_timestamp: clientTimestamp || localISOString() };
  if (research) body.research = true;
  if (cpu) body.cpu = true;
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

export async function sendLocation(latitude, longitude, taskId) {
  await fetch('/api/location', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ latitude, longitude, task_id: taskId }),
  });
}

export async function denyLocation(taskId) {
  await fetch('/api/location', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ denied: true, task_id: taskId }),
  });
}

export async function speak(text, voice) {
  const r = await authFetch('/api/tts', {
    method: 'POST',
    body: JSON.stringify({ text, voice: voice || undefined }),
  });
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

export async function uploadImage(dataB64, ext = 'jpg') {
  const r = await authFetch('/api/upload-image', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ data: dataB64, ext }),
  });
  return r.json();
}

export async function fetchTasks() {
  const r = await authFetch('/api/tasks');
  return r.json();
}

export async function createTask(data) {
  const r = await authFetch('/api/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return r.json();
}

export async function updateTask(taskId, data) {
  const r = await authFetch(`/api/tasks/${taskId}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return r.json();
}

export async function deleteTask(taskId) {
  await authFetch(`/api/tasks/${taskId}`, { method: 'DELETE' });
}

export async function shareMessage(sessionId, msgIndex) {
  const r = await authFetch('/api/shares', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId, msg_index: msgIndex }),
  });
  return r.json();
}

export async function listShares() {
  const r = await authFetch('/api/shares');
  return r.json();
}

export async function revokeShare(token) {
  const r = await authFetch(`/api/shares/${token}`, { method: 'DELETE' });
  return r.json();
}

export async function fetchPublicShare(token) {
  const r = await fetch(`/api/public/share/${encodeURIComponent(token)}`);
  return r.json();
}
