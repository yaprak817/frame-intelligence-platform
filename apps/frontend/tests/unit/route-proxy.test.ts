import { NextRequest } from "next/server";
import { createHmac } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "@/app/api/v1/[...path]/route";

const context = (path: string[]) => ({ params: Promise.resolve({ path }) });
const HMAC_VECTOR_SECRET = "fixture-only-proxy-secret-0123456789-DO-NOT-USE";
const HMAC_VECTOR_IP = "203.0.113.8";
const HMAC_VECTOR_EXPECTED = "d513dd72d0b92b859e2130ba7c5d6cd3b9ebf8ce894cb1aa0ddd9a8349d3f204";

describe("streaming API route proxy", () => {
  afterEach(() => {
    delete process.env.BACKEND_INTERNAL_URL;
    delete process.env.API_PROXY_TIMEOUT_SECONDS;
    delete process.env.UPLOAD_PROXY_TIMEOUT_SECONDS;
    delete process.env.INTERNAL_PROXY_SHARED_SECRET;
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("uses the separately bounded upload timeout without buffering the body", async () => {
    vi.useFakeTimers();
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.API_PROXY_TIMEOUT_SECONDS = "1";
    process.env.UPLOAD_PROXY_TIMEOUT_SECONDS = "60";
    const upstream = vi.fn((_target: URL, init: RequestInit) => new Promise<Response>((resolve, reject) => {
      const signal = init.signal as AbortSignal;
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
      setTimeout(() => resolve(new Response("ok", { status: 202 })), 31_000);
    }));
    vi.stubGlobal("fetch", upstream);
    const request = new NextRequest("http://frontend/api/v1/jobs/upload", { method: "POST", body: "video", duplex: "half" });
    const pending = POST(request, context(["jobs", "upload"]));
    await vi.advanceTimersByTimeAsync(31_000);
    const response = await pending;
    expect(response.status).toBe(202);
    expect((upstream.mock.calls[0][1] as RequestInit).body).toBe(request.body);
  });

  it("keeps ordinary API requests on their separately bounded timeout", async () => {
    vi.useFakeTimers();
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.API_PROXY_TIMEOUT_SECONDS = "1";
    process.env.UPLOAD_PROXY_TIMEOUT_SECONDS = "60";
    vi.stubGlobal("fetch", vi.fn((_target: URL, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      const signal = init.signal as AbortSignal;
      signal.addEventListener("abort", () => reject(new DOMException("credential=https://secret.example", "AbortError")));
    })));
    const pending = GET(new NextRequest("http://frontend/api/v1/jobs/1?token=secret"), context(["jobs", "1"]));
    await vi.advanceTimersByTimeAsync(1_000);
    const response = await pending;
    expect(response.status).toBe(502);
    expect(await response.text()).toBe('{"detail":"Backend service is unavailable"}');
  });

  it.each(["0", "59", "7201", "1.5", "invalid"])("fails closed for invalid upload timeout %s", async (value) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.UPLOAD_PROXY_TIMEOUT_SECONDS = value;
    const upstream = vi.fn();
    vi.stubGlobal("fetch", upstream);
    const response = await POST(new NextRequest("http://frontend/api/v1/jobs/upload", { method: "POST", body: "x", duplex: "half" }), context(["jobs", "upload"]));
    expect(response.status).toBe(503);
    expect(await response.text()).toBe('{"detail":"Proxy timeout configuration is invalid"}');
    expect(upstream).not.toHaveBeenCalled();
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

  it.each([
    ["203.0.113.8", "203.0.113.8"],
    ["2001:0db8:0:0:0:0:0:8", "2001:db8::8"],
  ])("forwards one canonical signed client IP from Caddy (%s)", async (forwarded, canonical) => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    const proxySecret = "proxy-secret-0123456789-ABCDEFGHIJ";
    process.env.INTERNAL_PROXY_SHARED_SECRET = proxySecret;
    const upstream = vi.fn().mockResolvedValue(new Response("ok"));
    vi.stubGlobal("fetch", upstream);
    await GET(new NextRequest("http://frontend/api/v1/jobs/1", { headers: { "X-Forwarded-For": forwarded } }), context(["jobs", "1"]));
    const headers = new Headers((upstream.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("x-forwarded-for")).toBe(canonical);
    const expected = createHmac("sha256", proxySecret)
      .update("frame-intelligence-platform:proxy-client-ip:v1")
      .update("\0")
      .update(canonical)
      .digest("hex");
    expect(headers.get("x-frame-client-ip-signature")).toBe(expected);
  });

  it.each([undefined, "", "bad-ip", "203.0.113.1, 198.51.100.2", "203.0.113.1\u0001", "1".repeat(46)])(
    "fails safe for an invalid Caddy client IP (%s)",
    async (forwarded) => {
      process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
      process.env.INTERNAL_PROXY_SHARED_SECRET = "proxy-secret-0123456789-ABCDEFGHIJ";
      const upstream = vi.fn().mockResolvedValue(new Response("ok"));
      vi.stubGlobal("fetch", upstream);
      const headers = new Headers();
      if (forwarded !== undefined) headers.append("X-Forwarded-For", forwarded);
      await GET(new NextRequest("http://frontend/api/v1/jobs/1", { headers }), context(["jobs", "1"]));
      const sent = new Headers((upstream.mock.calls[0][1] as RequestInit).headers);
      expect(sent.has("x-forwarded-for")).toBe(false);
      expect(sent.has("x-frame-client-ip-signature")).toBe(false);
    },
  );

  it("matches the shared hardcoded proxy-client-IP HMAC vector", async () => {
    process.env.BACKEND_INTERNAL_URL = "http://backend:8000";
    process.env.INTERNAL_PROXY_SHARED_SECRET = HMAC_VECTOR_SECRET;
    const upstream = vi.fn().mockResolvedValue(new Response("ok"));
    vi.stubGlobal("fetch", upstream);
    await GET(
      new NextRequest("http://frontend/api/v1/jobs/1", {
        headers: { "X-Forwarded-For": HMAC_VECTOR_IP },
      }),
      context(["jobs", "1"]),
    );
    const headers = new Headers((upstream.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("x-frame-client-ip-signature")).toBe(HMAC_VECTOR_EXPECTED);
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
