import { defineConfig } from "@playwright/test";

const port = Number(process.env.PLAYWRIGHT_PORT ?? "3000");
if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
  throw new Error("PLAYWRIGHT_PORT must be a valid TCP port");
}
const authStubPort = Number(process.env.PLAYWRIGHT_AUTH_STUB_PORT ?? "43188");
if (!Number.isSafeInteger(authStubPort) || authStubPort < 1 || authStubPort > 65535 || authStubPort === port) {
  throw new Error("PLAYWRIGHT_AUTH_STUB_PORT must be a valid port distinct from PLAYWRIGHT_PORT");
}
const externalBaseUrl = process.env.PLAYWRIGHT_BASE_URL;
const baseURL = externalBaseUrl ?? `http://127.0.0.1:${port}`;
const authSessionCookieValue = "playwright-e2e-only";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  reporter: "line",
  use: {
    baseURL,
    ...(externalBaseUrl
      ? {}
      : {
          storageState: {
            cookies: [
              {
                name: "frame_session",
                value: authSessionCookieValue,
                domain: "127.0.0.1",
                path: "/",
                expires: -1,
                httpOnly: true,
                secure: false,
                sameSite: "Lax" as const,
              },
            ],
            origins: [],
          },
        }),
    trace: "retain-on-failure",
  },
  webServer: externalBaseUrl
    ? undefined
    : [
        {
          command: "node tests/e2e/support/auth-stub.mjs",
          url: `http://127.0.0.1:${authStubPort}/__health`,
          reuseExistingServer: false,
          timeout: 30_000,
          env: {
            PLAYWRIGHT_AUTH_STUB_PORT: String(authStubPort),
            PLAYWRIGHT_E2E_SESSION_COOKIE: authSessionCookieValue,
          },
        },
        {
          command: `npm run dev -- --hostname 127.0.0.1 --port ${port}`,
          url: `http://127.0.0.1:${port}`,
          reuseExistingServer: false,
          timeout: 120_000,
          env: {
            BACKEND_INTERNAL_URL: `http://127.0.0.1:${authStubPort}`,
            DATABASE_URL: "",
            TEST_DATABASE_URL: "",
          },
        },
      ],
});
