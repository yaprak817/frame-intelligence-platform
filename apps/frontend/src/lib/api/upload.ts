import { ApiError, safeApiError } from "./errors";
import type { ApiErrorPayload, JobSubmissionResponse, ProcessingConfig } from "./types";

export interface UploadRequest {
  promise: Promise<JobSubmissionResponse>;
  abort: () => void;
}

export function uploadVideo(
  file: File,
  processing: ProcessingConfig,
  idempotencyKey: string,
  onProgress: (percent: number | null) => void,
): UploadRequest {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<JobSubmissionResponse>((resolve, reject) => {
    xhr.open("POST", "/api/v1/jobs/upload");
    xhr.setRequestHeader("Idempotency-Key", idempotencyKey);
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable || event.total <= 0) {
        onProgress(null);
        return;
      }
      const percent = Math.round((event.loaded / event.total) * 100);
      onProgress(Number.isFinite(percent) ? Math.min(100, Math.max(0, percent)) : null);
    });
    xhr.addEventListener("load", () => {
      const payload = xhr.response as JobSubmissionResponse | ApiErrorPayload | null;
      if (xhr.status !== 202) {
        reject(safeApiError(xhr.status, (payload ?? undefined) as ApiErrorPayload));
        return;
      }
      if (!payload || !("job_id" in payload) || typeof payload.job_id !== "string") {
        reject(new ApiError(502));
        return;
      }
      resolve(payload as JobSubmissionResponse);
    });
    xhr.addEventListener("error", () => reject(new TypeError("Network request failed")));
    xhr.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));

    const data = new FormData();
    data.append("file", file);
    data.append("candidate_fps", String(processing.candidate_fps));
    data.append("selection_window_seconds", String(processing.selection_window_seconds));
    xhr.send(data);
  });
  return { promise, abort: () => xhr.abort() };
}
