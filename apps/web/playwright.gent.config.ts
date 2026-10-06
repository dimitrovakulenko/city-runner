import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({ ...base, outputDir: 'test-results/gent', use: { ...base.use, baseURL: 'http://127.0.0.1:5173' }, testMatch: 'gent.public.ts', webServer: undefined, timeout: 90_000 });
