// Playwright config for the bvphoenix frontend.
//
// Goals:
//   - Hermetic by default: ``pnpm playwright test`` boots ``next start``
//     locally and runs the specs against it with mocked backend (the
//     specs in ``e2e/`` use ``page.route`` to stub the FastAPI surface).
//   - Real-backend mode for nightly runs: set ``E2E_USE_REAL_BACKEND=1``
//     and ``E2E_BASE_URL=http://localhost:3000`` (or the deployed env)
//     to skip the local dev server and the route mocks.
//
// Install (one-time, not run by Claude per repo policy):
//   pnpm add -D @playwright/test
//   pnpm exec playwright install --with-deps chromium
//
// Add to ``package.json`` scripts (already wired in this commit):
//   "test:e2e": "playwright test"

import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.PORT ?? 3100);
const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`;
const USE_REAL_BACKEND = process.env.E2E_USE_REAL_BACKEND === "1";

export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.spec\.ts$/,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [["github"], ["list"]]
    : [["list"], ["html", { outputFolder: "playwright/reports/html", open: "never" }]],
  timeout: 30_000,
  expect: { timeout: 5_000 },

  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Spin up ``next start`` for hermetic runs. When pointing at a
  // shared environment (``E2E_BASE_URL`` set) skip the web server
  // entirely.
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `pnpm next start --port ${PORT}`,
        url: BASE_URL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        env: {
          // In mock mode the API base never gets hit, but the client
          // still constructs the URL so it must parse cleanly.
          NEXT_PUBLIC_API_BASE_URL: USE_REAL_BACKEND
            ? (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000")
            : "http://localhost:8000",
        },
      },
});
