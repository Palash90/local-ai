import fs from 'node:fs';
import path from 'node:path';

const RAW_DIR = path.resolve('coverage-js', 'raw');
const ROOT = process.cwd();

function isReportable(url) {
  if (!url || url.startsWith('data:') || url.startsWith('blob:')) return false;
  const q = url.split('?')[0];
  if (q.startsWith('/@')) return false; // vite internals
  if (q.includes('/node_modules/')) return false;
  if (q.startsWith('/dist/')) return false;
  if (q.startsWith('/tests/')) return false;
  return true;
}

// byte offset -> 1-based line number via binary search over newline offsets
function makeLineIndex(text) {
  const nl = [-1];
  for (let i = 0; i < text.length; i++) {
    if (text.charCodeAt(i) === 10) nl.push(i);
  }
  nl.push(text.length + 1);
  return (offset) => {
    let lo = 0;
    let hi = nl.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (nl[mid] < offset) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  };
}

const merged = new Map(); // url -> { text, lines: Set<number> }

for (const file of fs.readdirSync(RAW_DIR)) {
  if (!file.endsWith('.json')) continue;
  let entries;
  try {
    entries = JSON.parse(fs.readFileSync(path.join(RAW_DIR, file), 'utf8'));
  } catch {
    continue;
  }
  for (const entry of entries) {
    if (!isReportable(entry.url)) continue;
    const text = entry.text || '';
    let rec = merged.get(entry.url);
    if (!rec) {
      rec = { text, lines: new Set(), lineIndex: makeLineIndex(text) };
      merged.set(entry.url, rec);
    }
    const startLine = rec.lineIndex(0);
    for (const r of entry.ranges || []) {
      const start = Math.max(0, Math.min(r.start, text.length));
      const end = Math.max(start, Math.min(r.end, text.length));
      const l1 = rec.lineIndex(start);
      const l2 = rec.lineIndex(Math.max(end - 1, start));
      for (let l = l1; l <= l2; l++) rec.lines.add(l);
    }
  }
}

const rows = [];
for (const [url, rec] of merged) {
  const srcPath = url.split('?')[0].replace(/^\//, '');
  const fsPath = path.join(ROOT, srcPath);
  let total = 0;
  if (fs.existsSync(fsPath)) {
    total = fs.readFileSync(fsPath, 'utf8').split('\n').length;
  } else {
    total = Math.max(...rec.lines, 1);
  }
  const hit = rec.lines.size;
  rows.push({ file: srcPath, hit, total, pct: total ? (hit / total) * 100 : 100 });
}

rows.sort((a, b) => a.pct - b.pct);

const allHit = rows.reduce((s, r) => s + r.hit, 0);
const allTotal = rows.reduce((s, r) => s + r.total, 0);

console.log('JS test coverage (V8, line-based, Chromium)');
console.log('');
console.log('File                                        Lines   Hit    Cover   Missing');
console.log('---------------------------------------------------------------------------');
for (const r of rows) {
  const missing = r.total - r.hit;
  const name = r.file.length > 42 ? '...' + r.file.slice(-39) : r.file;
  console.log(
    `${name.padEnd(43)} ${String(r.total).padStart(5)} ${String(r.hit).padStart(5)}  ${r.pct
      .toFixed(0)
      .padStart(4)}%   ${r.total > r.hit ? String(missing) : ''}`,
  );
}
console.log('---------------------------------------------------------------------------');
console.log(
  `TOTAL                                        ${String(allTotal).padStart(5)} ${String(allHit).padStart(5)}  ${(allHit / allTotal * 100).toFixed(0)}%`,
);
