import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/live',
  workers: 1,
  timeout: 45000,
  reporter: 'list',
  outputDir: 'test-results/live',
  use: {
    baseURL: 'http://127.0.0.1:8000',
    browserName: 'chromium',
    channel: 'msedge',
    headless: true,
    viewport: { width: 1440, height: 1000 },
    reducedMotion: 'reduce',
  },
});
