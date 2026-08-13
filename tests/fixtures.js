import fs from 'node:fs';
import path from 'node:path';
import { test as base, expect } from '@playwright/experimental-ct-react';

const RAW_DIR = path.resolve('coverage-js', 'raw');
const ROOT = process.cwd();

fs.mkdirSync(RAW_DIR, { recursive: true });

let seq = 0;

// --- minimal sourcemap (VLQ) decoder, generated-line -> first (source, line) ---

const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';

function decodeVlq(str) {
  const out = [];
  let shift = 0;
  let value = 0;
  for (const ch of str) {
    const digit = B64.indexOf(ch);
    if (digit === -1) return out;
    value |= (digit & 31) << shift;
    if (digit & 32) {
      shift += 5;
    } else {
      out.push(value & 1 ? -(value >> 1) : value >> 1);
      shift = 0;
      value = 0;
    }
  }
  return out;
}

function decodeMappings(mappings) {
  // lineMaps[genLine] = [{ col, src, line }] per generated line (0-based genLine)
  const lineMaps = [];
  let srcIdx = 0;
  let origLine = 0;
  let segs = [];
  for (const group of mappings.split(';')) {
    if (group) {
      let genCol = 0;
      for (const segment of group.split(',')) {
        const fields = decodeVlq(segment);
        if (fields.length < 4) continue;
        genCol += fields[0];
        srcIdx += fields[1];
        origLine += fields[2];
        segs.push({ col: genCol, src: srcIdx, line: origLine });
      }
    }
    lineMaps.push(segs);
    segs = [];
  }
  return lineMaps;
}

// byte offset -> 0-based line number
function lineOf(text, offset) {
  let line = 0;
  for (let i = 0; i < offset && i < text.length; i++) {
    if (text.charCodeAt(i) === 10) line++;
  }
  return line;
}

function toRepoRelative(map, srcIdx, assetUrl) {
  const src = map.sources[srcIdx];
  if (!src) return null;
  let url;
  try {
    url = new URL(src, assetUrl);
  } catch {
    return null;
  }
  const p = url.pathname;
  if (p.startsWith('/node_modules/')) return null;
  if (p.startsWith('/@fs/')) {
    return decodeURIComponent(p.slice('/@fs/'.length));
  }
  if (p.startsWith('/src/')) {
    return path.relative(ROOT, path.join(ROOT, p.slice(1)));
  }
  return p;
}

const MAP_CACHE = new Map(); // assetUrl -> { sources, lineMaps } | null

export const test = base.extend({
  page: async ({ page }, use) => {
    if (page.coverage) {
      await page.coverage.startJSCoverage();
    }
    await use(page);
    if (page.coverage) {
      try {
        const entries = await page.coverage.stopJSCoverage();
        const perFile = new Map(); // file -> Set<line>
        for (const entry of entries) {
          const text = entry.source || entry.text || '';
          if (!text || !entry.url) continue;
          const ranges = [];
          if (entry.functions) {
            for (const fn of entry.functions) {
              for (const r of fn.ranges || []) ranges.push(r);
            }
          }
          ranges.push(...(entry.ranges || []));
          if (!ranges.length) continue;

          let map = MAP_CACHE.get(entry.url);
          if (map === undefined) {
            map = null;
            try {
              const j = await page.evaluate(async (u) => {
                const r = await fetch(u + '.map');
                if (!r.ok) return null;
                return r.json();
              }, entry.url);
              if (j && j.mappings) map = { sources: j.sources || [], lineMaps: decodeMappings(j.mappings) };
            } catch {
              map = null;
            }
            MAP_CACHE.set(entry.url, map);
          }
          if (!map) continue;

          for (const r of ranges) {
            const start = Math.max(0, Math.min(r.startOffset ?? r.start, text.length));
            const end = Math.max(start, Math.min(r.endOffset ?? r.end, text.length));
            const l1 = lineOf(text, start);
            const l2 = lineOf(text, Math.max(end - 1, start));
            for (let gl = l1; gl <= l2; gl++) {
              const segs = map.lineMaps[gl];
              if (!segs || !segs.length) continue;
              let best = segs[0];
              for (const s of segs) if (s.col < best.col) best = s;
              const file = toRepoRelative(map, best.src, entry.url);
              if (!file) continue;
              let set = perFile.get(file);
              if (!set) {
                set = new Set();
                perFile.set(file, set);
              }
              set.add(best.line);
            }
          }
        }
        const out = [];
        for (const [file, lines] of perFile) {
          out.push({ file, lines: [...lines].sort((a, b) => a - b) });
        }
        if (out.length) {
          const f = path.join(RAW_DIR, `${process.pid}-${++seq}.json`);
          fs.writeFileSync(f, JSON.stringify(out));
        }
      } catch (e) {
        console.error('[coverage] collection failed:', e.message);
      }
    }
  },
});

export { expect };
