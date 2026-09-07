export type JobStatus =
  | "PENDING_DISPATCH"
  | "QUEUED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED";

export type SourceType = "URL" | "UPLOAD" | "IMAGE_DATASET";

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
  result_kind: "VIDEO_FRAMES" | "IMAGE_DATASET";
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

export type DatasetQuality = "normal" | "challenging" | "unusable" | "rejected";
export interface DatasetSummary {
  uploaded_files: number; accepted_files: number; normal: number; challenging: number;
  unusable: number; rejected: number; duplicates: number; ignored_metadata_entries: number;
  recommended_count: number; recommended_normal: number; recommended_challenging: number;
  target_challenging_ratio: number; actual_challenging_ratio: number; ratio_note: string | null;
}
export interface DatasetImage {
  index: number; filename: string; content_type: string; size_bytes: number; sha256: string;
  width: number; height: number; quality_category: DatasetQuality; sharpness: number;
  brightness: number; underexposed_ratio: number; overexposed_ratio: number;
  resolution_usable: boolean; duplicate: boolean; access_url: string | null; download_url: string | null;
}
export interface PublicDatasetManifest {
  schema_version: 1; dataset_type: "image"; job_id: string; created_at: string;
  summary: DatasetSummary; recommended_indices: number[]; images: DatasetImage[];
  accepted_download_url: string; yolo_download_url: string;
}
