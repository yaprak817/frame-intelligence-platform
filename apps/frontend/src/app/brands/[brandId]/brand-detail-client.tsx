"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { submitUrlJob } from "@/lib/api/client";
import { uploadImageDataset, uploadVideo } from "@/lib/api/upload";
import styles from "../brands.module.css";

const VIDEO_ACCEPT =
  ".avi,.m4v,.mkv,.mov,.mp4,.webm,video/x-msvideo,video/x-m4v,video/x-matroska,video/quicktime,video/mp4,video/webm";

type SourceMode = "upload" | "url";

type BrandClass = {
  id: string;
  name: string;
  color: string;
};

type BrandDataset = {
  id: string;
  name: string;
  job_id: string | null;
  annotation_project_id: string | null;
};

type BrandDetail = {
  id: string;
  name: string;
  status: string;
  created_at: string;
  updated_at: string;
  classes: BrandClass[];
  datasets: BrandDataset[];
};

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!response.ok) {
    throw new Error("İşlem tamamlanamadı.");
  }
  return response.json() as Promise<T>;
}

function validHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol);
  } catch {
    return false;
  }
}

export function BrandDetailClient({ brandId }: { brandId: string }) {
  const [brand, setBrand] = useState<BrandDetail | null>(null);
  const [className, setClassName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [modalOpen, setModalOpen] = useState(false);
  const [sourceMode, setSourceMode] = useState<SourceMode>("upload");
  const [videoFile, setVideoFile] = useState<File | null>(null);
  const [videoUrl, setVideoUrl] = useState("");
  const [framesPerMinute, setFramesPerMinute] = useState("20");
  const [videoBusy, setVideoBusy] = useState(false);
  const [videoProgress, setVideoProgress] = useState<number | null>(null);
  const [videoMessage, setVideoMessage] = useState<string | null>(null);
  const [imageBusy, setImageBusy] = useState(false);
  const imageInput = useRef<HTMLInputElement>(null);
  const singleImageInput = useRef<HTMLInputElement>(null);
  const abortVideo = useRef<(() => void) | null>(null);
  const abortImages = useRef<(() => void) | null>(null);
  const mutationControllers = useRef<Set<AbortController>>(new Set());
  const mutationGeneration = useRef(0);
  const activeBrandId = useRef(brandId);
  const mounted = useRef(false);
  const classLocked = useRef(false);
  const videoLocked = useRef(false);
  const imageLocked = useRef(false);
  const loadGeneration = useRef(0);

  useLayoutEffect(() => {
    activeBrandId.current = brandId;
  }, [brandId]);

  const load = useCallback(async (signal: AbortSignal, generation: number) => {
    try {
      const loaded = await api<BrandDetail>(`/brands/${brandId}`, { signal });
      if (signal.aborted || loadGeneration.current !== generation) return;
      setBrand({
        ...loaded,
        datasets: [...loaded.datasets].reverse(),
      });
      for (const dataset of loaded.datasets) {
        if (dataset.job_id) {
          try {
            window.localStorage.setItem(
              `frame-intelligence:job-brand:${dataset.job_id}`,
              brandId,
            );
          } catch {
            // Local mapping is only navigation context; server data remains authoritative.
          }
        }
      }
    } catch (cause) {
      if (!signal.aborted && loadGeneration.current === generation) {
        setError(cause instanceof Error ? cause.message : "Marka yüklenemedi.");
      }
    }
  }, [brandId]);

  useEffect(() => {
    mounted.current = true;
    const mutationId = ++mutationGeneration.current;
    const controller = new AbortController();
    const generation = ++loadGeneration.current;
    const pendingControllers = mutationControllers.current;
    queueMicrotask(() => {
      if (mutationGeneration.current !== mutationId) return;
      setBusy(false);
      setVideoBusy(false);
      setImageBusy(false);
      setError(null);
      setVideoMessage(null);
      setModalOpen(false);
      setClassName("");
      setVideoFile(null);
      setVideoUrl("");
      setVideoProgress(null);
      setFramesPerMinute("20");
      setSourceMode("upload");
    });
    void load(controller.signal, generation);
    return () => {
      if (mutationGeneration.current === mutationId) mutationGeneration.current += 1;
      mounted.current = false;
      loadGeneration.current += 1;
      controller.abort();
      for (const pending of pendingControllers) pending.abort();
      pendingControllers.clear();
      abortVideo.current?.();
      abortImages.current?.();
      classLocked.current = false;
      videoLocked.current = false;
      imageLocked.current = false;
    };
  }, [brandId, load]);

  function mutationContext() {
    const requestedBrand = brandId;
    const generation = mutationGeneration.current;
    return () => mounted.current && activeBrandId.current === requestedBrand && mutationGeneration.current === generation;
  }

  async function addClass(event: FormEvent) {
    event.preventDefault();
    if (brand?.id !== brandId || !className.trim() || classLocked.current) return;
    classLocked.current = true;
    const current = mutationContext();
    const controller = new AbortController();
    mutationControllers.current.add(controller);
    setBusy(true);
    setError(null);
    try {
      const created = await api<BrandClass>(`/brands/${brandId}/classes`, {
        method: "POST",
        body: JSON.stringify({ name: className }),
        signal: controller.signal,
      });
      if (!current()) return;
      setBrand((current) =>
        current ? { ...current, classes: [...current.classes, created] } : current,
      );
      setClassName("");
    } catch (cause) {
      if (current() && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Sınıf eklenemedi.");
    } finally {
      mutationControllers.current.delete(controller);
      if (current()) { classLocked.current = false; setBusy(false); }
    }
  }

  function closeModal() {
    if (videoLocked.current) return;
    setModalOpen(false);
    setVideoFile(null);
    setVideoUrl("");
    setVideoProgress(null);
  }

  async function addVideoFrames() {
    if (brand?.id !== brandId || videoLocked.current) return;
    const current = mutationContext();
    const controller = new AbortController();

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

    videoLocked.current = true;
    mutationControllers.current.add(controller);
    setVideoBusy(true);
    setVideoProgress(sourceMode === "upload" ? 0 : null);
    setVideoMessage(null);
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
          (progress) => { if (current()) setVideoProgress(progress); },
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
      if (!current()) return;

      const datasetName =
        sourceMode === "upload"
          ? videoFile!.name
          : `URL videosu - ${new URL(videoUrl).hostname}`;

      const dataset = await api<BrandDataset>(`/brands/${brandId}/datasets`, {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          name: datasetName,
          job_id: job.job_id,
          annotation_project_id: null,
        }),
      });
      if (!current()) return;

      setBrand((current) =>
        current ? { ...current, datasets: [dataset, ...current.datasets] } : current,
      );
      try {
        window.localStorage.setItem(
          `frame-intelligence:job-brand:${job.job_id}`,
          brandId,
        );
      } catch {
        // Local mapping is only navigation context; server data remains authoritative.
      }
      setVideoMessage("Video markaya eklendi. Kare çıkarma işi başladı.");
      setVideoFile(null);
      setVideoUrl("");
      setVideoProgress(null);
      setModalOpen(false);
    } catch (cause) {
      if (current() && !(cause instanceof DOMException && cause.name === "AbortError")) {
        setError(cause instanceof Error ? cause.message : "Video eklenemedi.");
      }
    } finally {
      mutationControllers.current.delete(controller);
      if (current()) { abortVideo.current = null; videoLocked.current = false; setVideoBusy(false); }
    }
  }

  async function addImages(files: FileList | null) {
    if (brand?.id !== brandId || !files || files.length === 0 || imageLocked.current) return;
    const current = mutationContext();
    const controller = new AbortController();

    const selected = Array.from(files);
    const zipFiles = selected.filter((file) =>
      file.name.toLocaleLowerCase().endsWith(".zip"),
    );

    if (zipFiles.length > 0 && (zipFiles.length !== 1 || selected.length !== 1)) {
      setError("ZIP dosyas\u0131n\u0131 tek ba\u015f\u0131na se\u00e7in; g\u00f6rsellerle birlikte y\u00fcklemeyin.");
      if (imageInput.current) imageInput.current.value = "";
      return;
    }

    const archive = zipFiles.length === 1;

    imageLocked.current = true;
    mutationControllers.current.add(controller);
    setImageBusy(true);
    setError(null);
    setVideoMessage(null);

    try {
      const request = uploadImageDataset(
        selected,
        archive,
        crypto.randomUUID(),
        () => {},
      );
      abortImages.current = request.abort;

      const job = await request.promise;
      if (!current()) return;

      const sourceName = archive
        ? selected[0].name
        : `${selected.length} g\u00f6rsel`;

      const dataset = await api<BrandDataset>(`/brands/${brandId}/datasets`, {
        method: "POST",
        signal: controller.signal,
        body: JSON.stringify({
          name: sourceName,
          job_id: job.job_id,
          annotation_project_id: null,
        }),
      });
      if (!current()) return;

      setBrand((current) =>
        current
          ? { ...current, datasets: [dataset, ...current.datasets] }
          : current,
      );

      try {
        window.localStorage.setItem(
          `frame-intelligence:job-brand:${job.job_id}`,
          brandId,
        );
      } catch {
        // Navigation context only.
      }

      setVideoMessage(
        archive
          ? "ZIP dosyas\u0131 markaya eklendi. G\u00f6rseller i\u015fleniyor."
          : `${selected.length} g\u00f6rsel markaya eklendi. \u0130\u015fleme ba\u015flad\u0131.`,
      );
    } catch (cause) {
      if (current() && !(cause instanceof DOMException && cause.name === "AbortError")) setError(
        cause instanceof Error
          ? cause.message
          : "G\u00f6rsel / ZIP eklenemedi.",
      );
    } finally {
      mutationControllers.current.delete(controller);
      if (current()) {
        abortImages.current = null;
        imageLocked.current = false;
        setImageBusy(false);
        if (imageInput.current) imageInput.current.value = "";
      }
    }
  }

  if (!brand || brand.id !== brandId) {
    return (
      <main className={styles.shell}>
        <Link className={styles.back} href="/brands">← Markalara dön</Link>
        {error ? <div className={styles.error}>{error}</div> : <p>Marka yükleniyor…</p>}
      </main>
    );
  }

  return (
    <main className={styles.shell}>
      <Link className={styles.back} href="/brands">← Markalara dön</Link>

      <div className={styles.brandHeader}>
        <div>
          <p className={styles.eyebrow}>Marka çalışma alanı</p>
          <h1 className={styles.title}>{brand.name}</h1>
          <p className={styles.copy}>
            Sınıfları ve farklı kaynaklardan gelen veri setlerini tek marka altında büyütün.
          </p>
        </div>
        <span className={styles.status}>● {brand.status === "READY" ? "Ready" : "Draft"}</span>
      </div>

      {error && <div className={styles.error}>{error}</div>}
      {videoMessage && (
        <div style={{
          marginBottom: 18,
          padding: "12px 14px",
          borderRadius: 12,
          background: "#ecfdf5",
          color: "#166534",
          fontWeight: 700,
        }}>
          {videoMessage}
        </div>
      )}

      <div className={styles.grid}>
        <section className={styles.card}>
          <h2>Sınıflar</h2>
          <form className={styles.inlineForm} onSubmit={addClass}>
            <input
              className={styles.input}
              value={className}
              onChange={(event) => setClassName(event.target.value)}
              placeholder="Örn. wordmark, symbol, swoosh"
              maxLength={80}
            />
            <button className={styles.button} type="submit" disabled={busy || !className.trim()}>
              + Sınıf ekle
            </button>
          </form>
          <div className={styles.chips}>
            {brand.classes.length === 0 ? (
              <span className={styles.muted}>Henüz sınıf eklenmedi.</span>
            ) : (
              brand.classes.map((item) => (
                <span className={styles.chip} key={item.id}>
                  <span className={styles.dot} style={{ background: item.color }} />
                  {item.name}
                </span>
              ))
            )}
          </div>
        </section>

        <section className={styles.card}>
          <h2>Veri ekle</h2>
          <p className={styles.muted}>
            Yeni kaynaklar bu markanın altında ayrı veri setleri olarak tutulur.
          </p>
          <div className={styles.actions}>
            <button
              className={styles.button}
              type="button"
              onClick={() => {
                setError(null);
                setModalOpen(true);
              }}
            >
              ▣ Videodan Kare Ekle
            </button>
            <button
              className={styles.secondary}
              type="button"
              disabled={imageBusy}
              onClick={() => singleImageInput.current?.click()}
            >
              {"Kare Se\u00e7"}
            </button>

            <input
              ref={singleImageInput}
              type="file"
              accept="image/*"
              hidden
              disabled={imageBusy}
              onChange={(event) => void addImages(event.target.files)}
            />
            <button
              className={styles.secondary}
              type="button"
              disabled={imageBusy}
              onClick={() => imageInput.current?.click()}
            >
              {imageBusy ? "Ekleniyor..." : "G\u00f6rsel / ZIP Ekle"}
            </button>

            <input
              ref={imageInput}
              type="file"
              accept="image/*,.zip,application/zip"
              multiple
              hidden
              disabled={imageBusy}
              onChange={(event) => void addImages(event.target.files)}
            />
          </div>
        </section>

        <section className={`${styles.card} ${styles.cardWide}`}>
          <h2>Veri setleri</h2>
          {brand.datasets.length === 0 ? (
            <p className={styles.muted}>Henüz veri seti bağlı değil.</p>
          ) : (
            brand.datasets.map((dataset) => (
              <div className={styles.dataset} key={dataset.id}>
                <div>
                  <strong>{dataset.name}</strong>
                  <div className={styles.muted}>
                    {dataset.annotation_project_id
                      ? "Etiket projesi bağlı"
                      : "Video işleniyor / kareler hazırlanıyor"}
                  </div>
                </div>
                {dataset.job_id && (
                  <Link className={styles.secondary} href={`/jobs/${dataset.job_id}`}>
                    İş durumunu aç →
                  </Link>
                )}
              </div>
            ))
          )}
        </section>
      </div>

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
            <div style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 20,
              padding: "18px 22px",
              borderBottom: "1px solid #e5e7eb",
            }}>
              <h2 id="video-frame-modal-title" style={{ margin: 0, fontSize: 22 }}>
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
                <div style={{ marginBottom: 10, fontWeight: 800 }}>Video Kaynağı *</div>
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
                  <button
                    type="button"
                    disabled={videoBusy}
                    onClick={() => setSourceMode("upload")}
                    style={{
                      minHeight: 42,
                      padding: "0 16px",
                      borderRadius: 10,
                      border: sourceMode === "upload" ? "2px solid #6d28d9" : "1px solid #dbe2ea",
                      background: sourceMode === "upload" ? "#f5f3ff" : "white",
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
                      border: sourceMode === "url" ? "2px solid #6d28d9" : "1px solid #dbe2ea",
                      background: sourceMode === "url" ? "#f5f3ff" : "white",
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
                  <label htmlFor="brand-video-file" style={{ display: "block", marginBottom: 8, fontWeight: 800 }}>
                    Video Seç *
                  </label>
                  <input
                    id="brand-video-file"
                    className={styles.input}
                    type="file"
                    accept={VIDEO_ACCEPT}
                    disabled={videoBusy}
                    onChange={(event) => setVideoFile(event.target.files?.[0] ?? null)}
                  />
                </div>
              ) : (
                <div style={{ marginBottom: 20 }}>
                  <label htmlFor="brand-video-url" style={{ display: "block", marginBottom: 8, fontWeight: 800 }}>
                    Video URL&apos;si *
                  </label>
                  <input
                    id="brand-video-url"
                    className={styles.input}
                    type="url"
                    value={videoUrl}
                    disabled={videoBusy}
                    placeholder="https://example.com/video.mp4"
                    onChange={(event) => setVideoUrl(event.target.value)}
                  />
                </div>
              )}

              <div style={{ marginBottom: 20 }}>
                <label htmlFor="frames-per-minute" style={{ display: "block", marginBottom: 8, fontWeight: 800 }}>
                  Dakikada kare sayısı *
                </label>
                <input
                  id="frames-per-minute"
                  className={styles.input}
                  type="number"
                  min="1"
                  max="3600"
                  step="1"
                  value={framesPerMinute}
                  disabled={videoBusy}
                  onChange={(event) => setFramesPerMinute(event.target.value)}
                />
                <div className={styles.muted} style={{ marginTop: 7 }}>
                  Önerilen: çoğu kullanım için 1–20 kare/dakika.
                </div>
              </div>

              <label style={{
                display: "flex",
                alignItems: "center",
                gap: 10,
                color: "#64748b",
                marginBottom: 8,
              }}>
                <input type="checkbox" disabled />
                Zaman aralığı belirle
              </label>
              <div className={styles.muted} style={{ fontSize: 13 }}>
                Zaman aralığı seçimi sonraki adımda bağlanacak; şu an tüm video işlenir.
              </div>

              {videoBusy && (
                <div style={{ marginTop: 18 }}>
                  <progress
                    max="100"
                    value={videoProgress ?? undefined}
                    style={{ width: "100%" }}
                  />
                  <div className={styles.muted}>
                    {sourceMode === "url"
                      ? "Video işi oluşturuluyor…"
                      : videoProgress === null
                        ? "Video yükleniyor…"
                        : `Video yükleniyor: %${videoProgress}`}
                  </div>
                </div>
              )}
            </div>

            <div style={{
              display: "flex",
              justifyContent: "flex-end",
              gap: 10,
              padding: "16px 22px",
              borderTop: "1px solid #e5e7eb",
              background: "#f8fafc",
            }}>
              <button
                type="button"
                className={styles.secondary}
                disabled={videoBusy}
                onClick={closeModal}
              >
                İptal
              </button>
              <button
                type="button"
                className={styles.button}
                disabled={
                  videoBusy ||
                  !framesPerMinute ||
                  (sourceMode === "upload" ? !videoFile : !videoUrl.trim())
                }
                onClick={() => void addVideoFrames()}
              >
                {videoBusy ? "Ekleniyor…" : "＋ Kare Ekle"}
              </button>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
