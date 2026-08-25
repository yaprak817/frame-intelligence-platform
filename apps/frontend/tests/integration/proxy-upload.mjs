import http from "node:http";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import process from "node:process";
import {
  forceTerminateProcess,
  processExited,
  terminateProcess,
  waitForPortRelease,
  waitForProcessExit,
} from "../../scripts/process-cleanup.mjs";

const uploadBytes = Number(process.env.PROXY_TEST_UPLOAD_BYTES ?? 13 * 1024 * 1024);
const frontendPort = Number(process.env.PROXY_TEST_FRONTEND_PORT ?? 3210);
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const bounded = (promise, label, ms = 10_000) => Promise.race([
  promise,
  delay(ms).then(() => { throw new Error(`${label} timed out`); }),
]);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
};

let firstUpstreamByteAt = 0;
let lastClientByteAt = 0;
let receivedBytes = 0;
let redirectTargetRequests = 0;
const uploadAborted = deferred();
const downloadClosed = deferred();
const trackedProcesses = new Set();

function spawnTracked(executable, args, options = {}) {
  const child = spawn(executable, args, {
    ...options,
    detached: process.platform !== "win32",
    windowsHide: true,
  });
  trackedProcesses.add(child);
  return child;
}

async function runTrackedChild(child, action) {
  if (!trackedProcesses.has(child)) throw new Error("refusing to manage an untracked child process");
  let actionError;
  let cleanupError;
  try {
    try {
      await action(child);
    } catch (error) {
      actionError = error;
    }
  } finally {
    try {
      if (!processExited(child)) await forceTerminateProcess(child);
      if (!(await waitForProcessExit(child, 5_000))) {
        throw new Error(`tracked process ${child.pid} did not exit`);
      }
      trackedProcesses.delete(child);
    } catch (error) {
      cleanupError = error;
    }
  }
  if (actionError && cleanupError) throw new AggregateError([actionError, cleanupError], "termination and cleanup failed");
  if (cleanupError) throw cleanupError;
  if (actionError) throw actionError;
}

async function withFinalCleanup(child, action) {
  let actionError;
  let finalCleanupError;
  try {
    await action(child);
  } catch (error) {
    actionError = error;
  } finally {
    if (trackedProcesses.has(child)) {
      try {
        await runTrackedChild(child, async () => undefined);
      } catch (error) {
        finalCleanupError = error;
      }
    }
  }
  if (actionError && finalCleanupError) {
    throw new AggregateError([actionError, finalCleanupError], "initial termination and final cleanup failed");
  }
  if (finalCleanupError) throw finalCleanupError;
  if (actionError) throw actionError;
}

const backend = http.createServer((request, response) => {
  if (request.url?.startsWith("/api/v1/jobs/upload-abort")) {
    let ended = false;
    request.on("data", () => undefined);
    request.on("end", () => { ended = true; });
    request.on("aborted", () => uploadAborted.resolve(!ended));
    request.on("close", () => { if (!ended) uploadAborted.resolve(true); });
    return;
  }
  if (request.url?.startsWith("/api/v1/jobs/upload")) {
    request.once("data", () => { firstUpstreamByteAt = performance.now(); });
    request.on("data", (chunk) => { receivedBytes += chunk.length; });
    request.on("end", () => {
      response.writeHead(202, {
        "Content-Type": "application/json",
        Location: "http://backend:8000/internal",
        Server: "internal-backend",
        "X-Internal-Address": "minio:9000",
      });
      response.end(JSON.stringify({ job_id: "11111111-1111-4111-8111-111111111111", status: "PENDING_DISPATCH", status_url: "/api/v1/jobs/11111111-1111-4111-8111-111111111111" }));
    });
    return;
  }
  if (request.url === "/api/v1/bad-length") {
    response.writeHead(200, { "Content-Type": "application/octet-stream", "Content-Length": "5" });
    response.end("short-stream");
    return;
  }
  if (request.url === "/api/v1/redirect-absolute") {
    response.writeHead(307, { Location: "http://backend:8000/api/v1/redirect-target" });
    response.end();
    return;
  }
  if (request.url === "/api/v1/redirect-protocol-relative") {
    response.writeHead(307, { Location: "//evil.example/private" });
    response.end();
    return;
  }
  if (request.url === "/api/v1/redirect-relative") {
    response.writeHead(307, { Location: "/api/v1/jobs/relative?q=1" });
    response.end();
    return;
  }
  if (request.url === "/api/v1/redirect-target") {
    redirectTargetRequests += 1;
    response.end("followed");
    return;
  }
  if (request.url === "/api/v1/download") {
    response.writeHead(200, { "Content-Type": "application/octet-stream" });
    const interval = setInterval(() => response.write(Buffer.alloc(64 * 1024, 0x44)), 10);
    response.on("close", () => { clearInterval(interval); downloadClosed.resolve(true); });
    return;
  }
  response.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify({ id: "11111111-1111-4111-8111-111111111111" }));
});
backend.listen(0, "127.0.0.1");
await once(backend, "listening");
const backendPort = backend.address().port;

const standaloneDirectory = new URL("../../.next/standalone/", import.meta.url);
const frontend = spawnTracked(process.execPath, [fileURLToPath(new URL("server.js", standaloneDirectory))], {
  cwd: fileURLToPath(standaloneDirectory),
  env: { ...process.env, BACKEND_INTERNAL_URL: `http://127.0.0.1:${backendPort}`, NODE_ENV: "production", HOSTNAME: "127.0.0.1", PORT: String(frontendPort) },
  stdio: ["ignore", "pipe", "pipe"],
});
let frontendOutput = "";
frontend.stdout.on("data", (chunk) => { frontendOutput += chunk; });
frontend.stderr.on("data", (chunk) => { frontendOutput += chunk; });

async function waitForFrontend() {
  for (let attempt = 0; attempt < 80; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${frontendPort}/`);
      if (response.ok) return;
    } catch {}
    await delay(250);
  }
  throw new Error(`frontend did not start: ${frontendOutput.slice(-1000)}`);
}

function slowUpload() {
  const boundary = `----frame-intelligence-${randomUUID()}`;
  const prefix = Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="stream.mp4"\r\nContent-Type: video/mp4\r\n\r\n`);
  const suffix = Buffer.from(`\r\n--${boundary}--\r\n`);
  const totalBytes = prefix.length + uploadBytes + suffix.length;
  return new Promise((resolve, reject) => {
    const request = http.request(`http://127.0.0.1:${frontendPort}/api/v1/jobs/upload`, {
      method: "POST",
      headers: { "Content-Type": `multipart/form-data; boundary=${boundary}`, "Content-Length": totalBytes, "Idempotency-Key": `frontend-proxy-${randomUUID()}` },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({ response, body: Buffer.concat(chunks).toString("utf8"), totalBytes }));
    });
    request.on("error", reject);
    request.write(prefix);
    let sent = 0;
    const write = () => {
      if (sent >= uploadBytes) {
        lastClientByteAt = performance.now();
        request.end(suffix);
        return;
      }
      const size = Math.min(64 * 1024, uploadBytes - sent);
      sent += size;
      const proceed = request.write(Buffer.alloc(size, 0x46));
      if (proceed) setTimeout(write, 15);
      else request.once("drain", () => setTimeout(write, 15));
    };
    setTimeout(write, 250);
  });
}

async function verifyClientUploadAbort() {
  await new Promise((resolve) => {
    const request = http.request(`http://127.0.0.1:${frontendPort}/api/v1/jobs/upload-abort`, {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream", "Transfer-Encoding": "chunked" },
    });
    request.on("error", () => resolve());
    request.write(Buffer.alloc(64 * 1024, 0x41), () => setTimeout(() => request.destroy(), 50));
  });
  if (!(await bounded(uploadAborted.promise, "upstream upload abort"))) throw new Error("upstream upload completed instead of aborting");
}

async function verifyResponseCancel() {
  await new Promise((resolve, reject) => {
    const request = http.get(`http://127.0.0.1:${frontendPort}/api/v1/download`, (response) => {
      response.once("data", () => { response.destroy(); request.destroy(); resolve(); });
    });
    request.on("error", (error) => error.code === "ECONNRESET" ? resolve() : reject(error));
  });
  await bounded(downloadClosed.promise, "upstream response cancel");
}

async function verifyCleanupPaths() {
  const normal = spawnTracked(process.execPath, ["-e", "setInterval(() => {}, 1000); process.on('SIGTERM', () => process.exit(0))"], { stdio: "ignore" });
  await withFinalCleanup(normal, async (child) => {
    await runTrackedChild(child, async (trackedChild) => {
      const result = await terminateProcess(trackedChild, { gracefulMs: 1_000 });
      if (result.forced) throw new Error("normal cleanup unexpectedly required force");
    });
  });

  const stubborn = spawnTracked(process.execPath, ["-e", "setInterval(() => {}, 1000); process.on('SIGTERM', () => {})"], { stdio: "ignore" });
  await withFinalCleanup(stubborn, async (child) => {
    await delay(100);
    await runTrackedChild(child, async (trackedChild) => {
      const result = await terminateProcess(trackedChild, { gracefulMs: 100, forcedMs: 5_000 });
      if (!result.forced) throw new Error("forced cleanup path was not exercised");
    });
  });

  const listener = spawnTracked(process.execPath, ["-e", "const net=require('node:net');const server=net.createServer();server.listen(0,'127.0.0.1',()=>process.send(server.address().port));process.on('SIGTERM',()=>{});"], { stdio: ["ignore", "ignore", "ignore", "ipc"] });
  let listenerPort;
  await withFinalCleanup(listener, async (child) => {
    listenerPort = await bounded(once(child, "message").then(([port]) => port), "cleanup fixture port");
    let visibleError;
    try {
      await runTrackedChild(child, async () => {
        throw new Error("controlled initial termination failure");
      });
    } catch (error) {
      visibleError = error;
    }
    if (!visibleError || !String(visibleError).includes("controlled initial termination failure")) {
      throw new Error("initial termination error was not preserved");
    }
    if (!processExited(child) || trackedProcesses.has(child)) {
      throw new Error("failed cleanup fixture remained tracked or alive");
    }
    await waitForPortRelease(listenerPort);
  });
  if (listenerPort !== undefined) await waitForPortRelease(listenerPort);
}

let testError;
try {
  await waitForFrontend();
  const submitted = await bounded(slowUpload(), "streaming upload", 30_000);
  if (submitted.response.statusCode !== 202) throw new Error(`unexpected upload status ${submitted.response.statusCode}`);
  if (!(firstUpstreamByteAt > 0 && firstUpstreamByteAt < lastClientByteAt)) throw new Error("upstream did not receive the first byte before the client finished uploading");
  if (receivedBytes !== submitted.totalBytes) throw new Error(`upstream received ${receivedBytes} of ${submitted.totalBytes} bytes`);
  const responseHeaders = JSON.stringify(submitted.response.headers);
  if (/backend:8000|minio|internal-backend/i.test(responseHeaders + submitted.body)) throw new Error("internal endpoint or header leaked");
  if (submitted.response.headers.location) throw new Error("unsafe absolute Location was forwarded");

  const badLength = await bounded(fetch(`http://127.0.0.1:${frontendPort}/api/v1/bad-length`), "bad Content-Length response");
  if (badLength.headers.has("content-length")) throw new Error("upstream Content-Length was forwarded");
  if (await badLength.text() !== "short") throw new Error("streaming response did not preserve the upstream-framed body");

  for (const path of ["redirect-absolute", "redirect-protocol-relative"]) {
    const response = await fetch(`http://127.0.0.1:${frontendPort}/api/v1/${path}`, { redirect: "manual" });
    if (response.status !== 307 || response.headers.has("location")) throw new Error(`${path} Location was exposed`);
  }
  const relative = await fetch(`http://127.0.0.1:${frontendPort}/api/v1/redirect-relative`, { redirect: "manual" });
  if (relative.status !== 307 || relative.headers.get("location") !== "/api/v1/jobs/relative?q=1") throw new Error("safe relative Location was not preserved");
  if (redirectTargetRequests !== 0) throw new Error("proxy automatically followed an upstream redirect");

  await verifyClientUploadAbort();
  await verifyResponseCancel();
  await new Promise((resolve) => backend.close(resolve));
  const failed = await fetch(`http://127.0.0.1:${frontendPort}/api/v1/jobs/unavailable`);
  if (failed.status !== 502 || await failed.text() !== '{"detail":"Backend service is unavailable"}') throw new Error("upstream failure was not sanitized");
  await verifyCleanupPaths();
} catch (error) {
  testError = error;
  } finally {
  if (backend.listening) await new Promise((resolve) => backend.close(resolve));
  try {
    let cleanup;
    await runTrackedChild(frontend, async (child) => { cleanup = await terminateProcess(child); });
    await waitForPortRelease(frontendPort);
    if (!testError) console.log(JSON.stringify({ upload_bytes: uploadBytes, received_bytes: receivedBytes, first_byte_before_upload_end: true, response_content_length_removed: true, redirects_manual_and_sanitized: true, upload_abort_reached_upstream: true, response_cancel_reached_upstream: true, normal_and_forced_cleanup_tested: true, frontend_cleanup_forced: cleanup.forced, frontend_port_released: true, failure_sanitized: true }));
  } catch (cleanupError) {
    testError = testError ? new AggregateError([testError, cleanupError], "integration and cleanup failed") : cleanupError;
  }
  for (const child of [...trackedProcesses]) {
    try {
      await runTrackedChild(child, async () => undefined);
    } catch (cleanupError) {
      testError = testError ? new AggregateError([testError, cleanupError], "integration and tracked-process cleanup failed") : cleanupError;
    }
  }
}
if (testError) throw testError;
