import { act, renderHook } from "@testing-library/react";
import { StrictMode, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/errors";
import type { JobStatus, JobStatusResponse } from "@/lib/api/types";
import { MAX_BACKOFF_MS, retryDelay, useJobPolling } from "@/hooks/use-job-polling";

const getJobStatus = vi.fn();
vi.mock("@/lib/api/client", () => ({ getJobStatus: (...args: unknown[]) => getJobStatus(...args) }));

function job(status: JobStatus): JobStatusResponse {
  return { id: "job-1", status, source_type: "URL", source: "https://example.com/", created_at: "2026-08-24T10:00:00Z", started_at: null, completed_at: null, failure: null, result: null };
}

async function flushAsyncWork() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("job polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getJobStatus.mockReset();
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
  });

  afterEach(() => vi.useRealTimers());

  it("uses bounded exponential retry delays", () => {
    expect([1, 2, 3, 4, 8].map(retryDelay)).toEqual([2_000, 4_000, 8_000, 10_000, MAX_BACKOFF_MS]);
  });

  it.each(["SUCCEEDED", "FAILED"] as JobStatus[])("stops at %s", async (status) => {
    getJobStatus.mockResolvedValue(job(status));
    renderHook(() => useJobPolling("job-1"));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(getJobStatus).toHaveBeenCalledTimes(1);
  });

  it("does not overlap requests and polls after completion", async () => {
    let resolve!: (value: JobStatusResponse) => void;
    getJobStatus.mockReturnValueOnce(new Promise((done) => { resolve = done; })).mockResolvedValue(job("RUNNING"));
    renderHook(() => useJobPolling("job-1"));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(getJobStatus).toHaveBeenCalledTimes(1);
    await act(async () => resolve(job("RUNNING")));
    await flushAsyncWork();
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(2);
  });

  it("retries network and 503 failures but stops on 404", async () => {
    getJobStatus.mockRejectedValueOnce(new TypeError("private endpoint"))
      .mockRejectedValueOnce(new ApiError(503))
      .mockRejectedValueOnce(new ApiError(404));
    const { result } = renderHook(() => useJobPolling("job-1"));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    await flushAsyncWork();
    await act(() => vi.advanceTimersByTimeAsync(4_000));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(3);
    expect(result.current.error).toBe("İş bulunamadı.");
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(getJobStatus).toHaveBeenCalledTimes(3);
  });

  it("aborts the active request on unmount", async () => {
    getJobStatus.mockImplementation((_id: string, signal: AbortSignal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
    }));
    const { unmount } = renderHook(() => useJobPolling("job-1"));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(1);
    const signal = getJobStatus.mock.calls[0][1] as AbortSignal;
    unmount();
    expect(signal.aborted).toBe(true);
  });

  it("clears the old job immediately when the job id changes", async () => {
    getJobStatus.mockResolvedValueOnce(job("SUCCEEDED")).mockReturnValueOnce(new Promise(() => undefined));
    const { result, rerender } = renderHook(({ id }) => useJobPolling(id), { initialProps: { id: "old" } });
    await flushAsyncWork();
    expect(result.current.job?.status).toBe("SUCCEEDED");
    rerender({ id: "new" });
    expect(result.current).toEqual({ job: null, error: null, loading: true });
  });

  it("does not show an old terminal job when the new job returns 404", async () => {
    getJobStatus.mockResolvedValueOnce(job("SUCCEEDED")).mockRejectedValueOnce(new ApiError(404));
    const { result, rerender } = renderHook(({ id }) => useJobPolling(id), { initialProps: { id: "old" } });
    await flushAsyncWork();
    rerender({ id: "missing" });
    await flushAsyncWork();
    expect(result.current.job).toBeNull();
    expect(result.current.error).toBe("İş bulunamadı.");
  });

  it("leaves only one polling chain after Strict Mode remount", async () => {
    getJobStatus.mockResolvedValue(job("RUNNING"));
    const wrapper = ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>;
    renderHook(() => useJobPolling("job-1"), { wrapper });
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(2_000));
    await flushAsyncWork();
    expect(getJobStatus).toHaveBeenCalledTimes(2);
  });
});
