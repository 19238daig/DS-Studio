import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 45000,
  expect: { timeout: 12000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: 'http://127.0.0.1:18180',
    browserName: 'chromium',
    channel: 'msedge',
    headless: true,
    reducedMotion: 'reduce',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command:
      'powershell -NoProfile -Command "& ../.venv/Scripts/python.exe ../scripts/web_acceptance.py --workspace (Join-Path ../.runtime/react-migration-20261030 ([guid]::NewGuid().ToString())) --port 18180"',
    url: 'http://127.0.0.1:18180/api/v1/health',
    reuseExistingServer: false,
    timeout: 90000,
  },
});
