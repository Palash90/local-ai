import React from 'react';
import { test, expect } from './fixtures.js';
import Message from '../src/components/Message';
import ChatArea from '../src/components/ChatArea';
import ImageLightbox from '../src/components/ImageLightbox';

// ---------- Message: basic rendering ----------

test('renders a plain user message', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'user', content: 'hello there' }} onImageOpen={() => {}} />);
  await expect(component).toHaveClass(/user/);
  await expect(component).toContainText('hello there');
});

test('renders a bot message with markdown', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'assistant', content: '**bold** and `code`' }} onImageOpen={() => {}} />);
  await expect(component.locator('.msg-content strong')).toHaveText('bold');
  await expect(component.locator('.msg-content code')).toHaveText('code');
});

test('renders code blocks with copy button', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'assistant', content: '```python\nprint(1)\n```' }} onImageOpen={() => {}} />);
  await expect(component.locator('.code-block')).toHaveCount(1);
  await expect(component.locator('.copy-code-btn')).toHaveText('Copy');
  await expect(component.locator('.code-block code.language-python')).toHaveText('print(1)');
});

test('renders file chip for FILE link', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'user', content: '[FILE: /uploads/abc123.py](script.py)' }} onImageOpen={() => {}} />);
  await expect(component.locator('.file-chip')).toHaveCount(1);
  await expect(component.locator('.file-chip')).toHaveAttribute('href', '/uploads/abc123.py');
  await expect(component.locator('.file-chip')).toHaveAttribute('download', 'script.py');
  await expect(component.locator('.file-chip-name')).toHaveText('script.py');
});

test('system and tool messages render nothing', async ({ mount }) => {
  const component = await mount(
    <div>
      <Message msg={{ role: 'system', content: 'hidden' }} onImageOpen={() => {}} />
      <Message msg={{ role: 'tool', content: 'hidden' }} onImageOpen={() => {}} />
    </div>
  );
  await expect(component.locator('.msg')).toHaveCount(0);
});

test('assistant message with only tool_calls renders nothing', async ({ mount }) => {
  const component = await mount(<div><Message msg={{ role: 'assistant', content: '', tool_calls: [{ id: 'x' }] }} onImageOpen={() => {}} /></div>);
  await expect(component.locator('.msg')).toHaveCount(0);
});

test('empty user message renders nothing', async ({ mount }) => {
  const component = await mount(<div><Message msg={{ role: 'user', content: '' }} onImageOpen={() => {}} /></div>);
  await expect(component.locator('.msg')).toHaveCount(0);
});

// ---------- Message: tool badges ----------

test('shows tool badges for web_search and generate_image', async ({ mount }) => {
  const component = await mount(
    <Message msg={{ role: 'assistant', content: 'result', _tools_used: ['web_search', 'generate_image'], _image_model: 'flux' }} onImageOpen={() => {}} />
  );
  await expect(component.locator('.tool-badge.search')).toHaveText('Web Search');
  await expect(component.locator('.tool-badge.image')).toHaveText('Image Gen (flux)');
});

test('shows fetch page badge with hostname and opens modal', async ({ mount }) => {
  const component = await mount(
    <Message
      msg={{
        role: 'assistant',
        content: 'result',
        _tools_used: ['fetch_page'],
        _search_details: [{ tool: 'fetch_page', url: 'https://example.com/page', title: 'Example Page', content: 'some body text' }],
      }}
      onImageOpen={() => {}}
    />
  );
  await expect(component.locator('.tool-badge.fetch')).toContainText('Fetched Page · example.com');
  await component.locator('.fetch-info-btn').click();
  await expect(component.locator('.page-modal-title')).toHaveText('Example Page');
  await expect(component.locator('.page-modal-content')).toContainText('some body text');
  await component.locator('.page-modal-close').click();
  await expect(component.locator('.page-modal')).toHaveCount(0);
});

test('fetch page modal shows error state', async ({ mount }) => {
  const component = await mount(
    <Message
      msg={{
        role: 'assistant',
        content: 'result',
        _tools_used: ['fetch_page'],
        _search_details: [{ tool: 'fetch_page', url: 'https://example.com/x', error: 'Page could not be fetched' }],
      }}
      onImageOpen={() => {}}
    />
  );
  await component.locator('.fetch-info-btn').click();
  await expect(component.locator('.page-modal-body.error')).toContainText('Page could not be fetched');
});

// ---------- Message: search popup ----------

test('web search popup shows queries and results on hover', async ({ mount }) => {
  const component = await mount(
    <Message
      msg={{
        role: 'assistant',
        content: 'result',
        _tools_used: ['web_search'],
        _search_details: [{ query: 'cats', search_url: 'https://searxng.example/?q=cats', results: [{ url: 'https://a.com', title: 'Cat Facts' }] }],
      }}
      onImageOpen={() => {}}
    />
  );
  await component.locator('.tool-badge.search').hover();
  await expect(component.locator('.search-popup').first()).toContainText('cats');
  await expect(component.locator('.search-popup').first()).toContainText('Cat Facts');
  const link = component.locator('.search-popup a[href="https://a.com"]');
  await expect(link).toHaveAttribute('target', '_blank');
});

// ---------- Message: image generation ----------

test('shows generated image and calls onImageOpen', async ({ mount }) => {
  let opened = null;
  const component = await mount(
    <Message msg={{ role: 'assistant', content: '', _image_url: '/output/gen.png', _gen_prompt: 'a cat in space' }} onImageOpen={(u) => { opened = u; }} />
  );
  await expect(component.locator('.image-wrap img')).toHaveAttribute('src', '/output/gen.png');
  await expect(component.locator('.image-wrap img')).toHaveAttribute('alt', 'Generated');
  await expect(component).toContainText('Prompt: a cat in space');
  await component.locator('.image-wrap img').click();
  expect(opened).toBe('/output/gen.png');
});

// ---------- Message: reasoning ----------

test('renders reasoning block and toggles it', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'assistant', content: 'answer', _reasoning: 'step 1\nstep 2' }} onImageOpen={() => {}} />);
  await expect(component.locator('.reasoning-block')).not.toHaveAttribute('open', '');
  await component.locator('.reasoning-block summary').click();
  await expect(component.locator('.reasoning-block')).toHaveAttribute('open', '');
  await expect(component.locator('.reasoning-text')).toBeVisible();
  await expect(component.locator('.reasoning-text')).toContainText('step 1');
});

// ---------- Message: elapsed time ----------

test('formats elapsed time', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'assistant', content: 'answer', _elapsed_ms: 185000 }} onImageOpen={() => {}} />);
  await expect(component.locator('.msg-elapsed')).toContainText('3m 5s');
});

// ---------- Message: copy buttons ----------

test('copies text via clipboard', async ({ page, mount }) => {
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (t) => { window.__copiedText = t; } },
      configurable: true,
    });
  });
  const component = await mount(<Message msg={{ role: 'assistant', content: 'copy me' }} onImageOpen={() => {}} />);
  await component.locator('.copy-btn').click();
  await expect(component.locator('.copy-btn')).toHaveText('Copied!');
  const copied = await page.evaluate(() => window.__copiedText);
  expect(copied).toBe('copy me');
});

test('copies code block content', async ({ page, mount }) => {
  await page.evaluate(() => {
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (t) => { window.__copiedCode = t; } },
      configurable: true,
    });
  });
  const component = await mount(<Message msg={{ role: 'assistant', content: '```js\nconst a = 1;\n```' }} onImageOpen={() => {}} />);
  await component.locator('.copy-code-btn').click();
  await expect(component.locator('.copy-code-btn')).toHaveText('Copied!');
  const copied = await page.evaluate(() => window.__copiedCode);
  expect(copied).toBe('const a = 1;');
});

test('speak button calls the TTS endpoint', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async (url, opts) => ({
      json: async () => ({ audio: 'UklGRgAAAAA=', type: 'audio/wav' }),
      status: 200,
    });
    window.Audio = class {
      constructor() {}
      play() { return Promise.resolve(); }
      pause() {}
    };
  });
  const component = await mount(<Message msg={{ role: 'assistant', content: 'read me' }} onImageOpen={() => {}} />);
  await component.locator('.speak-btn').click();
  await expect(component.locator('.speak-btn')).toHaveClass(/speaking/);
});

// ---------- Message: share button ----------

test('share button is hidden without session context', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'assistant', content: 'hello' }} onImageOpen={() => {}} />);
  await expect(component.locator('.share-btn')).toHaveCount(0);
});

test('share button is hidden on user messages', async ({ mount }) => {
  const component = await mount(<Message msg={{ role: 'user', content: 'hi' }} sessionId="s1" msgIndex={0} onImageOpen={() => {}} />);
  await expect(component.locator('.share-btn')).toHaveCount(0);
});

test('share button shares an assistant message and shows the link', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async (url, opts) => {
      if (String(url) === '/api/shares' && (opts?.method || 'GET') === 'POST') {
        return { status: 200, json: async () => ({ token: 'abc123', url: '/s/abc123' }) };
      }
      return { status: 404, json: async () => ({}) };
    };
  });
  const component = await mount(
    <Message msg={{ role: 'assistant', content: 'shared text' }} sessionId="s1" msgIndex={1} onImageOpen={() => {}} />
  );
  await component.locator('.share-btn').click();
  await expect(component.locator('.share-modal')).toContainText('Message shared');
  await expect(component.locator('.share-modal-url')).toHaveValue(/\/s\/abc123/);
  await component.locator('.share-modal-close').click();
  await expect(component.locator('.share-modal')).toHaveCount(0);
});

test('share button surfaces a server error', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async (url, opts) => {
      if (String(url) === '/api/shares' && (opts?.method || 'GET') === 'POST') {
        return { status: 400, json: async () => ({ error: 'Only assistant messages can be shared' }) };
      }
      return { status: 404, json: async () => ({}) };
    };
  });
  const component = await mount(
    <Message msg={{ role: 'assistant', content: 'x' }} sessionId="s1" msgIndex={1} onImageOpen={() => {}} />
  );
  await component.locator('.share-btn').click();
  await expect(component.locator('.share-error')).toContainText('Only assistant messages can be shared');
});

// ---------- Message: pending ----------

test('pending message shows status and resolves', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async () => ({
      json: async () => ({ status: 'done', response: 'final answer', reasoning: 'thinking' }),
    });
  });
  let resolved = null;
  const component = await mount(
    <Message
      pending={{ sessionId: 's', status: 'working', message: 'Thinking...', taskId: 't1', reasoning: '' }}
      onImageOpen={() => {}}
      onResolved={(p, st) => { resolved = { p, st }; }}
    />
  );
  await expect(component.locator('.status-box')).toContainText('Thinking...');
  await expect.poll(() => resolved !== null).toBe(true);
  expect(resolved.p.taskId).toBe('t1');
  expect(resolved.st.status).toBe('done');
});

test('pending message triggers location_needed', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async () => ({
      json: async () => ({ status: 'working', message: 'location_needed' }),
    });
  });
  let needed = null;
  const component = await mount(
    <Message
      pending={{ sessionId: 's', status: 'working', message: 'Working...', taskId: 't9', reasoning: '' }}
      onImageOpen={() => {}}
      onLocationNeeded={(tid) => { needed = tid; }}
    />
  );
  await expect.poll(() => needed !== null).toBe(true);
  expect(needed).toBe('t9');
});

// ---------- ChatArea ----------

test('ChatArea renders messages and pending messages', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async () => ({ json: async () => ({ status: 'working', message: 'Working...' }) });
  });
  const messages = [
    { role: 'user', content: 'hi' },
    { role: 'assistant', content: 'hello' },
  ];
  const pending = { p1: { sessionId: 's1', status: 'working', message: 'Thinking...', taskId: 'p1', reasoning: '' } };
  const component = await mount(
    <ChatArea
      messages={messages}
      pendingMessages={pending}
      currentSessionId="s1"
      onImageOpen={() => {}}
      selectingRef={{ current: false }}
      onPendingResolved={() => {}}
      onLocationNeeded={() => {}}
    />
  );
  await expect(component.locator('.msg.user')).toHaveCount(1);
  await expect(component.locator('.msg.user')).toContainText('hi');
  await expect(component.locator('.msg.bot')).toHaveCount(2);
  await expect(component.locator('.status-box')).toContainText('Thinking...');
});

test('ChatArea filters pending messages to the current session', async ({ page, mount }) => {
  await page.evaluate(() => {
    window.fetch = async () => ({ json: async () => ({ status: 'working', message: 'Working...' }) });
  });
  const pending = {
    p1: { sessionId: 's1', status: 'working', message: 'for s1', taskId: 'p1', reasoning: '' },
    p2: { sessionId: 's2', status: 'working', message: 'for s2', taskId: 'p2', reasoning: '' },
  };
  const component = await mount(
    <ChatArea
      messages={[]}
      pendingMessages={pending}
      currentSessionId="s1"
      onImageOpen={() => {}}
      selectingRef={{ current: false }}
      onPendingResolved={() => {}}
      onLocationNeeded={() => {}}
    />
  );
  await expect(component.locator('.status-box')).toHaveCount(1);
  await expect(component.locator('.status-box')).toContainText('for s1');
});

// ---------- ImageLightbox ----------

test('ImageLightbox renders image and zoom label', async ({ mount }) => {
  const component = await mount(<ImageLightbox src="/output/gen.png" onClose={() => {}} />);
  await expect(component.locator('#fullscreen-img')).toHaveAttribute('src', '/output/gen.png');
  await expect(component.locator('#zoom-label')).toHaveText('100%');
});

test('ImageLightbox renders nothing without src', async ({ mount }) => {
  const component = await mount(<div><ImageLightbox src={null} onClose={() => {}} /></div>);
  await expect(component.locator('#image-overlay')).toHaveCount(0);
});

test('ImageLightbox zooms on wheel', async ({ page, mount }) => {
  const component = await mount(<ImageLightbox src="/output/gen.png" onClose={() => {}} />);
  await expect(component.locator('#zoom-label')).toHaveText('100%');
  // Let React flush passive effects (the wheel listener is attached in one)
  // before dispatching the synthetic event, otherwise the event can be lost.
  await page.evaluate(() => new Promise((r) => setTimeout(r, 0)));
  await page.locator('#image-overlay').dispatchEvent('wheel', { deltaY: -100 });
  await expect(component.locator('#zoom-label')).toHaveText('110%');
  await page.locator('#image-overlay').dispatchEvent('wheel', { deltaY: 100 });
  await expect(component.locator('#zoom-label')).toHaveText('100%');
});

test('ImageLightbox closes on Escape', async ({ page, mount }) => {
  let closed = 0;
  const component = await mount(<ImageLightbox src="/output/gen.png" onClose={() => { closed += 1; }} />);
  await page.locator('#image-overlay').press('Escape');
  await expect.poll(() => closed).toBeGreaterThan(0);
});

test('ImageLightbox closes on backdrop mousedown', async ({ page, mount }) => {
  let closed = 0;
  const component = await mount(<ImageLightbox src="/output/gen.png" onClose={() => { closed += 1; }} />);
  await page.locator('#image-overlay').evaluate((el) => {
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
  });
  await expect.poll(() => closed).toBeGreaterThan(0);
});
