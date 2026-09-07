"use client";

import { useRouter } from "next/navigation";
import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import { submitUrlJob } from "@/lib/api/client";
import { userErrorMessage } from "@/lib/api/errors";
import { uploadImageDataset, uploadVideo } from "@/lib/api/upload";
import {
  idempotencyKeyFor,
  type SubmissionIdentity,
  uploadSignature,
  urlSignature,
} from "@/lib/idempotency";
import { ProcessingFields } from "./processing-fields";

type Mode = "upload" | "url" | "dataset";
type DatasetMode = "images" | "zip";
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
  const [datasetMode, setDatasetMode] = useState<DatasetMode>("images");
  const [datasetFiles, setDatasetFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
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
  const mounted = useRef(true);
  const generation = useRef(0);
  const tabs = useRef<Array<HTMLButtonElement | null>>([]);

  useEffect(() => () => {
    mounted.current = false;
    generation.current += 1;
    abort.current?.();
  }, []);
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
    if (mode === "dataset" && datasetFiles.length === 0) next.dataset = "En az bir görsel veya bir ZIP seçin.";
    if (mode === "dataset" && datasetMode === "zip" && datasetFiles.length !== 1) next.dataset = "Yalnız bir ZIP dosyası seçin.";
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
    const operation = ++generation.current;

    const processing = {
      candidate_fps: Number(candidateFps),
      selection_window_seconds: Number(windowSeconds),
    };
    const signature = mode === "url"
      ? urlSignature(url, processing.candidate_fps, processing.selection_window_seconds)
      : mode === "upload"
        ? uploadSignature(file!, processing.candidate_fps, processing.selection_window_seconds)
        : `dataset:${datasetMode}:${datasetFiles.map((item) => `${item.name}:${item.size}:${item.lastModified}`).join("|")}`;
    identity.current = idempotencyKeyFor(signature, identity.current);
    setBusy(true);
    setProgress(mode === "upload" || mode === "dataset" ? 0 : null);
    const updateProgress = (value: number | null) => {
      if (mounted.current && generation.current === operation) setProgress(value);
    };

    try {
      const response = mode === "url"
        ? await submitUrlJob(url, processing, identity.current.key)
        : mode === "upload" ? await (() => {
            const request = uploadVideo(file!, processing, identity.current!.key, updateProgress);
            abort.current = request.abort;
            return request.promise;
          })() : await (() => {
            const request = uploadImageDataset(datasetFiles, datasetMode === "zip", identity.current!.key, updateProgress);
            abort.current = request.abort;
            return request.promise;
          })();
      if (mounted.current && generation.current === operation) router.push(`/jobs/${response.job_id}`);
    } catch (error) {
      if (mounted.current && generation.current === operation && !(error instanceof DOMException && error.name === "AbortError")) {
        setRequestError(userErrorMessage(error));
      }
    } finally {
      if (!mounted.current || generation.current !== operation) return;
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

  function cancelUpload() {
    generation.current += 1;
    abort.current?.();
    abort.current = null;
    submitting.current = false;
    setProgress(null);
    setBusy(false);
  }

  function tabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const order: Mode[] = ["upload", "url", "dataset"];
    const current = order.indexOf(mode);
    let next = current;
    if (event.key === "ArrowRight") next = (current + 1) % order.length;
    else if (event.key === "ArrowLeft") next = (current - 1 + order.length) % order.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = order.length - 1;
    else return;
    event.preventDefault();
    changeMode(order[next]);
    queueMicrotask(() => tabs.current[next]?.focus());
  }

  function chooseDataset(next: File[]) {
    const accepted = datasetMode === "zip"
      ? next.filter((item) => item.name.toLowerCase().endsWith(".zip")).slice(0, 1)
      : next.filter((item) => /\.(jpe?g|png|webp)$/i.test(item.name));
    payloadChanged(() => setDatasetFiles(accepted));
  }

  return (
    <section className="submission-card" aria-labelledby="submission-title">
      <div className="card-heading">
        <p className="eyebrow">Yeni iş</p>
        <h2 id="submission-title">İçeriğinizi gönderin</h2>
      </div>
      <div className="mode-switch" role="tablist" aria-label="İş kaynağı" onKeyDown={tabKeyDown}>
        <button ref={(node) => { tabs.current[0] = node; }} type="button" role="tab" aria-selected={mode === "upload"} tabIndex={mode === "upload" ? 0 : -1} onClick={() => changeMode("upload")}>Dosya yükle</button>
        <button ref={(node) => { tabs.current[1] = node; }} type="button" role="tab" aria-selected={mode === "url"} tabIndex={mode === "url" ? 0 : -1} onClick={() => changeMode("url")}>Video URL’si</button>
        <button ref={(node) => { tabs.current[2] = node; }} type="button" role="tab" aria-selected={mode === "dataset"} tabIndex={mode === "dataset" ? 0 : -1} onClick={() => changeMode("dataset")}>Görsel veri seti</button>
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
        ) : mode === "url" ? (
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
        ) : (
          <div className="field" key="dataset-source">
            <fieldset className="dataset-mode">
              <legend>Yükleme biçimi</legend>
              <label><input type="radio" name="dataset-mode" checked={datasetMode === "images"} disabled={busy} onChange={() => payloadChanged(() => { setDatasetMode("images"); setDatasetFiles([]); })} /> Görseller</label>
              <label><input type="radio" name="dataset-mode" checked={datasetMode === "zip"} disabled={busy} onChange={() => payloadChanged(() => { setDatasetMode("zip"); setDatasetFiles([]); })} /> ZIP arşivi</label>
            </fieldset>
            <label
              className={`drop-zone${dragging ? " dragging" : ""}`}
              onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={(event) => { event.preventDefault(); setDragging(false); chooseDataset(Array.from(event.dataTransfer.files)); }}
            >
              <span>{datasetMode === "zip" ? "Bir ZIP seçin veya buraya bırakın" : "Görselleri seçin veya buraya bırakın"}</span>
              <input
                type="file"
                multiple={datasetMode === "images"}
                accept={datasetMode === "zip" ? ".zip,application/zip" : ".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp"}
                disabled={busy}
                aria-invalid={Boolean(errors.dataset)}
                onChange={(event) => {
                  chooseDataset(Array.from(event.target.files ?? []));
                  event.currentTarget.value = "";
                }}
              />
            </label>
            <span className="hint">{datasetFiles.length} dosya · {new Intl.NumberFormat("tr-TR", { style: "unit", unit: "megabyte", maximumFractionDigits: 2 }).format(datasetFiles.reduce((sum, item) => sum + item.size, 0) / 1_048_576)}</span>
            {datasetFiles.length > 0 && <button className="button secondary" type="button" disabled={busy} onClick={() => payloadChanged(() => setDatasetFiles([]))}>Seçimi temizle</button>}
            {errors.dataset && <span className="field-error">{errors.dataset}</span>}
          </div>
        )}

        {mode !== "dataset" && <ProcessingFields
          candidateFps={candidateFps}
          windowSeconds={windowSeconds}
          disabled={busy}
          errors={errors}
          onCandidateFps={(value) => payloadChanged(() => setCandidateFps(value))}
          onWindowSeconds={(value) => payloadChanged(() => setWindowSeconds(value))}
        />}

        {busy && (mode === "upload" || mode === "dataset") && (
          <div className="upload-progress">
            <div className="progress-label"><span>{mode === "dataset" ? "Veri seti gönderiliyor" : "Video gönderiliyor"}</span><span>{progress === null ? "…" : `%${progress}`}</span></div>
            {progress === null ? (
              <progress max="100" aria-label={mode === "dataset" ? "Görsel veri seti yükleme ilerlemesi" : "Video yükleme ilerlemesi"} />
            ) : (
              <progress max="100" value={progress} aria-label={mode === "dataset" ? "Görsel veri seti yükleme ilerlemesi" : "Video yükleme ilerlemesi"} />
            )}
          </div>
        )}

        <div className="form-actions">
          <button className="button primary" type="submit" disabled={busy}>{busy ? "Gönderiliyor…" : "İşi başlat"}</button>
          {busy && mode !== "url" && <button className="button secondary" type="button" onClick={cancelUpload}>Yüklemeyi iptal et</button>}
        </div>
      </form>
      <p className="trust-note">Bu sürüm güvenilir, tek kiracılı ortamlar içindir; kullanıcı hesabı veya sahiplik kontrolü içermez.</p>
    </section>
  );
}
