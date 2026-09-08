import { ApiError, safeApiError } from "./errors";
import type {
  ApiErrorPayload,
  JobStatus,
  JobStatusResponse,
  JobSubmissionResponse,
  ProcessingConfig,
  PublicResultManifest,
  FrameAccessResponse,
  FrameExportResponse,
  PublicDatasetManifest,
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
  return safeApiError(response.status, payload, response.headers.get("retry-after"));
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

const exactKeys = (item: Record<string, unknown>, keys: readonly string[]) =>
  Object.keys(item).length === keys.length && keys.every((key) => key in item);
const isRecord = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const isUtcDate = (value: unknown) =>
  typeof value === "string" &&
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$/.test(value) &&
  Number.isFinite(Date.parse(value));
const isSafeInteger = (value: unknown, minimum = 0) =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const isNonNegativeNumber = (value: unknown) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;

export function isJobStatus(value: unknown): value is JobStatusResponse {
  if (!isRecord(value) || !exactKeys(value, ["id", "status", "source_type", "source", "created_at", "started_at", "completed_at", "failure", "result"])) return false;
  const item = value as Record<string, unknown>;
  const nullableDate = (candidate: unknown) =>
    candidate === null ||
    (typeof candidate === "string" && candidate.length > 0 && Number.isFinite(Date.parse(candidate)));
  const failure = item.failure;
  const validFailure =
    failure === null ||
    (isRecord(failure) && exactKeys(failure, ["code", "message"]) &&
      typeof (failure as Record<string, unknown>).code === "string" &&
      typeof (failure as Record<string, unknown>).message === "string");
  const result = item.result;
  const validResult =
    result === null ||
    (isRecord(result) && exactKeys(result, ["result_kind", "available", "metadata_url", "manifest_download_url"]) &&
      ((result as Record<string, unknown>).result_kind === "VIDEO_FRAMES" || (result as Record<string, unknown>).result_kind === "IMAGE_DATASET") &&
      typeof (result as Record<string, unknown>).available === "boolean" &&
      typeof (result as Record<string, unknown>).metadata_url === "string" &&
      typeof (result as Record<string, unknown>).manifest_download_url === "string");
  return (
    typeof item.id === "string" && canonicalUuid(item.id) !== null &&
    typeof item.source === "string" &&
    (item.source_type === "URL" || item.source_type === "UPLOAD" || item.source_type === "IMAGE_DATASET") &&
    STATUSES.includes(item.status as JobStatus) &&
    typeof item.created_at === "string" &&
    item.created_at.length > 0 &&
    Number.isFinite(Date.parse(item.created_at)) &&
    nullableDate(item.started_at) &&
    nullableDate(item.completed_at) &&
    validFailure &&
    validResult &&
    (item.status === "SUCCEEDED" ? result !== null && (result as Record<string, unknown>).available === true : result === null) &&
    (item.status === "FAILED" || failure === null)
  );
}

export function canonicalUuid(value: string): string | null {
  let compact = value.trim().toLowerCase();
  if (compact.startsWith("urn:uuid:")) compact = compact.slice(9);
  if (compact.startsWith("{") && compact.endsWith("}")) compact = compact.slice(1, -1);
  compact = compact.replaceAll("-", "");
  if (!/^[0-9a-f]{32}$/.test(compact)) return null;
  return `${compact.slice(0, 8)}-${compact.slice(8, 12)}-${compact.slice(12, 16)}-${compact.slice(16, 20)}-${compact.slice(20)}`;
}

export function isResultManifest(value: unknown): value is PublicResultManifest {
  if (!isRecord(value) || !exactKeys(value, ["schema_version", "job_id", "created_at", "summary", "frames"])) return false;
  const summary = value.summary;
  if (!isRecord(summary) || !exactKeys(summary, ["frames_saved", "candidates", "shortlisted", "duplicates_removed", "processing_seconds", "duration_seconds"])) return false;
  if (![summary.frames_saved, summary.candidates, summary.shortlisted, summary.duplicates_removed].every((item) => isSafeInteger(item))) return false;
  if (![summary.processing_seconds, summary.duration_seconds].every(isNonNegativeNumber)) return false;
  if (value.schema_version !== 1 || typeof value.job_id !== "string" || canonicalUuid(value.job_id) === null || !isUtcDate(value.created_at) || !Array.isArray(value.frames) || value.frames.length > 10_000 || summary.frames_saved !== value.frames.length) return false;
  return value.frames.every((candidate, expectedIndex) => {
    if (!isRecord(candidate) || !exactKeys(candidate, ["index", "filename", "content_type", "size_bytes", "sha256", "timestamp_ms", "width", "height", "access_url"])) return false;
    if (candidate.index !== expectedIndex) return false;
    const expectedFilename = `frame_${String(expectedIndex).padStart(6, "0")}_${candidate.timestamp_ms}ms_${candidate.width}x${candidate.height}.jpg`;
    return candidate.filename === expectedFilename &&
      candidate.content_type === "image/jpeg" && isSafeInteger(candidate.size_bytes, 1) &&
      typeof candidate.sha256 === "string" && /^[0-9a-f]{64}$/.test(candidate.sha256) &&
      isSafeInteger(candidate.timestamp_ms) && isSafeInteger(candidate.width, 1) &&
      isSafeInteger(candidate.height, 1) && typeof candidate.access_url === "string" &&
      candidate.access_url === `/api/v1/jobs/${encodeURIComponent(value.job_id as string)}/result/frames/${candidate.index}/access`;
  });
}

export function isFrameAccess(value: unknown): value is FrameAccessResponse {
  if (!isRecord(value) || !exactKeys(value, ["url", "expires_at", "content_type", "size_bytes", "sha256"])) return false;
  let url: URL;
  try { url = new URL(value.url as string); } catch { return false; }
  return ["http:", "https:"].includes(url.protocol) && !url.username && !url.password &&
    isUtcDate(value.expires_at) && value.content_type === "image/jpeg" &&
    isSafeInteger(value.size_bytes, 1) && typeof value.sha256 === "string" && /^[0-9a-f]{64}$/.test(value.sha256);
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

export async function getJobResult(jobId: string, signal?: AbortSignal): Promise<PublicResultManifest> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/result`, { cache: "no-store", signal });
  if (!response.ok) throw await errorFromResponse(response);
  const payload: unknown = await response.json();
  const expectedId = canonicalUuid(jobId);
  if (!isResultManifest(payload) || expectedId === null || canonicalUuid(payload.job_id) !== expectedId) throw new ApiError(502, "MANIFEST_INVALID");
  return payload;
}

export async function getDatasetResult(jobId: string, signal?: AbortSignal): Promise<PublicDatasetManifest> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/result`, { cache: "no-store", signal });
  if (!response.ok) throw await errorFromResponse(response);
  const value: unknown = await response.json();
  if (!isRecord(value) || value.dataset_type !== "image" || value.schema_version !== 1 || canonicalUuid(String(value.job_id)) === null || !isRecord(value.summary) || !Array.isArray(value.images) || !Array.isArray(value.recommended_indices) || typeof value.accepted_download_url !== "string" || typeof value.yolo_download_url !== "string") throw new ApiError(502, "MANIFEST_INVALID");
  const categories = new Set(["normal", "challenging", "unusable", "rejected"]);
  if (!value.images.every((item, index) => isRecord(item) && item.index === index && typeof item.filename === "string" && categories.has(String(item.quality_category)) && typeof item.duplicate === "boolean" && (item.access_url === null || typeof item.access_url === "string") && (item.download_url === null || typeof item.download_url === "string"))) throw new ApiError(502, "MANIFEST_INVALID");
  return value as unknown as PublicDatasetManifest;
}

export async function createFrameAccess(jobId: string, frameIndex: number, signal?: AbortSignal): Promise<FrameAccessResponse> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/result/frames/${frameIndex}/access`, {
    method: "POST", cache: "no-store", signal,
  });
  if (!response.ok) throw await errorFromResponse(response);
  const payload: unknown = await response.json();
  if (!isFrameAccess(payload)) throw new ApiError(502, "MANIFEST_INVALID");
  return payload;
}

export async function getManifestDownload(jobId: string, signal?: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/result/manifest`, { cache: "no-store", signal });
  if (!response.ok) throw await errorFromResponse(response);
  if (!(response.headers.get("content-type") ?? "").toLowerCase().startsWith("application/json")) throw new ApiError(502, "MANIFEST_INVALID");
  return response.blob();
}

function isFrameExport(value: unknown): value is FrameExportResponse {
  if (!isRecord(value) || !exactKeys(value, ["id", "job_id", "status", "mode", "frame_count", "created_at", "completed_at", "status_url", "download_url", "failure_code"])) return false;
  return canonicalUuid(value.id as string) !== null && canonicalUuid(value.job_id as string) !== null &&
    ["PREPARING", "READY", "FAILED"].includes(value.status as string) && ["all", "selected"].includes(value.mode as string) &&
    isSafeInteger(value.frame_count) && isUtcDate(value.created_at) && (value.completed_at === null || isUtcDate(value.completed_at)) &&
    typeof value.status_url === "string" && (value.download_url === null || typeof value.download_url === "string") &&
    (value.failure_code === null || typeof value.failure_code === "string");
}

export async function createFrameExport(jobId: string, mode: "all" | "selected", frameIndices: number[] | null, signal?: AbortSignal): Promise<FrameExportResponse> {
  const response = await fetch(`/api/v1/jobs/${encodeURIComponent(jobId)}/exports`, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(mode === "all" ? { mode } : { mode, frame_indices: frameIndices }), cache: "no-store", signal });
  if (!response.ok) throw await errorFromResponse(response);
  const payload: unknown = await response.json(); if (!isFrameExport(payload)) throw new ApiError(502); return payload;
}

export async function getFrameExport(statusUrl: string, signal?: AbortSignal): Promise<FrameExportResponse> {
  const response = await fetch(statusUrl, { cache: "no-store", signal });
  if (!response.ok) throw await errorFromResponse(response);
  const payload: unknown = await response.json(); if (!isFrameExport(payload)) throw new ApiError(502); return payload;
}
