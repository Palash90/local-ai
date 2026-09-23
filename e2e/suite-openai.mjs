/** OpenAI lane live: auth matrix + models + a real completion.
 *
 * Uses --openai-key-file (600-perms file, never logged). Asserts 401s for
 * missing/bad keys, a model list containing the GPU model, and one
 * non-streaming chat completion with real content.
 */
export const name = 'openai';
export const long = false;

const BASE = 'http://127.0.0.1:3001';

async function post(path, body, key) {
  const r = await fetch(BASE + path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(key ? { Authorization: `Bearer ${key}` } : {}),
    },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(300000),
  });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}

async function get(path, key) {
  const r = await fetch(BASE + path, {
    headers: { ...(key ? { Authorization: `Bearer ${key}` } : {}) },
    signal: AbortSignal.timeout(30000),
  });
  return { status: r.status, json: await r.json().catch(() => ({})) };
}

export async function run(page, ctx, opts) {
  if (!opts.hasOpenaiKey) {
    ctx.step('openai lane skipped (no key)', true, '');
    return;
  }
  const key = opts.openaiKey;

  const noAuth = await get('/v1/models', '');
  ctx.step('models without key → 401', noAuth.status === 401, `${noAuth.status}`);
  const badKey = await get('/v1/models', 'wrong-key');
  ctx.step('models with bad key → 401', badKey.status === 401, `${badKey.status}`);

  const models = await get('/v1/models', key);
  const ids = (models.json.data || []).map(m => m.id);
  ctx.step('models list answers', models.status === 200 && ids.length > 0, ids.join(','));

  // ids[0] is the tiny E2B judge model (often empty-handed on chat) —
  // exercise the real GPU chat model instead.
  const chatModel = ids.find(id => /e4b/i.test(id)) || ids[0];
  const chat = await post('/v1/chat/completions', {
    model: chatModel,
    messages: [{ role: 'user', content: 'e2e openai probe: reply with exactly OPENAI-OK' }],
    max_tokens: 256,
    temperature: 0,
    stream: false,
  }, key);
  const text = chat.json.choices?.[0]?.message?.content || '';
  ctx.step('completion returns content', chat.status === 200 && text.includes('OPENAI-OK'), `${chat.status} ${text.slice(0, 60)}`);
  await page.screenshot({ path: 'reports/shot-openai.png' }).catch(() => {});
}
