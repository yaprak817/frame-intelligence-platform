import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, safeRetryAfter } from "@/app/api/downloads/image-datasets/[jobId]/[mode]/route";

const JOB = "11111111-1111-4111-8111-111111111111";
const context = (mode: string, jobId = JOB) => ({ params: Promise.resolve({ jobId, mode }) });
const request = (mode = "accepted", signal?: AbortSignal) =>
  new NextRequest(`http://frontend/api/downloads/image-datasets/${JOB}/${mode}`, { signal });
const chunks = (...values: string[]) => new ReadableStream<Uint8Array>({
  start(controller) {
    for (const value of values) controller.enqueue(new TextEncoder().encode(value));
    controller.close();
  },
});

describe("dataset ZIP streaming route", () => {
  afterEach(() => {
    delete process.env.BACKEND_INTERNAL_URL;
    delete process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES;
    vi.unstubAllGlobals();
  });

  it.each(["accepted", "yolo"])("streams %s ZIP chunks with safe headers", async (mode) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const body = chunks("one", "two", "three");
    const blob = vi.spyOn(Response.prototype, "blob");
    const arrayBuffer = vi.spyOn(Response.prototype, "arrayBuffer");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
      headers: { "Content-Type": "application/zip", "Content-Length": "11", "X-Object-Key": "secret" },
    })));
    const response = await GET(request(mode), context(mode));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-disposition")).toBe(`attachment; filename="image-dataset-${JOB}-${mode}.zip"`);
    const reader = response.body!.getReader();
    const seen: string[] = [];
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      seen.push(new TextDecoder().decode(next.value));
    }
    expect(seen).toEqual(["one", "two", "three"]);
    expect(blob).not.toHaveBeenCalled();
    expect(arrayBuffer).not.toHaveBeenCalled();
    expect([...response.headers].join(" ")).not.toMatch(/backend|secret|object/i);
  });

  it("cancels the upstream reader when the client cancels", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ pull() {}, cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "application/zip" } })));
    const response = await GET(request(), context("accepted"));
    await response.body!.cancel("client-left");
    expect(cancel).toHaveBeenCalledWith("client-left");
  });

  it("propagates request abort to the upstream body", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ pull() {}, cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { headers: { "Content-Type": "application/zip" } })));
    const controller = new AbortController();
    const response = await GET(request("accepted", controller.signal), context("accepted"));
    controller.abort("request-left");
    await vi.waitFor(() => expect(cancel).toHaveBeenCalledWith("request-left"));
    await response.body!.cancel().catch(() => undefined);
  });

  it.each([
    ["6", 303], ["-1", 303], ["bad", 303],
  ])("rejects unsafe Content-Length %s before reading", async (length, status) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES = "5";
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ pull() {}, cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, body,
      headers: new Headers({ "Content-Type": "application/zip", "Content-Length": length }),
    }));
    const response = await GET(request(), context("accepted"));
    expect(response.status).toBe(status);
    expect(cancel).toHaveBeenCalled();
  });

  it.each([
    ["2", ["123"]],
    ["3", ["12"]],
  ])("fails closed when declared length %s does not equal the body", async (length, values) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES = "5";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(chunks(...values), {
      headers: { "Content-Type": "application/zip", "Content-Length": length },
    })));
    const response = await GET(request(), context("accepted"));
    expect(response.headers.has("content-length")).toBe(false);
    await expect(response.text()).rejects.toThrow();
  });

  it("streams when declared length exactly matches the body", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES = "5";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(chunks("1", "23"), {
      headers: { "Content-Type": "application/zip", "Content-Length": "3" },
    })));
    const response = await GET(request(), context("accepted"));
    expect(response.headers.has("content-length")).toBe(false);
    await expect(response.text()).resolves.toBe("123");
  });

  it.each([
    [["12", "3"], true],
    [["123", "456"], false],
  ])("bounds a body without Content-Length (success=%s)", async (values, succeeds) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES = "5";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(chunks(...values), {
      headers: { "Content-Type": "application/zip" },
    })));
    const response = await GET(request(), context("accepted"));
    if (succeeds) await expect(response.text()).resolves.toBe("123");
    else await expect(response.text()).rejects.toThrow();
  });

  it("rejects actual bytes over the maximum even when declared is below it", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.IMAGE_DATASET_ZIP_MAX_COMPRESSED_BYTES = "5";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(chunks("123", "456"), {
      headers: { "Content-Type": "application/zip", "Content-Length": "5" },
    })));
    const response = await GET(request(), context("accepted"));
    await expect(response.text()).rejects.toThrow();
  });

  it("rejects an invalid content type without exposing upstream details", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("object_key=secret", { headers: { "Content-Type": "text/plain" } })));
    const response = await GET(request(), context("accepted"));
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toBe(`http://frontend/jobs/${JOB}/result?download_error=DOWNLOAD_FAILED`);
  });

  it.each([429, 200])("keeps the safe error when upstream cancellation rejects (status=%s)", async (status) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const body = new ReadableStream<Uint8Array>({ cancel: () => { throw new Error("cancel failed"); } });
    const upstream = {
      ok: status === 200,
      status,
      body,
      headers: new Headers(status === 429
        ? { "Retry-After": "7" }
        : { "Content-Type": "text/plain" }),
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(upstream));
    const incoming = request();
    const remove = vi.spyOn(incoming.signal, "removeEventListener");
    const response = await GET(incoming, context("accepted"));
    expect(response.status).toBe(303);
    expect(response.headers.get("location")).toContain(status === 429 ? "RATE_LIMITED" : "DOWNLOAD_FAILED");
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
  });

  it("releases the upstream reader and removes listeners on normal EOF", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const body = chunks("123");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, {
      headers: { "Content-Type": "application/zip", "Content-Length": "3" },
    })));
    const incoming = request();
    const remove = vi.spyOn(incoming.signal, "removeEventListener");
    const response = await GET(incoming, context("accepted"));
    await expect(response.text()).resolves.toBe("123");
    expect(body.locked).toBe(false);
    expect(remove).toHaveBeenCalledWith("abort", expect.any(Function));
  });

  it.each([["7", 7], [null, 60], ["0", 60], ["-1", 60], ["nope", 60], ["86400", 86400], ["86401", 86400]])(
    "normalizes Retry-After %s",
    (value, expected) => expect(safeRetryAfter(value)).toBe(expected),
  );

  it("redirects 429 and 5xx without reading or exposing their bodies", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const fetch = vi.fn()
      .mockResolvedValueOnce(new Response("internal object key", { status: 429, headers: { "Retry-After": "7" } }))
      .mockResolvedValueOnce(new Response("backend:8000 secret", { status: 503 }));
    vi.stubGlobal("fetch", fetch);
    const limited = await GET(request(), context("accepted"));
    const failed = await GET(request("yolo"), context("yolo"));
    expect(limited.headers.get("location")).toBe(`http://frontend/jobs/${JOB}/result?download_error=RATE_LIMITED&retry_after=7`);
    expect(failed.headers.get("location")).toBe(`http://frontend/jobs/${JOB}/result?download_error=DOWNLOAD_FAILED`);
  });

  it.each([["bad", "accepted"], [JOB, "other"]])("rejects invalid identifiers", async (jobId, mode) => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    expect((await GET(request(), context(mode, jobId))).status).toBe(404);
    expect(fetch).not.toHaveBeenCalled();
  });
});
