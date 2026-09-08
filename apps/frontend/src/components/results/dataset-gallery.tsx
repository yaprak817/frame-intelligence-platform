"use client";
/* eslint-disable @next/next/no-img-element */

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { getDatasetResult } from "@/lib/api/client";
import { userErrorMessage } from "@/lib/api/errors";
import type { DatasetImage, DatasetQuality, PublicDatasetManifest } from "@/lib/api/types";

const labels: Record<DatasetQuality, string> = {
  normal: "Normal",
  challenging: "Zorlayıcı",
  unusable: "Kullanılamaz",
  rejected: "Reddedildi",
};

function DatasetCard({ image }: { image: DatasetImage }) {
  const url = image.access_url && image.access_url.startsWith("/") ? image.access_url : null;
  return <article className="frame-card dataset-card">
    <div className="frame-card-actions"><span className={`quality quality-${image.quality_category}`}>{labels[image.quality_category]}</span>{image.download_url && <a className="frame-download" href={image.download_url}>İndir</a>}</div>
    {url ? <img className="dataset-preview" src={url} alt={image.filename} loading="lazy" /> : <div className="frame-placeholder">{["unusable", "rejected"].includes(image.quality_category) ? "Önizleme kullanılamıyor" : "Görsel yükleniyor…"}</div>}
    <div className="dataset-meta"><strong>{image.filename}</strong><span>{image.width} × {image.height} · {(image.size_bytes / 1024).toFixed(1)} KB</span><span>Netlik {image.sharpness.toFixed(1)} · Parlaklık {image.brightness.toFixed(1)}</span>{image.duplicate && <span>Exact duplicate</span>}</div>
  </article>;
}

type DownloadNavigator = (url: string) => void;

const navigateToDownload: DownloadNavigator = (url) => window.location.assign(url);

export function DatasetGallery({ jobId, initialDownloadError = null, downloadNavigator = navigateToDownload }: { jobId: string; initialDownloadError?: string | null; downloadNavigator?: DownloadNavigator }) {
  const [manifest, setManifest] = useState<PublicDatasetManifest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<DatasetQuality | "all">("all");
  const [downloading, setDownloading] = useState<"accepted" | "yolo" | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(initialDownloadError);
  const downloadActive = useRef(false);
  const downloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void getDatasetResult(jobId, controller.signal).then(setManifest).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(userErrorMessage(caught));
    });
    return () => controller.abort();
  }, [jobId]);
  useEffect(() => {
    if (initialDownloadError) {
      const clean = new URL(window.location.href);
      clean.searchParams.delete("download_error");
      clean.searchParams.delete("retry_after");
      window.history.replaceState(null, "", `${clean.pathname}${clean.search}${clean.hash}`);
    }
  }, [initialDownloadError]);
  useEffect(() => () => {
    if (downloadTimer.current) clearTimeout(downloadTimer.current);
  }, []);
  const beginDownload = (event: React.MouseEvent<HTMLAnchorElement>, mode: "accepted" | "yolo") => {
    event.preventDefault();
    if (downloadActive.current) {
      return;
    }
    downloadActive.current = true;
    setDownloading(mode);
    setDownloadError(null);
    downloadNavigator(event.currentTarget.href);
    downloadTimer.current = setTimeout(() => {
      downloadActive.current = false;
      setDownloading(null);
      downloadTimer.current = null;
    }, 1_000);
  };
  if (error) return <main className="page-shell"><div className="alert error" role="alert">{error}</div></main>;
  if (!manifest) return <main className="page-shell"><div className="status-message" role="status">Sonuçlar alınıyor…</div></main>;
  const images = filter === "all" ? manifest.images : manifest.images.filter((item) => item.quality_category === filter);
  const summary = manifest.summary;
  return <main className="page-shell result-shell">
    <Link className="back-link" href={`/jobs/${encodeURIComponent(jobId)}`}>← İş durumuna dön</Link>
    <header className="result-heading"><p className="eyebrow">Görsel veri seti sonucu</p><h1>Veri seti analizi</h1><p>İş kimliği: <code>{jobId}</code></p></header>
    <section className="summary-card"><h2>Özet</h2><dl className="summary-grid dataset-summary"><div><dt>Yüklenen</dt><dd>{summary.uploaded_files}</dd></div><div><dt>Kabul edilen</dt><dd>{summary.accepted_files}</dd></div><div><dt>Normal</dt><dd>{summary.normal}</dd></div><div><dt>Zorlayıcı</dt><dd>{summary.challenging}</dd></div><div><dt>Kullanılamaz</dt><dd>{summary.unusable}</dd></div><div><dt>Reddedilen</dt><dd>{summary.rejected}</dd></div><div><dt>Duplicate</dt><dd>{summary.duplicates}</dd></div><div><dt>Önerilen</dt><dd>{summary.recommended_count}</dd></div><div><dt>Gerçek zorlayıcı oranı</dt><dd>%{(summary.actual_challenging_ratio * 100).toFixed(1)}</dd></div></dl>{summary.ratio_note && <p className="hint">Hedef oran sağlanamadı: {summary.ratio_note}</p>}<div className="gallery-actions"><a className="button secondary manifest-download" href={`/api/downloads/image-datasets/${encodeURIComponent(jobId)}/accepted`} download={`image-dataset-${jobId}-accepted.zip`} aria-disabled={downloading !== null} onClick={(event) => beginDownload(event, "accepted")}>{downloading === "accepted" ? "ZIP indiriliyor…" : "Kabul edilen görselleri ZIP indir"}</a><a className="button primary manifest-download" href={`/api/downloads/image-datasets/${encodeURIComponent(jobId)}/yolo`} download={`image-dataset-${jobId}-yolo.zip`} aria-disabled={downloading !== null} onClick={(event) => beginDownload(event, "yolo")}>{downloading === "yolo" ? "ZIP indiriliyor…" : "Önerilen YOLO-ready 640×640 ZIP indir"}</a></div>{downloadError && <p className="field-error" role="alert">{downloadError}</p>}</section>
    <section><div className="gallery-heading"><div><h2>Görsel galerisi</h2><span>{images.length} görsel</span></div><div className="gallery-actions" aria-label="Kalite filtresi">{(["all", "normal", "challenging", "unusable", "rejected"] as const).map((value) => <button key={value} type="button" className="button secondary" aria-pressed={filter === value} onClick={() => setFilter(value)}>{value === "all" ? "Tümü" : labels[value]}</button>)}</div></div><div className="frame-grid">{images.map((image) => <DatasetCard key={image.index} image={image} />)}</div></section>
  </main>;
}
