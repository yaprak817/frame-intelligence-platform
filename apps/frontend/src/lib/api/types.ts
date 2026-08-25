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
