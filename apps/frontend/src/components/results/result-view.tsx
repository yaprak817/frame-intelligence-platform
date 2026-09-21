"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { getJobStatus, submitUrlJob } from "@/lib/api/client";
import { uploadImageDataset, uploadVideo } from "@/lib/api/upload";
import brandStyles from "@/app/brands/brands.module.css";
import { DatasetGallery } from "./dataset-gallery";
import { ResultGallery } from "./result-gallery";

const VIDEO_ACCEPT =
  ".avi,.m4v,.mkv,.mov,.mp4,.webm,video/x-msvideo,video/x-m4v,video/x-matroska,video/quicktime,video/mp4,video/webm";

const IMAGE_ACCEPT =
  ".jpg,.jpeg,.png,.webp,.zip,image/jpeg,image/png,image/webp,application/zip";

type SourceMode = "upload" | "url";

type BrandDataset = {
  id: string;
  name: string;
  job_id: string | null;
  annotation_project_id: string | null;
};

type BrandDetail = {
  datasets: BrandDataset[];
};

function validHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

async function attachJobToBrand(
  brandId: string,
  jobId: string,
  name: string,
): Promise<BrandDataset> {
  const response = await fetch(`/api/v1/brands/${encodeURIComponent(brandId)}/datasets`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name,
      job_id: jobId,
      annotation_project_id: null,
    }),
  });

  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail =
      typeof payload?.detail === "string"
        ? payload.detail
        : payload?.detail?.message;
    throw new Error(detail ?? "Yeni veri kaynağı markaya bağlanamadı.");
  }

  return response.json() as Promise<BrandDataset>;
}

export function ResultView({
  jobId,
  downloadError = null,
}: {
  jobId: string;
  downloadError?: string | null;
}) {
  return (
    <ResolvedResultView
      key={jobId}
      jobId={jobId}
      downloadError={downloadError}
    />
  );
}

function ResolvedResultView({
  jobId,
  downloadError,
}: {
  jobId: string;
  downloadError: string | null;
}) {
  const router = useRouter();
  const [dataset, setDataset] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [brandDatasets, setBrandDatasets] = useState<BrandDataset[]>([]);
  const [brandJobMeta, setBrandJobMeta] = useState<
    Record<string, { status: string; isDataset: boolean }>
  >({});

  const [modalOpen, setModalOpen] = useState(false);
  const [sourceMode, setSourceMode] = useState<SourceMode>("upload");
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [videoUrl, setVideoUrl] = useState("");
  const [framesPerMinute, setFramesPerMinute] = useState("20");
  const [videoBusy, setVideoBusy] = useState(false);
  const [videoProgress, setVideoProgress] = useState<number | null>(null);

  const [imageBusy, setImageBusy] = useState(false);
  const [imageProgress, setImageProgress] = useState<number | null>(null);
  const [labelingBusy, setLabelingBusy] = useState(false);

  const abortVideo = useRef<(() => void) | null>(null);
  const abortImages = useRef<(() => void) | null>(null);
  const imageInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const controller = new AbortController();

    void getJobStatus(jobId, controller.signal)
      .then((job) => {
        if (job.status !== "SUCCEEDED" || job.result?.available !== true) {
          setError("Kareler henüz hazır değil.");
          return;
        }

        const isDataset = job.source_type === "IMAGE_DATASET";
        if (
          (isDataset ? "IMAGE_DATASET" : "VIDEO_FRAMES") !==
          job.result.result_kind
        ) {
          setError("Sonuç türü doğrulanamadı.");
          return;
        }

        setDataset(isDataset);
        setError(null);
      })
      .catch((caught) => {
        if (!(caught instanceof DOMException && caught.name === "AbortError")) {
          setError("Sonuç türü doğrulanamadı.");
        }
      });

    return () => {
      controller.abort();
      abortVideo.current?.();
      abortImages.current?.();
    };
  }, [jobId]);

  const resolveCurrentBrandId = useCallback(async (): Promise<string | null> => {
    try {
      const cached = window.localStorage.getItem(
        `frame-intelligence:job-brand:${jobId}`,
      );
      if (cached) return cached;
    } catch {
      // Fall through to the API lookup.
    }

    try {
      const response = await fetch("/api/v1/brands", { cache: "no-store" });
      if (!response.ok) return null;
      const brands = (await response.json()) as Array<{ id?: string }>;
      for (const item of brands) {
        if (!item?.id) continue;
        const detailResponse = await fetch(
          `/api/v1/brands/${encodeURIComponent(item.id)}`,
          { cache: "no-store" },
        );
        if (!detailResponse.ok) continue;
        const detail = (await detailResponse.json()) as {
          datasets?: Array<{ job_id?: string | null }>;
        };
        if (detail.datasets?.some((dataset) => dataset.job_id === jobId)) {
          try {
            window.localStorage.setItem(
              `frame-intelligence:job-brand:${jobId}`,
              item.id,
            );
          } catch {
            // Cache is optional.
          }
          return item.id;
        }
      }
    } catch {
      return null;
    }

    return null;
  }, [jobId]);

  useEffect(() => {
    let active = true;

    void resolveCurrentBrandId().then(async (brandId) => {
      if (!active || !brandId) return;

      try {
        const response = await fetch(
          `/api/v1/brands/${encodeURIComponent(brandId)}`,
          { cache: "no-store" },
        );
        if (!response.ok || !active) return;

        const detail = (await response.json()) as BrandDetail;
        if (!active || !Array.isArray(detail.datasets)) return;

        // Backend currently returns oldest -> newest. UI is intentionally LIFO:
        // the most recently added source is always shown first.
        setBrandDatasets([...detail.datasets].reverse());
      } catch {
        // Source listing is supplementary; the existing frame gallery still works.
      }
    });

    return () => {
      active = false;
    };
  }, [jobId, resolveCurrentBrandId]);

  useEffect(() => {
    const jobs = brandDatasets
      .map((item) => item.job_id)
      .filter((value): value is string => Boolean(value));

    if (jobs.length === 0) {
      const timer = window.setTimeout(() => setBrandJobMeta({}), 0);
      return () => window.clearTimeout(timer);
    }

    let active = true;

    const refresh = async () => {
      const entries = await Promise.all(
        jobs.map(async (sourceJobId) => {
          try {
            const status = await getJobStatus(sourceJobId);
            return [
              sourceJobId,
              {
                status:
                  status.status === "SUCCEEDED" &&
                  status.result?.available === true
                    ? "SUCCEEDED"
                    : status.status === "FAILED"
                      ? "FAILED"
                      : "PENDING",
                isDataset: status.source_type === "IMAGE_DATASET",
              },
            ] as const;
          } catch {
            return null;
          }
        }),
      );

      if (!active) return;

      setBrandJobMeta((current) => {
        const next = { ...current };
        for (const entry of entries) {
          if (entry) next[entry[0]] = entry[1];
        }
        return next;
      });
    };

    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);

    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [brandDatasets]);

  async function registerNewSource(
    newJobId: string,
    name: string,
  ): Promise<BrandDataset | null> {
    const brandId = await resolveCurrentBrandId();
    if (!brandId) return null;

    const created = await attachJobToBrand(brandId, newJobId, name);
    setBrandDatasets((current) => [
      created,
      ...current.filter((item) => item.id !== created.id),
    ]);

    try {
      window.localStorage.setItem(
        `frame-intelligence:job-brand:${newJobId}`,
        brandId,
      );
    } catch {
      // Brand association already exists server-side; local mapping is only UX context.
    }
    return created;
  }

  async function startLabeling() {
    if (labelingBusy) return;

    setLabelingBusy(true);
    setError(null);
    setMessage(null);

    try {
      const brandId = await resolveCurrentBrandId();

      if (!brandId) {
        router.push(`/jobs/${encodeURIComponent(jobId)}/annotations`);
        return;
      }

      const response = await fetch(
        `/api/v1/brands/${encodeURIComponent(brandId)}/annotations?page=1&page_size=100`,
        {
          method: "POST",
          headers: { Accept: "application/json" },
        },
      );

      if (!response.ok) {
        const payload = await response.json().catch(() => null);
        const detail =
          typeof payload?.detail === "string"
            ? payload.detail
            : payload?.detail?.message;
        throw new Error(
          detail ?? "Marka etiketleme alanı hazırlanamadı.",
        );
      }

      const project = (await response.json()) as { job_id?: string };
      if (!project.job_id) {
        throw new Error("Etiketleme projesinin kaynak işi bulunamadı.");
      }

      router.push(`/jobs/${encodeURIComponent(project.job_id)}/annotations`);
    } catch (caught) {
      setError(
        caught instanceof Error
          ? caught.message
          : "Marka etiketleme alanı hazırlanamadı.",
      );
      setLabelingBusy(false);
    }
  }

  function closeModal() {
    if (videoBusy) return;
    setModalOpen(false);
    setVideoFile(null);
    setVideoUrl("");
    setVideoProgress(null);
  }

  async function addVideoFrames() {
    if (videoBusy) return;

    const fpm = Number(framesPerMinute);
    if (!Number.isFinite(fpm) || fpm <= 0 || fpm > 3600) {
      setError("Dakikada kare sayısı 0'dan büyük ve en fazla 3600 olmalı.");
      return;
    }
    if (sourceMode === "upload" && !videoFile) {
      setError("Lütfen bir video dosyası seçin.");
      return;
    }
    if (sourceMode === "url" && !validHttpUrl(videoUrl)) {
      setError("Lütfen geçerli bir HTTP veya HTTPS video bağlantısı girin.");
      return;
    }

    setVideoBusy(true);
    setVideoProgress(sourceMode === "upload" ? 0 : null);
    setMessage(null);
    setError(null);

    const processing = {
      candidate_fps: 5,
      selection_window_seconds: 60 / fpm,
    };

    try {
      let job;
      if (sourceMode === "upload") {
        const request = uploadVideo(
          videoFile!,
          processing,
          crypto.randomUUID(),
          setVideoProgress,
        );
        abortVideo.current = request.abort;
        job = await request.promise;
      } else {
        job = await submitUrlJob(
          videoUrl.trim(),
          processing,
          crypto.randomUUID(),
        );
      }

      const sourceName =
        sourceMode === "upload"
          ? videoFile!.name
          : `URL videosu - ${new URL(videoUrl).hostname}`;

      const attached = (await registerNewSource(job.job_id, sourceName)) !== null;

      setVideoFile(null);
      setVideoUrl("");
      setVideoProgress(null);
      setModalOpen(false);
      setMessage(
        attached
          ? "Yeni video markaya veri kaynağı olarak eklendi. Kare çıkarma işi başladı."
          : "Yeni video işi oluşturuldu. Kare çıkarma işi başladı.",
      );
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(cause instanceof Error ? cause.message : "Video eklenemedi.");
      }
    } finally {
      abortVideo.current = null;
      setVideoBusy(false);
    }
  }

  async function addImages(files: FileList | null) {
    if (!files || files.length === 0 || imageBusy) return;

    const selected = Array.from(files);
    const zipFiles = selected.filter((file) =>
      file.name.toLocaleLowerCase().endsWith(".zip"),
    );

    if (zipFiles.length > 0 && (zipFiles.length !== 1 || selected.length !== 1)) {
      setError("ZIP dosyasını tek başına seçin; görsellerle birlikte yüklemeyin.");
      if (imageInput.current) imageInput.current.value = "";
      return;
    }

    const archive = zipFiles.length === 1;
    setImageBusy(true);
    setImageProgress(0);
    setMessage(null);
    setError(null);

    try {
      const request = uploadImageDataset(
        selected,
        archive,
        crypto.randomUUID(),
        setImageProgress,
      );
      abortImages.current = request.abort;
      const job = await request.promise;

      const sourceName = archive
        ? selected[0].name
        : `${selected.length} görsel`;

      const attached = (await registerNewSource(job.job_id, sourceName)) !== null;
      setMessage(
        attached
          ? "Görsel / ZIP markaya veri kaynağı olarak eklendi. İşleme başladı."
          : "Görsel / ZIP işi oluşturuldu. İşleme başladı.",
      );
    } catch (cause) {
      if (!(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(
          cause instanceof Error
            ? cause.message
            : "Görsel / ZIP eklenemedi.",
        );
      }
    } finally {
      abortImages.current = null;
      setImageBusy(false);
      setImageProgress(null);
      if (imageInput.current) imageInput.current.value = "";
    }
  }

  if (error && dataset === null) {
    return (
      <main className="page-shell">
        <div className="alert error" role="alert">{error}</div>
      </main>
    );
  }

  if (dataset === null) {
    return (
      <main className="page-shell">
        <div className="status-message" role="status">
          Kareler hazırlanıyor…
        </div>
      </main>
    );
  }

  return (
    <>
      <div className="page-shell" style={{ paddingBottom: 0 }}>
        <section className={brandStyles.card}>
          <h2>Veri ekle</h2>
          <p className={brandStyles.muted}>
            Yeni kaynaklar aynı marka çalışma alanında ayrı veri setleri olarak tutulur.
          </p>

          <div className={brandStyles.actions}>
            <button
              className={brandStyles.button}
              type="button"
              onClick={() => {
                setError(null);
                setMessage(null);
                setModalOpen(true);
              }}
            >
              ▣ Videodan Kare Ekle
            </button>

            <button
              className={brandStyles.secondary}
              type="button"
              onClick={() =>
                document
                  .getElementById("result-frame-gallery")
                  ?.scrollIntoView({ behavior: "smooth", block: "start" })
              }
            >
              ▧ Kare Seç
            </button>

            <button
              className={brandStyles.secondary}
              type="button"
              disabled={imageBusy}
              onClick={() => imageInput.current?.click()}
            >
              {imageBusy ? "Ekleniyor…" : "＋ Görsel / ZIP Ekle"}
            </button>

            <input
              ref={imageInput}
              type="file"
              accept={IMAGE_ACCEPT}
              multiple
              hidden
              disabled={imageBusy}
              onChange={(event) => void addImages(event.target.files)}
            />
          </div>

          {imageBusy && (
            <div style={{ marginTop: 14 }}>
              <progress
                max="100"
                value={imageProgress ?? undefined}
                style={{ width: "100%" }}
              />
              <div className={brandStyles.muted}>
                {imageProgress === null
                  ? "Yükleniyor…"
                  : `Yükleniyor: %${imageProgress}`}
              </div>
            </div>
          )}

        </section>

        {message && (
          <div
            style={{
              marginTop: 14,
              padding: "12px 14px",
              borderRadius: 12,
              background: "#ecfdf5",
              color: "#166534",
              fontWeight: 700,
            }}
          >
            {message}
          </div>
        )}

        {error && (
          <div className="alert error" role="alert" style={{ marginTop: 14 }}>
            {error}
          </div>
        )}

        <div style={{ marginTop: 14 }}>
          <button
            className="button primary"
            type="button"
            onClick={() => void startLabeling()}
            disabled={labelingBusy}
          >
            {labelingBusy
              ? "Etiketleme alanı hazırlanıyor…"
              : "Etiketlemeye Başla"}
          </button>
        </div>
      </div>

      <div id="result-frame-gallery" className="brand-frame-stack">
        {brandDatasets.some((item) => item.job_id) ? (
          <>
            {brandDatasets
              .filter(
                (item): item is BrandDataset & { job_id: string } =>
                  Boolean(item.job_id),
              )
              .map((item) => {
                const meta = brandJobMeta[item.job_id];

                if (!meta || meta.status !== "SUCCEEDED") {
                  return (
                    <div
                      className="page-shell brand-frame-pending"
                      key={item.id}
                      role="status"
                    >
                      <div className="status-message">
                        Yeni kareler hazırlanıyor…
                      </div>
                    </div>
                  );
                }

                return (
                  <div className="brand-frame-source" key={item.id}>
                    {meta.isDataset ? (
                      <DatasetGallery
                        jobId={item.job_id}
                        initialDownloadError={
                          item.job_id === jobId ? downloadError : null
                        }
                      />
                    ) : (
                      <ResultGallery jobId={item.job_id} />
                    )}
                  </div>
                );
              })}
          </>
        ) : dataset ? (
          <DatasetGallery
            jobId={jobId}
            initialDownloadError={downloadError}
          />
        ) : (
          <ResultGallery jobId={jobId} />
        )}
      </div>

      <style jsx global>{`
        /*
         * All brand sources intentionally render as one frame wall.
         * The source-specific result headers/summaries stay available on each job's
         * own page, but are hidden here so the user sees one continuous stack.
         */
        .brand-frame-stack .brand-frame-source .back-link,
        .brand-frame-stack .brand-frame-source .result-heading,
        .brand-frame-stack .brand-frame-source .summary-card,
        .brand-frame-stack .brand-frame-source .gallery-heading {
          display: none !important;
        }

        .brand-frame-stack .brand-frame-source > .page-shell.result-shell {
          padding-top: 0 !important;
          padding-bottom: 0 !important;
        }

        .brand-frame-stack .brand-frame-source > .page-shell.result-shell > section {
          margin-top: 0 !important;
        }

        .brand-frame-stack .brand-frame-source + .brand-frame-source {
          margin-top: 0 !important;
        }

        .brand-frame-stack .brand-frame-pending {
          padding-top: 10px !important;
          padding-bottom: 10px !important;
        }
      `}</style>

      {modalOpen && (
        <div
          role="presentation"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeModal();
          }}
          style={{
            position: "fixed",
            inset: 0,
            zIndex: 1000,
            display: "grid",
            placeItems: "center",
            padding: 20,
            background: "rgba(15, 23, 42, .42)",
          }}
        >
          <section
            role="dialog"
            aria-modal="true"
            aria-labelledby="video-frame-modal-title"
            style={{
              width: "min(720px, 100%)",
              maxHeight: "90vh",
              overflow: "auto",
              borderRadius: 18,
              background: "white",
              boxShadow: "0 30px 80px rgba(15, 23, 42, .28)",
            }}
          >
            <div
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                gap: 20,
                padding: "18px 22px",
                borderBottom: "1px solid #e5e7eb",
              }}
            >
              <h2
                id="video-frame-modal-title"
                style={{ margin: 0, fontSize: 22 }}
              >
                Videodan Kare Ekle
              </h2>
              <button
                type="button"
                onClick={closeModal}
                disabled={videoBusy}
                aria-label="Kapat"
                style={{
                  width: 44,
                  height: 44,
                  border: 0,
                  borderRadius: 12,
                  background: "#6d28d9",
                  color: "white",
                  fontSize: 26,
                  cursor: "pointer",
                }}
              >
                ×
              </button>
            </div>

            <div style={{ padding: 22 }}>
              <div style={{ marginBottom: 18 }}>
                <div style={{ marginBottom: 10, fontWeight: 800 }}>
                  Video Kaynağı *
                </div>
                <div
                  style={{ display: "flex", gap: 10, flexWrap: "wrap" }}
                >
                  <button
                    type="button"
                    disabled={videoBusy}
                    onClick={() => setSourceMode("upload")}
                    style={{
                      minHeight: 42,
                      padding: "0 16px",
                      borderRadius: 10,
                      border:
                        sourceMode === "upload"
                          ? "2px solid #6d28d9"
                          : "1px solid #dbe2ea",
                      background:
                        sourceMode === "upload" ? "#f5f3ff" : "white",
                      color: "#334155",
                      fontWeight: 800,
                      cursor: "pointer",
                    }}
                  >
                    ⇧ Yükle
                  </button>
                  <button
                    type="button"
                    disabled={videoBusy}
                    onClick={() => setSourceMode("url")}
                    style={{
                      minHeight: 42,
                      padding: "0 16px",
                      borderRadius: 10,
                      border:
                        sourceMode === "url"
                          ? "2px solid #6d28d9"
                          : "1px solid #dbe2ea",
                      background:
                        sourceMode === "url" ? "#f5f3ff" : "white",
                      color: "#334155",
                      fontWeight: 800,
                      cursor: "pointer",
                    }}
                  >
                    🔗 URL&apos;den İçe Aktar
                  </button>
                </div>
              </div>

              {sourceMode === "upload" ? (
                <div style={{ marginBottom: 20 }}>
                  <label
                    htmlFor="result-video-file"
                    style={{
                      display: "block",
                      marginBottom: 8,
                      fontWeight: 800,
                    }}
                  >
                    Video Seç *
                  </label>
                  <input
                    id="result-video-file"
                    className={brandStyles.input}
                    type="file"
                    accept={VIDEO_ACCEPT}
                    disabled={videoBusy}
                    onChange={(event) =>
                      setVideoFile(event.target.files?.[0] ?? null)
                    }
                  />
                </div>
              ) : (
                <div style={{ marginBottom: 20 }}>
                  <label
                    htmlFor="result-video-url"
                    style={{
                      display: "block",
                      marginBottom: 8,
                      fontWeight: 800,
                    }}
                  >
                    Video URL&apos;si *
                  </label>
                  <input
                    id="result-video-url"
                    className={brandStyles.input}
                    type="url"
                    value={videoUrl}
                    disabled={videoBusy}
                    placeholder="https://example.com/video.mp4"
                    onChange={(event) => setVideoUrl(event.target.value)}
                  />
                </div>
              )}

              <div style={{ marginBottom: 20 }}>
                <label
                  htmlFor="result-frames-per-minute"
                  style={{
                    display: "block",
                    marginBottom: 8,
                    fontWeight: 800,
                  }}
                >
                  Dakikada kare sayısı *
                </label>
                <input
                  id="result-frames-per-minute"
                  className={brandStyles.input}
                  type="number"
                  min="1"
                  max="3600"
                  step="1"
                  value={framesPerMinute}
                  disabled={videoBusy}
                  onChange={(event) =>
                    setFramesPerMinute(event.target.value)
                  }
                />
                <div
                  className={brandStyles.muted}
                  style={{ marginTop: 7 }}
                >
                  Önerilen: çoğu kullanım için 1–20 kare/dakika.
                </div>
              </div>

              <label
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 10,
                  color: "#64748b",
                  marginBottom: 8,
                }}
              >
                <input type="checkbox" disabled />
                Zaman aralığı belirle
              </label>
              <div
                className={brandStyles.muted}
                style={{ fontSize: 13 }}
              >
                Zaman aralığı seçimi sonraki adımda bağlanacak; şu an tüm video
                işlenir.
              </div>

              {videoBusy && (
                <div style={{ marginTop: 18 }}>
                  <progress
                    max="100"
                    value={videoProgress ?? undefined}
                    style={{ width: "100%" }}
                  />
                  <div className={brandStyles.muted}>
                    {sourceMode === "url"
                      ? "Video işi oluşturuluyor…"
                      : videoProgress === null
                        ? "Video yükleniyor…"
                        : `Video yükleniyor: %${videoProgress}`}
                  </div>
                </div>
              )}
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: 10,
                padding: "16px 22px",
                borderTop: "1px solid #e5e7eb",
                background: "#f8fafc",
              }}
            >
              <button
                type="button"
                className={brandStyles.secondary}
                disabled={videoBusy}
                onClick={closeModal}
              >
                İptal
              </button>
              <button
                type="button"
                className={brandStyles.button}
                disabled={
                  videoBusy ||
                  !framesPerMinute ||
                  (sourceMode === "upload"
                    ? !videoFile
                    : !videoUrl.trim())
                }
                onClick={() => void addVideoFrames()}
              >
                {videoBusy ? "Ekleniyor…" : "＋ Kare Ekle"}
              </button>
            </div>
          </section>
        </div>
      )}
    </>
  );
}
