import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const REQUEST_HEADERS = ["accept", "content-type", "content-length", "idempotency-key"] as const;
const RESPONSE_HEADERS = [
  "content-type",
  "cache-control",
  "pragma",
  "content-disposition",
  "retry-after",
] as const;
const UPSTREAM_TIMEOUT_MS = 30_000;

type StreamingRequestInit = RequestInit & { duplex?: "half" };

function safeContentLength(value: string | null): string | null {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? value : null;
}

function trustedBackendOrigin(): URL | null {
  const configured = process.env.BACKEND_INTERNAL_URL;
  if (!configured) return null;
  try {
    const origin = new URL(configured);
    if (
      !["http:", "https:"].includes(origin.protocol) ||
      origin.username ||
      origin.password ||
      origin.search ||
      origin.hash ||
      (origin.pathname !== "/" && origin.pathname !== "")
    ) return null;
    return origin;
  } catch {
    return null;
  }
}

function safeSegments(segments: string[]): boolean {
  if (segments.length === 0) return false;
  return segments.every((segment) => {
    if (!segment || segment === "." || segment === ".." || segment.includes("\\")) return false;
    try {
      const decoded = decodeURIComponent(segment);
      return decoded !== "." && decoded !== ".." && !decoded.includes("\\") && !decoded.includes("/");
    } catch {
      return false;
    }
  });
}

function requestHeaders(request: NextRequest): Headers {
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (!value) continue;
    if (name === "content-length") {
      const length = safeContentLength(value);
      if (length !== null) headers.set(name, length);
    } else {
      headers.set(name, value);
    }
  }
  return headers;
}

function safeRelativeLocation(value: string | null, request: NextRequest): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return null;
  try {
    const location = new URL(value, request.nextUrl.origin);
    return location.origin === request.nextUrl.origin ? `${location.pathname}${location.search}${location.hash}` : null;
  } catch {
    return null;
  }
}

function responseHeaders(upstream: Response, request: NextRequest): Headers {
  const headers = new Headers();
  for (const name of RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (!value) continue;
    headers.set(name, value);
  }
  const location = safeRelativeLocation(upstream.headers.get("location"), request);
  if (location) headers.set("location", location);
  return headers;
}

function streamedBody(
  body: ReadableStream<Uint8Array> | null,
  controller: AbortController,
  cleanup: () => void,
): ReadableStream<Uint8Array> | null {
  if (!body) {
    cleanup();
    return null;
  }
  const reader = body.getReader();
  return new ReadableStream<Uint8Array>({
    async pull(output) {
      try {
        const next = await reader.read();
        if (next.done) {
          cleanup();
          output.close();
        } else {
          output.enqueue(next.value);
        }
      } catch (error) {
        cleanup();
        output.error(error);
      }
    },
    async cancel(reason) {
      controller.abort(reason);
      cleanup();
      await reader.cancel(reason);
    },
  });
}

async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const backend = trustedBackendOrigin();
  const { path } = await context.params;
  if (!backend) {
    return NextResponse.json({ detail: "Backend service is unavailable" }, { status: 503 });
  }
  if (!safeSegments(path)) {
    return NextResponse.json({ detail: "Invalid API path" }, { status: 400 });
  }

  const target = new URL(`/api/v1/${path.map(encodeURIComponent).join("/")}${request.nextUrl.search}`, backend);
  const controller = new AbortController();
  const abortUpstream = () => controller.abort(request.signal.reason);
  if (request.signal.aborted) abortUpstream();
  else request.signal.addEventListener("abort", abortUpstream, { once: true });
  const timeout = setTimeout(() => controller.abort(new DOMException("Timed out", "TimeoutError")), UPSTREAM_TIMEOUT_MS);
  const cleanup = () => {
    clearTimeout(timeout);
    request.signal.removeEventListener("abort", abortUpstream);
  };
  const init: StreamingRequestInit = {
    method: request.method,
    headers: requestHeaders(request),
    redirect: "manual",
    signal: controller.signal,
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = request.body;
    init.duplex = "half";
  }

  try {
    const upstream = await fetch(target, init);
    return new Response(streamedBody(upstream.body, controller, cleanup), {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders(upstream, request),
    });
  } catch {
    cleanup();
    return NextResponse.json({ detail: "Backend service is unavailable" }, { status: 502 });
  }
}

export const GET = forward;
export const POST = forward;
