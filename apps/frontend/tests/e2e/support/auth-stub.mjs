import { createServer } from "node:http";

const port = Number(process.env.PLAYWRIGHT_AUTH_STUB_PORT);
const expectedSessionCookie = process.env.PLAYWRIGHT_E2E_SESSION_COOKIE;

if (!Number.isSafeInteger(port) || port < 1 || port > 65535 || !expectedSessionCookie) {
  throw new Error("Playwright auth stub configuration is invalid");
}

const server = createServer((request, response) => {
  const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");

  if (request.method === "GET" && requestUrl.pathname === "/__health") {
    response.writeHead(200, { "content-type": "text/plain", "cache-control": "no-store" });
    response.end("ok");
    return;
  }

  if (request.method !== "GET" || requestUrl.pathname !== "/api/v1/auth/me") {
    response.writeHead(404, { "content-type": "application/json", "cache-control": "no-store" });
    response.end(JSON.stringify({ detail: "Not found" }));
    return;
  }

  const hasTestSession = (request.headers.cookie ?? "")
    .split(";")
    .some((cookie) => cookie.trim() === `frame_session=${expectedSessionCookie}`);

  response.writeHead(hasTestSession ? 200 : 401, {
    "content-type": "application/json",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(hasTestSession ? {} : { detail: "Unauthorized" }));
});

server.on("error", () => {
  process.exitCode = 1;
});

server.listen(port, "127.0.0.1");
