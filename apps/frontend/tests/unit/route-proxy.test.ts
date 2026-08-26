import { NextRequest } from "next/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "@/app/api/v1/[...path]/route";

const context = (path: string[]) => ({ params: Promise.resolve({ path }) });

describe("streaming API route proxy", () => {
  afterEach(() => {
    delete process.env.BACKEND_INTERNAL_URL;
    vi.unstubAllGlobals();
  });

  it("reads the backend target at request time and allowlists request headers", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const upstream = vi.fn().mockResolvedValue(new Response("ok", { status: 202 }));
    vi.stubGlobal("fetch", upstream);
    const request = new NextRequest("http://frontend/api/v1/jobs/upload?q=1", {
      method: "POST",
      headers: {
        Accept: "application/json",
        "Content-Type": "video/mp4",
        "Content-Length": "5",
        "Idempotency-Key": "web-request-1",
        Cookie: "session=secret",
        Authorization: "Bearer secret",
        Connection: "keep-alive",
        Host: "attacker.example",
        "X-Forwarded-Host": "internal",
      },
      body: "video",
      duplex: "half",
    });

    await POST(request, context(["jobs", "upload"]));
    const [target, init] = upstream.mock.calls[0] as [URL, RequestInit];
    expect(target.href).toBe("http://backend:8000/api/v1/jobs/upload?q=1");
    expect(Object.fromEntries(init.headers as Headers)).toEqual({
      accept: "application/json",
      "content-length": "5",
      "content-type": "video/mp4",
      "idempotency-key": "web-request-1",
    });
    expect(init.body).toBe(request.body);
    expect((init as RequestInit & { duplex: string }).duplex).toBe("half");
  });

  it("allowlists response headers and removes internal locations", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("body", {
      status: 202,
      headers: {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
        "Content-Length": "999",
        Server: "internal-server",
        Connection: "close",
        Location: "http://backend:8000/private",
        "X-Internal-Address": "minio:9000",
      },
    })));
    const response = await GET(new NextRequest("http://frontend/api/v1/jobs/1"), context(["jobs", "1"]));
    expect(response.headers.get("content-type")).toContain("application/json");
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("server")).toBeNull();
    expect(response.headers.get("connection")).toBeNull();
    expect(response.headers.get("location")).toBeNull();
    expect(response.headers.get("content-length")).toBeNull();
    expect([...response.headers].join(" ")).not.toMatch(/backend:8000|minio/);
  });

  it("keeps only safe relative redirects", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 307, headers: { Location: "/api/v1/jobs/1?q=1" } })));
    const response = await GET(new NextRequest("http://frontend/api/v1/jobs/1"), context(["jobs", "1"]));
    expect(response.headers.get("location")).toBe("/api/v1/jobs/1?q=1");
  });

  it("returns a fixed safe error when the upstream connection fails", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("connect backend:8000 secret")));
    const response = await GET(new NextRequest("http://frontend/api/v1/jobs/1"), context(["jobs", "1"]));
    expect(response.status).toBe(502);
    expect(await response.text()).toBe('{"detail":"Backend service is unavailable"}');
  });

  it("passes client abort to the upstream request", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const client = new AbortController();
    let upstreamSignal!: AbortSignal;
    vi.stubGlobal("fetch", vi.fn((_target: URL, init: RequestInit) => {
      upstreamSignal = init.signal as AbortSignal;
      return new Promise((_resolve, reject) => {
        if (upstreamSignal.aborted) reject(new DOMException("Aborted", "AbortError"));
        else upstreamSignal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      });
    }));
    const pending = GET(new NextRequest("http://frontend/api/v1/jobs/1", { signal: client.signal }), context(["jobs", "1"]));
    client.abort();
    await pending;
    expect(upstreamSignal.aborted).toBe(true);
  });

  it.each([
    { path: [".."] },
    { path: ["."] },
    { path: ["jobs\\upload"] },
    { path: ["jobs", "%2Fupload"] },
  ])("rejects unsafe path segments", async ({ path }) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const response = await GET(new NextRequest("http://frontend/api/v1/test"), context(path));
    expect(response.status).toBe(400);
    expect(upstream).not.toHaveBeenCalled();
  });
});
