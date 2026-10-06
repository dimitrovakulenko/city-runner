import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({ ...base, testMatch: 'gent.public.ts', webServer: undefined, timeout: 90_000 });
