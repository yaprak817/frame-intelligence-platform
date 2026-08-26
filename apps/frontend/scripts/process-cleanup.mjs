import net from "node:net";
import { spawn } from "node:child_process";
import { once } from "node:events";

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

export function processExited(child) {
  return child.exitCode !== null || child.signalCode !== null;
}

export async function waitForProcessExit(child, milliseconds) {
  if (processExited(child)) return true;
  return Promise.race([
    once(child, "exit").then(() => true),
    delay(milliseconds).then(() => false),
  ]);
}

function validatedPid(child) {
  if (!Number.isSafeInteger(child.pid) || child.pid <= 0) {
    throw new Error("cannot terminate child without a valid PID");
  }
  return child.pid;
}

export async function forceTerminateProcess(child, forcedMs = 5_000) {
  if (processExited(child)) return;
  const pid = validatedPid(child);
  if (processExited(child)) return;

  if (process.platform === "win32") {
    const killer = spawn("taskkill", ["/PID", String(pid), "/T", "/F"], {
      stdio: "ignore",
      windowsHide: true,
    });
    const [code] = await once(killer, "exit");
    if (code !== 0 && !processExited(child)) {
      throw new Error(`taskkill failed with exit code ${code}`);
    }
  } else {
    process.kill(-pid, "SIGKILL");
  }
  if (!(await waitForProcessExit(child, forcedMs))) {
    throw new Error(`process ${pid} remained alive after forced termination`);
  }
}

export async function terminateProcess(child, { gracefulMs = 5_000, forcedMs = 5_000 } = {}) {
  if (processExited(child)) return { forced: false };
  validatedPid(child);
  child.kill("SIGTERM");
  if (await waitForProcessExit(child, gracefulMs)) return { forced: false };
  await forceTerminateProcess(child, forcedMs);
  return { forced: true };
}

async function portAcceptsConnections(port, host) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ port, host });
    socket.setTimeout(250);
    socket.once("connect", () => { socket.destroy(); resolve(true); });
    socket.once("timeout", () => { socket.destroy(); resolve(false); });
    socket.once("error", () => resolve(false));
  });
}

export async function waitForPortRelease(port, host = "127.0.0.1", timeoutMs = 5_000) {
  const deadline = performance.now() + timeoutMs;
  while (performance.now() < deadline) {
    if (!(await portAcceptsConnections(port, host))) return;
    await delay(50);
  }
  throw new Error(`port ${host}:${port} remained in use after cleanup`);
}
