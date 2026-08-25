import { ApiError, safeApiError } from "./errors";
import type {
  ApiErrorPayload,
  JobStatus,
  JobStatusResponse,
  JobSubmissionResponse,
  ProcessingConfig,
} from "./types";

const STATUSES: JobStatus[] = [
  "PENDING_DISPATCH",
  "QUEUED",
  "RUNNING",
  "SUCCEEDED",
  "FAILED",
];

async function errorFromResponse(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload | undefined;
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    payload = undefined;
  }
  return safeApiError(response.status, payload);
}

function isSubmission(value: unknown): value is JobSubmissionResponse {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  return (
    typeof item.job_id === "string" &&
    typeof item.status_url === "string" &&
    STATUSES.includes(item.status as JobStatus)
  );
}

function isJobStatus(value: unknown): value is JobStatusResponse {
  if (!value || typeof value !== "object") return false;
  const item = value as Record<string, unknown>;
  const nullableDate = (candidate: unknown) =>
    candidate === null ||
    (typeof candidate === "string" && candidate.length > 0 && Number.isFinite(Date.parse(candidate)));
  const failure = item.failure;
  const validFailure =
    failure === null ||
    (typeof failure === "object" &&
      typeof (failure as Record<string, unknown>).code === "string" &&
      typeof (failure as Record<string, unknown>).message === "string");
  const result = item.result;
  const validResult =
    result === null ||
    (typeof result === "object" &&
      typeof (result as Record<string, unknown>).available === "boolean" &&
      typeof (result as Record<string, unknown>).metadata_url === "string" &&
      typeof (result as Record<string, unknown>).manifest_download_url === "string");
  return (
    typeof item.id === "string" &&
    typeof item.source === "string" &&
    (item.source_type === "URL" || item.source_type === "UPLOAD") &&
    STATUSES.includes(item.status as JobStatus) &&
    typeof item.created_at === "string" &&
    item.created_at.length > 0 &&
    Number.isFinite(Date.parse(item.created_at)) &&
    nullableDate(item.started_at) &&
    nullableDate(item.completed_at) &&
    validFailure &&
    validResult
  );
}

export async function submitUrlJob(
  url: string,
  processing: ProcessingConfig,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<JobSubmissionResponse> {
  const response = await fetch("/api/v1/jobs/url", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ url, processing }),
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw await errorFromResponse(response);
  const payload: unknown = await response.json();
  if (!isSubmission(payload)) throw new ApiError(502);
  return payload;
}

export async function getJobStatus(
  jobId: string,
  signal?: AbortSignal,
): Promise<JobStatusResponse> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}`, {
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw await errorFromResponse(response);
  const payload: unknown = await response.json();
  if (!isJobStatus(payload)) throw new ApiError(502);
  return payload;
}
