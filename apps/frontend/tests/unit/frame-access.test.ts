import { afterEach, describe, expect, it, vi } from "vitest";
import { FrameAccessLoader, FRAME_ACCESS_CONCURRENCY } from "@/lib/api/frame-access";
import type { ResultFrame } from "@/lib/api/types";

const api = vi.hoisted(() => ({ createFrameAccess: vi.fn() }));
vi.mock("@/lib/api/client", () => ({ createFrameAccess: api.createFrameAccess }));
const hash = "a".repeat(64);
const frame = (index: number): ResultFrame => ({ index, filename: `frame-${index}.jpg`, content_type: "image/jpeg", size_bytes: 10, sha256: hash, timestamp_ms: index * 1000, width: 10, height: 10, access_url: `/api/v1/jobs/job/result/frames/${index}/access` });
const response = (index: number) => ({ url: `https://download.example/${index}`, expires_at: "2099-01-01T00:00:00.000Z", content_type: "image/jpeg", size_bytes: 10, sha256: hash });
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
};

describe("FrameAccessLoader", () => {
  afterEach(() => api.createFrameAccess.mockReset());
  it("deduplicates the same in-flight frame", async () => {
    api.createFrameAccess.mockResolvedValue(response(0));
    const loader = new FrameAccessLoader("job", new AbortController().signal);
    const first = loader.load(frame(0)); const second = loader.load(frame(0));
    expect(first).toBe(second); await first; expect(api.createFrameAccess).toHaveBeenCalledTimes(1);
  });
  it("limits concurrent requests", async () => {
    let active = 0, peak = 0;
    api.createFrameAccess.mockImplementation(async (_job: string, index: number) => { active++; peak = Math.max(peak, active); await new Promise((resolve) => setTimeout(resolve, 5)); active--; return response(index); });
    const loader = new FrameAccessLoader("job", new AbortController().signal);
    await loader.loadMany(Array.from({ length: 9 }, (_, index) => frame(index)));
    expect(peak).toBe(FRAME_ACCESS_CONCURRENCY);
  });
  it("passes abort signals and isolates metadata mismatches", async () => {
    const controller = new AbortController(); api.createFrameAccess.mockResolvedValue({ ...response(0), sha256: "b".repeat(64) });
    const loader = new FrameAccessLoader("job", controller.signal);
    await expect(loader.load(frame(0))).rejects.toThrow("FRAME_METADATA_MISMATCH");
    expect(api.createFrameAccess.mock.calls[0][2]).toBe(controller.signal);
  });
  it("rejects every active and queued promise on a real abort without starting queued work", async () => {
    const controller = new AbortController();
    api.createFrameAccess.mockImplementation((_job: string, _index: number, signal: AbortSignal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    }));
    const loader = new FrameAccessLoader("job", controller.signal);
    const requests = Array.from({ length: FRAME_ACCESS_CONCURRENCY + 2 }, (_, index) => loader.load(frame(index)));
    controller.abort();
    const settled = await Promise.allSettled(requests);
    expect(settled).toHaveLength(FRAME_ACCESS_CONCURRENCY + 2);
    expect(settled.every((item) => item.status === "rejected" && (item.reason as { name?: unknown }).name === "AbortError")).toBe(true);
    expect(api.createFrameAccess).toHaveBeenCalledTimes(FRAME_ACCESS_CONCURRENCY);
    await expect(loader.load(frame(99))).rejects.toMatchObject({ name: "AbortError" });
  });
  it("releases a failed active slot, starts queued work and fully restores capacity", async () => {
    const calls = Array.from({ length: 7 }, () => deferred<ReturnType<typeof response>>());
    let started = 0, active = 0, peak = 0;
    api.createFrameAccess.mockImplementation((_job: string, index: number) => {
      started += 1; active += 1; peak = Math.max(peak, active);
      return calls[index].promise.finally(() => { active -= 1; });
    });
    const loader = new FrameAccessLoader("job", new AbortController().signal);
    const initial = Array.from({ length: 6 }, (_, index) => loader.load(frame(index)));
    expect(started).toBe(FRAME_ACCESS_CONCURRENCY);
    calls[0].reject(new Error("controlled access failure"));
    await expect(initial[0]).rejects.toThrow("controlled access failure");
    await vi.waitFor(() => expect(started).toBe(4));
    calls[1].resolve(response(1)); calls[2].resolve(response(2)); calls[3].resolve(response(3));
    await vi.waitFor(() => expect(started).toBe(6));
    calls[4].resolve(response(4)); calls[5].resolve(response(5));
    await expect(Promise.all(initial.slice(1))).resolves.toHaveLength(5);
    const afterDrain = loader.load(frame(6));
    expect(started).toBe(7); calls[6].resolve(response(6)); await expect(afterDrain).resolves.toEqual(response(6));
    expect(active).toBe(0); expect(peak).toBe(FRAME_ACCESS_CONCURRENCY);
  });
});
