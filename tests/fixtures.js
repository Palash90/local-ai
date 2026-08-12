import fs from 'node:fs';
import path from 'node:path';
import { test as base, expect } from '@playwright/experimental-ct-react';

const RAW_DIR = path.resolve('coverage-js', 'raw');
fs.mkdirSync(RAW_DIR, { recursive: true });

let seq = 0;

export const test = base.extend({
  page: async ({ page }, use) => {
    if (page.coverage) {
      await page.coverage.startJSCoverage();
    }
    await use(page);
    if (page.coverage) {
      try {
        const entries = await page.coverage.stopJSCoverage();
        if (entries.length) {
          const file = path.join(RAW_DIR, `${process.pid}-${++seq}.json`);
          fs.writeFileSync(file, JSON.stringify(entries));
        }
      } catch {
        // coverage collection must never fail the test
      }
    }
  },
});

export { expect };
