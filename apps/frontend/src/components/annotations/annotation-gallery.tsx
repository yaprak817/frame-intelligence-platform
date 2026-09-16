"use client";
/* eslint-disable @next/next/no-img-element */

import { useEffect, useMemo, useRef, useState } from "react";
import { createAndStartAnnotationTraining, getAnnotationTraining, getOrCreateAnnotationProject, listAnnotationTrainings } from "@/lib/api/client";
import { ApiError, userErrorMessage } from "@/lib/api/errors";
import type { AnnotationProject, AnnotationTraining } from "@/lib/api/types";

type Filter = "all" | "unlabelled" | "manual";
const PAGE_SIZE = 12;
export const POLL_DELAY_MS = 1500;
export const POLL_MAX_DELAY_MS = 8000;
export const POLL_MAX_RETRIES = 4;

export const trainingPollRetryDelay = (error: unknown, failures: number) => {
  const retryable = error instanceof TypeError || (error instanceof ApiError && [429, 502, 503, 504].includes(error.status));
  if (!retryable) return null;
  const exponential = Math.min(POLL_MAX_DELAY_MS, POLL_DELAY_MS * (2 ** failures));
  const retryAfter = error instanceof ApiError && error.retryAfterSeconds
    ? Math.min(POLL_MAX_DELAY_MS, Math.max(POLL_DELAY_MS, error.retryAfterSeconds * 1000))
    : exponential;
  return Math.min(POLL_MAX_DELAY_MS, retryAfter);
};

export function AnnotationGallery({ jobId }: { jobId: string }) {
  return <AnnotationGalleryContent key={jobId} jobId={jobId} />;
}

function AnnotationGalleryContent({ jobId }: { jobId: string }) {
  const [project, setProject] = useState<AnnotationProject | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [descending, setDescending] = useState(false);
  const [page, setPage] = useState(1);
  const [training, setTraining] = useState<AnnotationTraining | null>(null);
  const [trainingBusy, setTrainingBusy] = useState(false);
  const [pollRetry, setPollRetry] = useState(0);
  const [pollExhausted, setPollExhausted] = useState(false);
  const mutationRef = useRef<Promise<void> | null>(null);
  const mutationAbortRef = useRef<AbortController | null>(null);
  const mutationGenerationRef = useRef(0);
  const listGenerationRef = useRef(0);
  const pollGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const jobRef = useRef(jobId);
  const trainingRef = useRef(training);
  useEffect(() => { jobRef.current = jobId; }, [jobId]);
  useEffect(() => { trainingRef.current = training; }, [training]);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      mutationGenerationRef.current += 1;
      mutationAbortRef.current?.abort();
      mutationAbortRef.current = null;
      mutationRef.current = null;
    };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const current = () => active && mountedRef.current && !controller.signal.aborted && jobRef.current === jobId;
    const startup = window.setTimeout(() => void getOrCreateAnnotationProject(jobId, controller.signal)
      .then((item) => { if (current()) setProject(item); })
      .catch((caught) => { if (current()) setError(userErrorMessage(caught)); })
      .finally(() => { if (current()) setLoading(false); }), 0);
    return () => { active = false; window.clearTimeout(startup); controller.abort(); };
  }, [jobId]);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    const generation = ++listGenerationRef.current;
    void listAnnotationTrainings(jobId, controller.signal).then((items) => {
      if (active && mountedRef.current && !controller.signal.aborted && jobRef.current === jobId && listGenerationRef.current === generation) setTraining(items.at(-1) ?? null);
    }).catch(() => undefined);
    return () => { active = false; listGenerationRef.current += 1; controller.abort(); };
  }, [jobId]);
  const trainingStatus = training?.status;
  const trainingStatusUrl = training?.status_url ?? "";
  useEffect(() => {
    if (!trainingStatus || !["PENDING", "RUNNING"].includes(trainingStatus)) return;
    const controller = new AbortController();
    let timer: number | undefined;
    let active = true;
    let failures = 0;
    const generation = ++pollGenerationRef.current;
    const current = () => active && mountedRef.current && !controller.signal.aborted && jobRef.current === jobId && pollGenerationRef.current === generation && trainingRef.current?.id === training?.id && ["PENDING", "RUNNING"].includes(trainingRef.current?.status ?? "");
    const poll = async () => {
      try {
        const next = await getAnnotationTraining(jobId, trainingStatusUrl, controller.signal);
        if (!current()) return;
        failures = 0;
        setPollExhausted(false);
        setTraining(next);
        if (["PENDING", "RUNNING"].includes(next.status)) timer = window.setTimeout(() => void poll(), POLL_DELAY_MS);
      } catch (caught) {
        if (!current()) return;
        const delay = trainingPollRetryDelay(caught, failures);
        if (delay !== null && failures < POLL_MAX_RETRIES) {
          failures += 1;
          timer = window.setTimeout(() => void poll(), delay);
        } else {
          setError(userErrorMessage(caught));
          setPollExhausted(true);
        }
      }
    };
    timer = window.setTimeout(() => void poll(), POLL_DELAY_MS);
    return () => { active = false; pollGenerationRef.current += 1; controller.abort(); if (timer !== undefined) window.clearTimeout(timer); };
  }, [jobId, training?.id, trainingStatus, trainingStatusUrl, pollRetry]);

  const startTraining = () => {
    if (!project || project.job_id !== jobId || completed < 50 || mutationRef.current) return;
    const controller = new AbortController();
    const generation = ++mutationGenerationRef.current;
    mutationAbortRef.current = controller;
    const current = () => mountedRef.current && !controller.signal.aborted && jobRef.current === jobId && mutationGenerationRef.current === generation;
    setTrainingBusy(true); setError(null);
    const request = createAndStartAnnotationTraining(jobId, project.revision, `training-${crypto.randomUUID()}`, controller.signal)
      .then((item) => { if (current()) setTraining(item); })
      .catch((caught) => { if (current()) setError(userErrorMessage(caught)); })
      .finally(() => {
        if (mutationRef.current === request) mutationRef.current = null;
        if (mutationAbortRef.current === controller) mutationAbortRef.current = null;
        if (current()) setTrainingBusy(false);
      });
    mutationRef.current = request;
  };

  const items = useMemo(() => {
    const filtered = project?.images.filter((item) => filter === "all" || (filter === "manual" ? item.completed : !item.completed)) ?? [];
    return [...filtered].sort((a, b) => descending ? b.index - a.index : a.index - b.index);
  }, [descending, filter, project]);
  const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  const safePage = Math.min(page, pages);
  const visible = items.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);
  const completed = project?.images.filter((item) => item.completed).length ?? 0;

  if (loading) return <main className="page-shell"><div className="status-message" role="status">Etiketleme galerisi yükleniyor…</div></main>;
  if (!project) return <main className="page-shell"><div className="alert error" role="alert">{error ?? "Etiketleme galerisi yüklenemedi."}</div></main>;
  return <main className="page-shell annotation-gallery-shell">
    <header className="gallery-heading"><div><a className="back-link" href={`/jobs/${encodeURIComponent(jobId)}/result`}>← Sonuca dön</a><p className="eyebrow">Manuel görsel etiketleme</p><h1>Frame ve görsel galerisi</h1><p>{completed} / {project.total_images} manuel etiketlendi</p></div></header>
    <section className="annotation-training-panel" aria-labelledby="training-heading">
      <h2 id="training-heading">Model eğitimi</h2>
      <p>{completed} / {project.total_images} manuel tamamlandı · En az 50 tamamlanmış görsel gerekir.</p>
      {!training && <button className="button primary" disabled={completed < 50 || trainingBusy} onClick={startTraining}>{trainingBusy ? "Hazırlanıyor…" : "Modeli eğit"}</button>}
      {training && <div role="status">
        <strong>{training.status === "SUCCEEDED" ? `Model v${training.model_version} hazır` : training.status}</strong>
        {training.progress_total > 0 && <p>{training.progress_completed} / {training.progress_total}</p>}
        {training.status === "FAILED" && <p>Eğitim güvenli biçimde tamamlanamadı. Lütfen yeni bir snapshot deneyin.</p>}
        {training.snapshot_download_url && <a href={training.snapshot_download_url}>Snapshot’ı indir</a>}
        {pollExhausted && ["PENDING", "RUNNING"].includes(training.status) && <button className="button secondary" onClick={() => { setError(null); setPollExhausted(false); setPollRetry((value) => value + 1); }}>Durumu yeniden dene</button>}
      </div>}
      <p>Otomatik etiketleme sonraki aşamada etkinleştirilecek.</p>
    </section>
    <div className="annotation-gallery-controls">
      <label>Durum<select value={filter} onChange={(event) => { setFilter(event.target.value as Filter); setPage(1); }}><option value="all">Tümü</option><option value="unlabelled">Etiketlenmedi</option><option value="manual">Manuel etiketlendi</option></select></label>
      <label>Sıralama<select value={descending ? "desc" : "asc"} onChange={(event) => { setDescending(event.target.value === "desc"); setPage(1); }}><option value="asc">Index artan</option><option value="desc">Index azalan</option></select></label>
    </div>
    {visible.length === 0 ? <div className="status-message">Bu filtrede görsel bulunamadı.</div> : <div className="annotation-card-grid">{visible.map((item) => <a className="annotation-card" key={item.index} href={`/jobs/${encodeURIComponent(jobId)}/annotations/${item.index}`}>
      <img src={item.preview_url} alt={item.filename} loading="lazy" />
      <div><strong>{item.filename}</strong><span>Index {item.index}</span>{item.timestamp_ms !== null && <span>{(item.timestamp_ms / 1000).toFixed(3)} sn</span>}<span>{item.width}×{item.height}</span><span>{item.box_count} kutu</span><span className={`annotation-status ${item.completed ? "manual" : "unlabelled"}`}>{item.completed ? "MANUEL ETİKETLENDİ" : "ETİKETLENMEDİ"}</span></div>
    </a>)}</div>}
    <nav className="annotation-pagination" aria-label="Galeri sayfaları"><button className="button secondary" disabled={safePage === 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Önceki sayfa</button><span>{safePage} / {pages}</span><button className="button secondary" disabled={safePage === pages} onClick={() => setPage((value) => Math.min(pages, value + 1))}>Sonraki sayfa</button></nav>
  </main>;
}
