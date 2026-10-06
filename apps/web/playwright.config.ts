import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests', testMatch: '*.spec.ts', workers: 1, timeout: 45_000,
  use: { baseURL: 'http://127.0.0.1:5174', channel: 'chrome', headless: true, launchOptions: { args: ['--enable-unsafe-swiftshader'] }, viewport: { width: 1440, height: 960 }, trace: 'retain-on-failure' },
  webServer: [
    { command: 'python tests/serve-fixture.py', url: 'http://127.0.0.1:8004/docs', env: { WEB_TEST_API_PORT: '8004', WEB_PREVIEW_EMPTY: '0', WEB_PREVIEW_GENT_OSM: '', WEB_TEST_LOGIN: '' }, timeout: 60_000 },
    { command: process.env.WEB_TEST_PRODUCTION ? 'npm run preview -- --port 5174' : 'npm run dev -- --port 5174', url: 'http://127.0.0.1:5174', env: { API_PROXY_TARGET: 'http://127.0.0.1:8004', VITE_DEV_TEST_LOGIN: '' }, timeout: 30_000 },
  ],
});
