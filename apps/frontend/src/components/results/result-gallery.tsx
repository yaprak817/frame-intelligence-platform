"use client";
/* eslint-disable @next/next/no-img-element */
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { getJobResult, getJobStatus, getManifestDownload } from "@/lib/api/client";
import { FrameAccessLoader } from "@/lib/api/frame-access";
import { userErrorMessage } from "@/lib/api/errors";
import type { FrameAccessResponse, JobStatusResponse, PublicResultManifest, ResultFrame } from "@/lib/api/types";

const PAGE_SIZE = 12;
const isAbort = (value: unknown) => value instanceof DOMException && value.name === "AbortError";
const isFresh = (access: FrameAccessResponse) => Date.parse(access.expires_at) > Date.now();
const frameAlt = (frame: ResultFrame) => `Videonun ${new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 2 }).format(frame.timestamp_ms / 1000)} saniyesindeki seçilmiş kare`;
const formatDuration = (seconds: number) => `${new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 1 }).format(seconds)} sn`;

function FrameCard({ frame, loader, onOpen }: { frame: ResultFrame; loader: FrameAccessLoader; onOpen: (frame: ResultFrame, trigger: HTMLButtonElement) => void }) {
  const host = useRef<HTMLElement>(null);
  const mounted = useRef(false);
  const generation = useRef(0);
  const refreshed = useRef(false);
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    mounted.current = true;
    const token = ++generation.current;
    refreshed.current = false;
    const current = () => mounted.current && generation.current === token;
    const load = () => loader.load(frame).then((access) => { if (current()) { setError(null); setUrl(access.url); } }).catch((caught) => { if (current() && !isAbort(caught)) setError("Kare yüklenemedi."); });
    const observer = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) { observer?.disconnect(); void load(); }
    }, { rootMargin: "240px" }) : null;
    if (observer && host.current) observer.observe(host.current); else void load();
    return () => { mounted.current = false; generation.current += 1; observer?.disconnect(); };
  }, [frame, loader]);
  const retry = () => {
    const token = generation.current;
    if (refreshed.current) { if (mounted.current) { setUrl(null); setError("Kare yüklenemedi."); } return; }
    refreshed.current = true;
    void loader.load(frame, true).then((access) => { if (mounted.current && generation.current === token) { setError(null); setUrl(access.url); } }).catch((caught) => {
      if (mounted.current && generation.current === token && !isAbort(caught)) { setUrl(null); setError("Kare yüklenemedi."); }
    });
  };
  return <article className="frame-card" ref={host}>{url ? <button type="button" className="frame-button" onClick={(event) => onOpen(frame, event.currentTarget)} aria-label={`${frameAlt(frame)} büyüt`}><img src={url} alt={frameAlt(frame)} loading="lazy" onError={retry} width={frame.width} height={frame.height} /></button> : <div className="frame-placeholder" aria-busy={!error}>{error ?? "Kare yükleniyor…"}</div>}<dl className="frame-meta"><div><dt>Zaman</dt><dd>{(frame.timestamp_ms / 1000).toFixed(2)} sn</dd></div><div><dt>Boyut</dt><dd>{frame.width} × {frame.height}</dd></div></dl></article>;
}

function FrameModal({ frame, loader, onClose, returnFocus }: { frame: ResultFrame; loader: FrameAccessLoader; onClose: () => void; returnFocus: HTMLButtonElement }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const mounted = useRef(false);
  const generation = useRef(0);
  const refreshed = useRef(false);
  const [access, setAccess] = useState<FrameAccessResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    mounted.current = true;
    const token = ++generation.current;
    refreshed.current = false;
    const node = dialog.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (node && !node.open) node.showModal();
    node?.querySelector<HTMLElement>("button")?.focus();
    void loader.load(frame).then((next) => isFresh(next) ? next : loader.load(frame, true)).then((next) => { if (mounted.current && generation.current === token) setAccess(next); }).catch((caught) => { if (mounted.current && generation.current === token && !isAbort(caught)) setError("Büyütülmüş kare yüklenemedi."); });
    return () => { mounted.current = false; generation.current += 1; if (node?.open) node.close(); document.body.style.overflow = previousOverflow; returnFocus.focus(); };
  }, [frame, loader, returnFocus]);
  const retry = () => {
    const token = generation.current;
    if (refreshed.current) { if (mounted.current) { setAccess(null); setError("Büyütülmüş kare yüklenemedi."); } return; }
    refreshed.current = true;
    void loader.load(frame, true).then((next) => { if (mounted.current && generation.current === token) { setError(null); setAccess(next); } }).catch((caught) => { if (mounted.current && generation.current === token && !isAbort(caught)) { setAccess(null); setError("Büyütülmüş kare yüklenemedi."); } });
  };
  const trapFocus = (event: React.KeyboardEvent<HTMLDialogElement>) => {
    if (event.key === "Escape") { event.preventDefault(); onClose(); return; }
    if (event.key !== "Tab" || !dialog.current) return;
    const items = [...dialog.current.querySelectorAll<HTMLElement>('button:not(:disabled),[href],[tabindex]:not([tabindex="-1"])')];
    if (!items.length) { event.preventDefault(); dialog.current.focus(); return; }
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  };
  return <dialog className="frame-modal" aria-labelledby="frame-modal-title" ref={dialog} onKeyDown={trapFocus} onCancel={(event) => { event.preventDefault(); onClose(); }} onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><div className="modal-heading"><h2 id="frame-modal-title">Seçilmiş kare</h2><button className="button secondary" type="button" onClick={onClose}>Kapat</button></div><div aria-live="polite" aria-busy={!access && !error}>{!access && !error && "Büyütülmüş kare yükleniyor…"}</div>{error && <p className="field-error" role="alert">{error}</p>}{access && <img src={access.url} alt={frameAlt(frame)} onError={retry} />}<p>{(frame.timestamp_ms / 1000).toFixed(2)} saniye · {frame.width} × {frame.height}</p></dialog>;
}

type RequestState = { phase: "loading" } | { phase: "ready"; job: JobStatusResponse; result: { manifest: PublicResultManifest; loader: FrameAccessLoader } | null } | { phase: "error"; message: string };
function ResultGalleryJob({ jobId }: { jobId: string }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const mounted = useRef(false);
  const canonicalJobId = useRef<string | null>(null);
  const downloadController = useRef<AbortController | null>(null);
  const [request, setRequest] = useState<RequestState>({ phase: "loading" });
  const [visible, setVisible] = useState(PAGE_SIZE);
  const [modal, setModal] = useState<{ frame: ResultFrame; trigger: HTMLButtonElement } | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    heading.current?.focus();
    void getJobStatus(jobId, controller.signal).then(async (job) => {
      if (!mounted.current) return;
      canonicalJobId.current = job.id;
      if (job.status !== "SUCCEEDED") { setRequest({ phase: "ready", job, result: null }); return; }
      const manifest = await getJobResult(job.id, controller.signal);
      if (mounted.current) setRequest({ phase: "ready", job, result: { manifest, loader: new FrameAccessLoader(job.id, controller.signal) } });
    }).catch((caught) => { if (mounted.current && !isAbort(caught)) setRequest({ phase: "error", message: userErrorMessage(caught) }); });
    return () => { mounted.current = false; controller.abort(); downloadController.current?.abort(); canonicalJobId.current = null; };
  }, [jobId]);
  const closeModal = useCallback(() => setModal(null), []);
  const download = async () => {
    const currentId = canonicalJobId.current;
    if (!currentId) return;
    downloadController.current?.abort();
    const controller = new AbortController();
    downloadController.current = controller;
    setDownloadError(null); setDownloading(true);
    let objectUrl: string | null = null;
    try {
      const blob = await getManifestDownload(currentId, controller.signal);
      if (!mounted.current || controller.signal.aborted || canonicalJobId.current !== currentId) return;
      objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a"); anchor.href = objectUrl; anchor.download = `job-${currentId}-manifest.json`; anchor.click();
    } catch (caught) { if (mounted.current && canonicalJobId.current === currentId && !isAbort(caught)) setDownloadError(userErrorMessage(caught)); }
    finally { if (objectUrl) URL.revokeObjectURL(objectUrl); if (mounted.current && downloadController.current === controller) setDownloading(false); if (downloadController.current === controller) downloadController.current = null; }
  };
  const job = request.phase === "ready" ? request.job : null;
  const result = request.phase === "ready" ? request.result : null;
  const pending = job && ["PENDING_DISPATCH", "QUEUED", "RUNNING"].includes(job.status);
  const manifest = result?.manifest ?? null;
  const loader = result?.loader ?? null;
  return <main className="page-shell result-shell"><Link className="back-link" href={`/jobs/${encodeURIComponent(jobId)}`}>← İş durumuna dön</Link><header className="result-heading"><p className="eyebrow">Analiz sonucu</p><h1 ref={heading} tabIndex={-1}>Seçilen kareler</h1><p>İş kimliği: <code>{jobId}</code></p></header>{request.phase === "loading" && <div className="status-message" role="status" aria-live="polite" aria-busy="true"><span className="spinner" aria-hidden="true" />Sonuçlar alınıyor…</div>}{request.phase === "error" && <div className="alert error" role="alert">{request.message}</div>}{pending && <div className="result-state" role="status"><h2>Sonuç henüz hazır değil</h2><p>Video işlenmeye devam ediyor. İş durumu ekranından ilerlemeyi takip edebilirsiniz.</p></div>}{job?.status === "FAILED" && <div className="alert error" role="alert"><strong>Bu iş için sonuç galerisi oluşturulamadı.</strong><p>Güvenle yeni bir iş oluşturup tekrar deneyebilirsiniz.</p></div>}{manifest && <><section className="summary-card" aria-labelledby="summary-title"><div><p className="eyebrow">Özet</p><h2 id="summary-title">Analiz özeti</h2></div><dl className="summary-grid"><div><dt>Kaydedilen kare</dt><dd>{manifest.summary.frames_saved}</dd></div><div><dt>Aday kare</dt><dd>{manifest.summary.candidates}</dd></div><div><dt>Tekrar kaldırıldı</dt><dd>{manifest.summary.duplicates_removed}</dd></div><div><dt>İşleme süresi</dt><dd>{formatDuration(manifest.summary.processing_seconds)}</dd></div><div><dt>Video süresi</dt><dd>{formatDuration(manifest.summary.duration_seconds)}</dd></div></dl><button className="button secondary" type="button" disabled={downloading} onClick={() => void download()}>{downloading ? "Manifest indiriliyor…" : "Public manifesti indir"}</button>{downloadError && <p className="field-error" role="alert">{downloadError}</p>}</section><section aria-labelledby="gallery-title"><div className="gallery-heading"><h2 id="gallery-title">Frame galerisi</h2><span>{manifest.frames.length} kare</span></div>{manifest.frames.length === 0 ? <div className="result-state"><h3>Gösterilecek kare yok</h3><p>Analiz tamamlandı ancak seçilmiş bir kare bulunmuyor.</p></div> : loader && <><div className="frame-grid">{manifest.frames.slice(0, visible).map((frame) => <FrameCard key={frame.index} frame={frame} loader={loader} onOpen={(selected, trigger) => setModal({ frame: selected, trigger })} />)}</div>{visible < manifest.frames.length && <button className="button secondary load-more" type="button" onClick={() => setVisible((count) => Math.min(count + PAGE_SIZE, manifest.frames.length))}>Daha fazla kare yükle</button>}</>}</section></>}{modal && loader && <FrameModal frame={modal.frame} loader={loader} returnFocus={modal.trigger} onClose={closeModal} />}</main>;
}
export function ResultGallery({ jobId }: { jobId: string }) { return <ResultGalleryJob key={jobId} jobId={jobId} />; }
