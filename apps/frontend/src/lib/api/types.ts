export type JobStatus =
  | "PENDING_DISPATCH"
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED";

export type SourceType = "URL" | "UPLOAD";

export interface ProcessingConfig {
  candidate_fps: number;
  selection_window_seconds: number;
}

export interface JobSubmissionResponse {
  job_id: string;
  status: JobStatus;
  status_url: string;
}

export interface JobFailure {
  code: string;
  message: string;
}

export interface JobResultDescriptor {
  available: boolean;
  metadata_url: string;
  manifest_download_url: string;
}

export interface JobStatusResponse {
  id: string;
  status: JobStatus;
  source_type: SourceType;
  source: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  failure: JobFailure | null;
  result: JobResultDescriptor | null;
}

export interface ApiErrorPayload {
  detail?: string | { code?: string; message?: string } | unknown[];
}

export interface ResultSummary {
  frames_saved: number;
  candidates: number;
  shortlisted: number;
  duplicates_removed: number;
  processing_seconds: number;
  duration_seconds: number;
}

export interface ResultFrame {
  index: number;
  filename: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
  timestamp_ms: number;
  width: number;
  height: number;
  access_url: string;
}

export interface PublicResultManifest {
  schema_version: 1;
  job_id: string;
  created_at: string;
  summary: ResultSummary;
  frames: ResultFrame[];
}

export interface FrameAccessResponse {
  url: string;
  expires_at: string;
  content_type: string;
  size_bytes: number;
  sha256: string;
}

export type FrameExportStatus = "PREPARING" | "READY" | "FAILED";
export interface FrameExportResponse {
  id: string; job_id: string; status: FrameExportStatus; mode: "all" | "selected";
  frame_count: number; created_at: string; completed_at: string | null;
  status_url: string; download_url: string | null; failure_code: string | null;
}
