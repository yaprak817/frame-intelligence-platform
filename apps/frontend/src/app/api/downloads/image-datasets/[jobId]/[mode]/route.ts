import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { requestHeaders, trustedBackendOrigin } from "@/app/api/v1/[...path]/route";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const MODES = new Set(["accepted", "yolo"]);
const DEFAULT_MAX_BYTES = 2_147_483_648;
const DEFAULT_RETRY_AFTER = 60;

function maximumBytes(): number | null {
  const value = process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES;
  const parsed = value === undefined ? DEFAULT_MAX_BYTES : Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export function safeRetryAfter(value: string | null): number {
  if (!value || !/^\d+$/.test(value)) return DEFAULT_RETRY_AFTER;
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) return DEFAULT_RETRY_AFTER;
  return Math.min(parsed, 86_400);
}

function errorRedirect(request: NextRequest, jobId: string, code: "RATE_LIMITED" | "DOWNLOAD_FAILED", retryAfter?: number) {
  const target = new URL(`/jobs/${jobId}/result`, request.nextUrl.origin);
  target.searchParams.set("download_error", code);
  if (code === "RATE_LIMITED") target.searchParams.set("retry_after", String(retryAfter ?? DEFAULT_RETRY_AFTER));
  return NextResponse.redirect(target, 303);
}

function parsedLength(value: string | null): number | null | "invalid" {
  if (value === null) return null;
  if (!/^\d+$/.test(value)) return "invalid";
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : "invalid";
}

async function discardUpstream(body: ReadableStream<Uint8Array> | null, cleanup: () => void) {
  try {
    await body?.cancel();
  } catch {
    // A failed cancellation must not replace the safe downstream error.
  } finally {
    cleanup();
  }
}

function boundedBody(body: ReadableStream<Uint8Array>, maximum: number, declared: number | null, signal: AbortSignal, cleanup: () => void) {
  const reader = body.getReader();
  let total = 0;
  let closed = false;
  const finish = () => {
    if (closed) return false;
    closed = true;
    signal.removeEventListener("abort", onAbort);
    try { reader.releaseLock(); } catch { /* the reader is already released */ }
    cleanup();
    return true;
  };
  const close = async (reason?: unknown) => {
    if (closed) return;
    try { await reader.cancel(reason); } catch { /* upstream is already closed */ }
    finally { finish(); }
  };
  const onAbort = () => void close(signal.reason);
  signal.addEventListener("abort", onAbort, { once: true });
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const next = await reader.read();
        if (next.done) {
          if (declared !== null && total !== declared) {
            await close(new Error("Dataset export length mismatch"));
            controller.error(new Error("Dataset export stream failed"));
            return;
          }
          finish();
          controller.close();
          return;
        }
        const nextTotal = total + next.value.byteLength;
        if (nextTotal > maximum || (declared !== null && nextTotal > declared)) {
          await close(new Error("Dataset export exceeds configured limit"));
          controller.error(new Error("Dataset export exceeds configured limit"));
          return;
        }
        total = nextTotal;
        controller.enqueue(next.value);
      } catch {
        await close();
        controller.error(new Error("Dataset export stream failed"));
      }
    },
    async cancel(reason) { await close(reason); },
  });
}

export async function GET(request: NextRequest, context: { params: Promise<{ jobId: string; mode: string }> }) {
  const { jobId, mode } = await context.params;
  if (!UUID.test(jobId) || !MODES.has(mode)) return NextResponse.json({ detail: "Invalid download" }, { status: 404 });
  const backend = trustedBackendOrigin();
  const maximum = maximumBytes();
  if (!backend || maximum === null) return errorRedirect(request, jobId, "DOWNLOAD_FAILED");
  const controller = new AbortController();
  const abort = () => controller.abort(request.signal.reason);
  if (request.signal.aborted) abort(); else request.signal.addEventListener("abort", abort, { once: true });
  let upstream: Response;
  try {
    upstream = await fetch(new URL(`/api/v1/jobs/${jobId}/dataset-exports/${mode}/download`, backend), {
      headers: requestHeaders(request), cache: "no-store", redirect: "manual", signal: controller.signal,
    });
  } catch {
    try { /* fetch already failed */ } finally { request.signal.removeEventListener("abort", abort); }
    return errorRedirect(request, jobId, "DOWNLOAD_FAILED");
  }
  if (upstream.status === 429) {
    await discardUpstream(upstream.body, () => request.signal.removeEventListener("abort", abort));
    return errorRedirect(request, jobId, "RATE_LIMITED", safeRetryAfter(upstream.headers.get("retry-after")));
  }
  const length = parsedLength(upstream.headers.get("content-length"));
  if (!upstream.ok || upstream.headers.get("content-type")?.toLowerCase() !== "application/zip" || length === "invalid" || (typeof length === "number" && length > maximum) || !upstream.body) {
    await discardUpstream(upstream.body, () => request.signal.removeEventListener("abort", abort));
    return errorRedirect(request, jobId, "DOWNLOAD_FAILED");
  }
  const headers = new Headers({
    "Content-Type": "application/zip",
    "Content-Disposition": `attachment; filename="image-dataset-${jobId}-${mode}.zip"`,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  const body = boundedBody(upstream.body, maximum, length, controller.signal, () => {
    request.signal.removeEventListener("abort", abort);
  });
  return new Response(body, { status: 200, headers });
}
