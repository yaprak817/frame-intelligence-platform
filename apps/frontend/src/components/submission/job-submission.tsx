"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useRef, useState } from "react";
import { submitUrlJob } from "@/lib/api/client";
import { userErrorMessage } from "@/lib/api/errors";
import { uploadVideo } from "@/lib/api/upload";
import {
  idempotencyKeyFor,
  type SubmissionIdentity,
  uploadSignature,
  urlSignature,
} from "@/lib/idempotency";
import { ProcessingFields } from "./processing-fields";

type Mode = "upload" | "url";
type Errors = Record<string, string>;
const ACCEPTED_TYPES = ".avi,.m4v,.mkv,.mov,.mp4,.webm,video/x-msvideo,video/x-m4v,video/x-matroska,video/quicktime,video/mp4,video/webm";

function validateNumber(value: string, maximum: number, label: string): string | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 && parsed <= maximum
    ? null
    : `${label} 0'dan büyük ve en fazla ${maximum} olmalı.`;
}

function validHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol) && !parsed.username && !parsed.password;
  } catch {
    return false;
  }
}

export function JobSubmission() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("upload");
  const [url, setUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [candidateFps, setCandidateFps] = useState("5");
  const [windowSeconds, setWindowSeconds] = useState("1");
  const [errors, setErrors] = useState<Errors>({});
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const identity = useRef<SubmissionIdentity | null>(null);
  const abort = useRef<(() => void) | null>(null);
  const submitting = useRef(false);
  const firstError = useRef<HTMLDivElement>(null);

  useEffect(() => () => abort.current?.(), []);
  useEffect(() => {
    if (Object.keys(errors).length) firstError.current?.focus();
  }, [errors]);

  function changeMode(next: Mode) {
    if (busy) return;
    setMode(next);
    setErrors({});
    setRequestError(null);
    identity.current = null;
  }

  function validation(): Errors {
    const next: Errors = {};
    const fpsError = validateNumber(candidateFps, 60, "Aday kare değeri");
    const windowError = validateNumber(windowSeconds, 3600, "Seçim penceresi");
    if (fpsError) next.candidateFps = fpsError;
    if (windowError) next.windowSeconds = windowError;
    if (mode === "url" && !validHttpUrl(url)) next.url = "Geçerli bir HTTP veya HTTPS video bağlantısı girin.";
    if (mode === "upload" && !file) next.file = "Bir video dosyası seçin.";
    return next;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    const nextErrors = validation();
    setErrors(nextErrors);
    setRequestError(null);
    if (Object.keys(nextErrors).length) return;
    submitting.current = true;

    const processing = {
      candidate_fps: Number(candidateFps),
      selection_window_seconds: Number(windowSeconds),
    };
    const signature = mode === "url"
      ? urlSignature(url, processing.candidate_fps, processing.selection_window_seconds)
      : uploadSignature(file!, processing.candidate_fps, processing.selection_window_seconds);
    identity.current = idempotencyKeyFor(signature, identity.current);
    setBusy(true);
    setProgress(mode === "upload" ? 0 : null);

    try {
      const response = mode === "url"
        ? await submitUrlJob(url, processing, identity.current.key)
        : await (() => {
            const request = uploadVideo(file!, processing, identity.current!.key, setProgress);
            abort.current = request.abort;
            return request.promise;
          })();
      router.push(`/jobs/${response.job_id}`);
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setRequestError(userErrorMessage(error));
      }
    } finally {
      abort.current = null;
      submitting.current = false;
      setProgress(null);
      setBusy(false);
    }
  }

  function payloadChanged(action: () => void) {
    action();
    identity.current = null;
  }

  return (
    <section className="submission-card" aria-labelledby="submission-title">
      <div className="card-heading">
        <p className="eyebrow">Yeni iş</p>
        <h2 id="submission-title">Videonuzu gönderin</h2>
      </div>
      <div className="mode-switch" role="tablist" aria-label="Video kaynağı">
        <button type="button" role="tab" aria-selected={mode === "upload"} tabIndex={mode === "upload" ? 0 : -1} onClick={() => changeMode("upload")}>Dosya yükle</button>
        <button type="button" role="tab" aria-selected={mode === "url"} tabIndex={mode === "url" ? 0 : -1} onClick={() => changeMode("url")}>Video URL’si</button>
      </div>

      <form onSubmit={submit} noValidate>
        {Object.keys(errors).length > 0 && (
          <div ref={firstError} tabIndex={-1} className="alert error" role="alert">Lütfen işaretli alanları düzeltin.</div>
        )}
        {requestError && <div className="alert error" role="alert">{requestError}</div>}

        {mode === "upload" ? (
          <div className="field" key="upload-source">
            <label htmlFor="video-file">Video dosyası</label>
            <input
              id="video-file"
              type="file"
              accept={ACCEPTED_TYPES}
              disabled={busy}
              aria-invalid={Boolean(errors.file)}
              aria-describedby={errors.file ? "file-error" : "file-help"}
              onChange={(event) => payloadChanged(() => setFile(event.target.files?.[0] ?? null))}
            />
            <span id="file-help" className="hint">MP4, MOV, AVI, M4V, MKV veya WebM</span>
            {errors.file && <span id="file-error" className="field-error">{errors.file}</span>}
          </div>
        ) : (
          <div className="field" key="url-source">
            <label htmlFor="video-url">Video bağlantısı</label>
            <input
              id="video-url"
              type="url"
              value={url}
              placeholder="https://example.com/video"
              disabled={busy}
              aria-invalid={Boolean(errors.url)}
              aria-describedby={errors.url ? "url-error" : "url-help"}
              onChange={(event) => payloadChanged(() => setUrl(event.target.value))}
            />
            <span id="url-help" className="hint">Kimlik bilgisi içermeyen HTTP veya HTTPS adresi</span>
            {errors.url && <span id="url-error" className="field-error">{errors.url}</span>}
          </div>
        )}

        <ProcessingFields
          candidateFps={candidateFps}
          windowSeconds={windowSeconds}
          disabled={busy}
          errors={errors}
          onCandidateFps={(value) => payloadChanged(() => setCandidateFps(value))}
          onWindowSeconds={(value) => payloadChanged(() => setWindowSeconds(value))}
        />

        {busy && mode === "upload" && (
          <div className="upload-progress">
            <div className="progress-label"><span>Video gönderiliyor</span><span>{progress === null ? "…" : `%${progress}`}</span></div>
            {progress === null ? (
              <progress max="100" aria-label="Video yükleme ilerlemesi" />
            ) : (
              <progress max="100" value={progress} aria-label="Video yükleme ilerlemesi" />
            )}
          </div>
        )}

        <div className="form-actions">
          <button className="button primary" type="submit" disabled={busy}>{busy ? "Gönderiliyor…" : "İşi başlat"}</button>
          {busy && mode === "upload" && <button className="button secondary" type="button" onClick={() => abort.current?.()}>Yüklemeyi iptal et</button>}
        </div>
      </form>
      <p className="trust-note">Bu sürüm güvenilir, tek kiracılı ortamlar içindir; kullanıcı hesabı veya sahiplik kontrolü içermez.</p>
    </section>
  );
}
