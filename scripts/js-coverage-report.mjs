import fs from 'node:fs';
import path from 'node:path';

const RAW_DIR = path.resolve('coverage-js', 'raw');
const ROOT = process.cwd();

const merged = new Map(); // file -> Set<1-based line>

for (const file of fs.readdirSync(RAW_DIR)) {
  if (!file.endsWith('.json')) continue;
  let entries;
  try {
    entries = JSON.parse(fs.readFileSync(path.join(RAW_DIR, file), 'utf8'));
  } catch {
    continue;
  }
  if (!Array.isArray(entries)) continue;
  for (const e of entries) {
    if (!e.file) continue;
    if (e.file.startsWith('/node_modules/') || e.file.includes('/node_modules/')) continue;
    if (!e.file.startsWith('src/')) continue;
    let set = merged.get(e.file);
    if (!set) {
      set = new Set();
      merged.set(e.file, set);
    }
    for (const l of e.lines || []) set.add(l + 1); // sourcemap lines are 0-based
  }
}

const rows = [];
for (const [file, hitLines] of merged) {
  const fsPath = path.join(ROOT, file);
  if (!fs.existsSync(fsPath)) continue;
  const total = fs.readFileSync(fsPath, 'utf8').split('\n').length;
  const hit = [...hitLines].filter((l) => l >= 1 && l <= total).length;
  rows.push({ file, hit, total, pct: total ? (hit / total) * 100 : 100 });
}

rows.sort((a, b) => a.pct - b.pct);

const allHit = rows.reduce((s, r) => s + r.hit, 0);
const allTotal = rows.reduce((s, r) => s + r.total, 0);

console.log('JS test coverage (V8, source-level, Chromium)');
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
