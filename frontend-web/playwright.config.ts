import { defineConfig } from "@playwright/test";

// Health checks target local test services even when the shell uses a proxy.
const localBypass = [process.env.NO_PROXY, process.env.no_proxy, "localhost", "127.0.0.1", "::1"].filter(Boolean).join(",");
process.env.NO_PROXY = localBypass;
process.env.no_proxy = localBypass;

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 60000,
  expect: { timeout: 15000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: "http://127.0.0.1:15910",
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "node scripts/test-backend.mjs research",
      url: "http://127.0.0.1:18906/health",
      reuseExistingServer: !process.env.CI,
      timeout: 90000,
    },
    {
      command: "node scripts/test-backend.mjs review",
      url: "http://127.0.0.1:18907/api/health",
      reuseExistingServer: !process.env.CI,
      timeout: 90000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 15910",
      port: 15910,
      reuseExistingServer: !process.env.CI,
      env: {
        AGENT_BRIDGE_PORT: "18906",
        REVIEW_PORT: "18907",
        RESOURCE_MONITOR_PORT: "18905",
      },
    },
  ],
});
