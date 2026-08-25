"use client";

import { useEffect, useState } from "react";
import { getJobStatus } from "@/lib/api/client";
import { ApiError, userErrorMessage } from "@/lib/api/errors";
import type { JobStatusResponse } from "@/lib/api/types";

export const NORMAL_POLL_MS = 2_000;
export const HIDDEN_POLL_MS = 10_000;
export const MAX_BACKOFF_MS = 10_000;

export function retryDelay(failures: number): number {
  return Math.min(NORMAL_POLL_MS * 2 ** Math.max(0, failures - 1), MAX_BACKOFF_MS);
}

export function useJobPolling(jobId: string) {
  const [state, setState] = useState<{
    jobId: string;
    job: JobStatusResponse | null;
    error: string | null;
    loading: boolean;
  }>({ jobId, job: null, error: null, loading: true });

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let controller: AbortController | undefined;
    let failures = 0;

    const schedule = (delay: number) => {
      if (!stopped) timer = setTimeout(poll, delay);
    };

    const poll = async () => {
      if (stopped) return;
      controller = new AbortController();
      try {
        const next = await getJobStatus(jobId, controller.signal);
        if (stopped) return;
        setState({ jobId, job: next, error: null, loading: false });
        failures = 0;
        if (next.status === "SUCCEEDED" || next.status === "FAILED") return;
        schedule(document.hidden ? HIDDEN_POLL_MS : NORMAL_POLL_MS);
      } catch (caught) {
        if (stopped || (caught instanceof DOMException && caught.name === "AbortError")) return;
        setState((current) => ({
          jobId,
          job: current.jobId === jobId ? current.job : null,
          error: userErrorMessage(caught),
          loading: false,
        }));
        if (caught instanceof ApiError && caught.status === 404) return;
        if (!(caught instanceof ApiError) || caught.status === 503) {
          failures += 1;
          schedule(document.hidden ? HIDDEN_POLL_MS : retryDelay(failures));
        }
      }
    };

    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      controller?.abort();
    };
  }, [jobId]);

  return state.jobId === jobId
    ? { job: state.job, error: state.error, loading: state.loading }
    : { job: null, error: null, loading: true };
}
