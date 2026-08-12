import { test, expect } from './fixtures.js';
import { downloadFile } from '../src/utils.js';

let events;

function stubGlobals() {
  events = [];
  const fakeBlob = new Blob(['pdfdata'], { type: 'application/pdf' });
  globalThis.fetch = async () => ({ blob: async () => fakeBlob });
  const fakeUrl = 'blob:fake-1';
  globalThis.URL.createObjectURL = (blob) => {
    events.push('createObjectURL:' + blob.type);
    return fakeUrl;
  };
  globalThis.URL.revokeObjectURL = (u) => events.push('revoke:' + u);
  const body = {
    appendChild: (node) => { events.push('append'); },
    removeChild: (node) => { events.push('remove'); },
  };
  globalThis.document = {
    createElement: (tag) => {
      const el = {
        tagName: tag.toUpperCase(),
        click: () => { events.push('click:' + (el.download || '')); },
      };
      return el;
    },
    body,
  };
  globalThis.window = {
    location: { origin: 'http://localhost:3000' },
    open: (url) => { events.push('window.open:' + url); return null; },
  };
}

test.beforeEach(() => {
  stubGlobals();
});

test('downloadFile fetches, creates blob anchor and clicks it', async () => {
  await downloadFile('/output/report.pdf');
  expect(events).toEqual([
    'createObjectURL:application/pdf',
    'append',
    'click:report.pdf',
    'remove',
    'revoke:blob:fake-1',
  ]);
});

test('downloadFile opens new window when fetch fails', async () => {
  globalThis.fetch = async () => { throw new Error('boom'); };
  await downloadFile('/output/missing.pdf');
  expect(events).toContain('window.open:http://localhost:3000/output/missing.pdf');
});

test('downloadFile resolves relative urls against origin', async () => {
  let fetched;
  globalThis.fetch = async (url) => {
    fetched = url;
    return { blob: async () => new Blob(['x']) };
  };
  await downloadFile('/output/file.pdf');
  expect(fetched).toBe('http://localhost:3000/output/file.pdf');
});

test('downloadFile passes absolute urls through unchanged', async () => {
  let fetched;
  globalThis.fetch = async (url) => {
    fetched = url;
    return { blob: async () => new Blob(['x']) };
  };
  await downloadFile('http://other.example/out/x.pdf');
  expect(fetched).toBe('http://other.example/out/x.pdf');
});

test('downloadFile uses fallback name when url has no filename', async () => {
  globalThis.fetch = async () => ({ blob: async () => new Blob(['x']) });
  await downloadFile('http://localhost:3000/output/', 'fallback.txt');
  expect(events).toContain('click:fallback.txt');
});

test('downloadFile uses last url segment as filename for data urls', async () => {
  globalThis.fetch = async () => ({ blob: async () => new Blob(['x']) });
  await downloadFile('data:application/pdf;base64,SGVsbG8=');
  expect(events).toContain('click:pdf;base64,SGVsbG8=');
});
