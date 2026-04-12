import { defineConfig, devices } from '@playwright/test';

const captureScript = 'scripts/failure-analysis/capture_screenshots.ts';
const normalizedArgs = new Set(process.argv.map((arg) => arg.replace(/\\/g, '/')));
const includeCaptureScript =
  normalizedArgs.has(captureScript) ||
  normalizedArgs.has(`./${captureScript}`);

export default defineConfig({
  testDir: '.',
  testMatch: includeCaptureScript ? ['e2e-test.spec.ts', captureScript] : 'e2e-test.spec.ts',
  timeout: 180000,
  use: {
    baseURL: 'http://127.0.0.1:5173',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
