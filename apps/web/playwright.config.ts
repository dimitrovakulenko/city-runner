import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests', testMatch: '*.spec.ts', workers: 1, timeout: 45_000,
  use: { baseURL: 'http://127.0.0.1:5173', channel: 'chrome', headless: true, launchOptions: { args: ['--enable-unsafe-swiftshader'] }, viewport: { width: 1440, height: 960 }, trace: 'retain-on-failure' },
  webServer: [
    { command: 'python tests/serve-fixture.py', url: 'http://127.0.0.1:8003/docs', timeout: 60_000 },
    { command: process.env.WEB_TEST_PRODUCTION ? 'npm run preview' : 'npm run dev', url: 'http://127.0.0.1:5173', env: { API_PROXY_TARGET: 'http://127.0.0.1:8003' }, timeout: 30_000 },
  ],
});
