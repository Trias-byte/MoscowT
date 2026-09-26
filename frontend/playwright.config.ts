import { defineConfig, devices } from '@playwright/test';
const externalBaseURL = process.env.PLAYWRIGHT_BASE_URL;
export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30000,
  expect: { timeout: 10000 },
  reporter: 'list',
  use: {
    baseURL: externalBaseURL || 'http://127.0.0.1:5173',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure',
    launchOptions: {
      ...(process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
        ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
        : {}),
      args: ['--no-sandbox', '--enable-unsafe-swiftshader'],
    },
  },
  webServer: externalBaseURL
    ? []
    : [
        {
          command: 'uv run --directory ../backend moscowt serve',
          url: 'http://127.0.0.1:8000/health/ready',
          reuseExistingServer: !process.env.CI,
        },
        {
          command: 'npm run dev -- --host 127.0.0.1',
          url: 'http://127.0.0.1:5173',
          reuseExistingServer: !process.env.CI,
          env: { VITE_DEMO: 'false' },
        },
      ],
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
