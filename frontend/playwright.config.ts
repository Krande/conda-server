import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright config for public-facing smoke tests.
 *
 * The webServer launches the backend via pixi (uvicorn on :8001 with
 * in-memory SQLite) and the same FastAPI process serves the built SPA.
 * Tests run against that single origin, so there's no CORS/proxy to
 * keep straight and assertions can navigate the real application.
 *
 * We intentionally don't drive the OIDC login flow here — it depends on
 * a live identity provider, which isn't guaranteed in every environment. Coverage
 * is limited to anon-readable surface (home, search, public channels,
 * NotFound). Once we have a mock OIDC or a test-only auth bypass, we can
 * add admin-flow tests.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? "dot" : "list",
  use: {
    baseURL: "http://127.0.0.1:8001",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // Run from the repo root because pixi resolves its manifest there.
    command: "cd .. && pixi run e2e-server",
    url: "http://127.0.0.1:8001/health",
    timeout: 120_000,
    reuseExistingServer: !process.env.CI,
    stdout: "pipe",
    stderr: "pipe",
  },
});
