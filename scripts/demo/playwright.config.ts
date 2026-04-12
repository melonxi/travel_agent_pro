// scripts/demo/playwright.config.ts
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: 'demo-*.spec.ts',
  timeout: 300000,  // 5 minutes — LLM responses need time
  use: {
    baseURL: 'http://127.0.0.1:5173',
    video: { mode: 'on', size: { width: 1280, height: 720 } },
    screenshot: 'on',
  },
  projects: [
    {
      name: 'demo',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
