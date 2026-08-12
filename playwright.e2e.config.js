import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: 'tests/e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  timeout: 90000,
  reporter: process.env.CI ? 'dot' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:3099',
    trace: 'on-first-retry',
    actionTimeout: 20000,
    headless: false,
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'npm run build && python3 tests/e2e/backend.py',
    url: 'http://127.0.0.1:3099/api/check-auth',
    reuseExistingServer: !process.env.CI,
    timeout: 180000,
  },
});
