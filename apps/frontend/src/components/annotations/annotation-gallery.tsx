"use client";
/* eslint-disable @next/next/no-img-element */

import { useEffect, useMemo, useState } from "react";
import { getOrCreateAnnotationProject } from "@/lib/api/client";
import { userErrorMessage } from "@/lib/api/errors";
import type { AnnotationProject } from "@/lib/api/types";

type Filter = "all" | "unlabelled" | "manual";
const PAGE_SIZE = 12;

export function AnnotationGallery({ jobId }: { jobId: string }) {
  const [project, setProject] = useState<AnnotationProject | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [descending, setDescending] = useState(false);
  const [page, setPage] = useState(1);
  useEffect(() => {
    const controller = new AbortController();
    const startup = window.setTimeout(() => void getOrCreateAnnotationProject(jobId, controller.signal)
      .then(setProject)
      .catch((caught) => { if (!controller.signal.aborted) setError(userErrorMessage(caught)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); }), 0);
    return () => { window.clearTimeout(startup); controller.abort(); };
  }, [jobId]);

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
    <div className="annotation-gallery-controls">
      <label>Durum<select value={filter} onChange={(event) => { setFilter(event.target.value as Filter); setPage(1); }}><option value="all">Tümü</option><option value="unlabelled">Etiketlenmedi</option><option value="manual">Manuel etiketlendi</option></select></label>
      <label>Sıralama<select value={descending ? "desc" : "asc"} onChange={(event) => { setDescending(event.target.value === "desc"); setPage(1); }}><option value="asc">Index artan</option><option value="desc">Index azalan</option></select></label>
    </div>
    {visible.length === 0 ? <div className="status-message">Bu filtrede görsel bulunamadı.</div> : <div className="annotation-card-grid">{visible.map((item) => <a className="annotation-card" key={item.index} href={`/jobs/${encodeURIComponent(jobId)}/annotations/${item.index}`}>
      <img src={item.preview_url} alt={item.filename} loading="lazy" />
      <div><strong>{item.filename}</strong><span>Index {item.index}</span><span>{item.box_count} kutu</span><span className={`annotation-status ${item.completed ? "manual" : "unlabelled"}`}>{item.completed ? "MANUEL ETİKETLENDİ" : "ETİKETLENMEDİ"}</span></div>
    </a>)}</div>}
    <nav className="annotation-pagination" aria-label="Galeri sayfaları"><button className="button secondary" disabled={safePage === 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>Önceki sayfa</button><span>{safePage} / {pages}</span><button className="button secondary" disabled={safePage === pages} onClick={() => setPage((value) => Math.min(pages, value + 1))}>Sonraki sayfa</button></nav>
  </main>;
}
