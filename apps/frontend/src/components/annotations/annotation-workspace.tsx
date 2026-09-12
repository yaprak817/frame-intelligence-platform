"use client";
/* eslint-disable @next/next/no-img-element, react-hooks/refs */

import { useCallback, useEffect, useRef, useState } from "react";
import { createAnnotationClass, deleteAnnotationClass, getImageAnnotations, getOrCreateAnnotationProject, putImageAnnotations, updateAnnotationClass } from "@/lib/api/client";
import { ApiError, userErrorMessage } from "@/lib/api/errors";
import type { AnnotationBox, AnnotationClass, AnnotationProject } from "@/lib/api/types";

type Rect = { id: string; class_id: string; left: number; top: number; width: number; height: number };
type Corner = "nw" | "ne" | "sw" | "se";
type Gesture = { kind: "draw" | "move" | "resize"; corner?: Corner; id: string; pointerId: number; startX: number; startY: number; original: Rect };
const MIN_BOX = 0.01;
const clamp = (value: number, min = 0, max = 1) => Math.min(max, Math.max(min, value));
const fromBox = (box: AnnotationBox): Rect => ({ id: box.id, class_id: box.class_id, left: box.x_center - box.width / 2, top: box.y_center - box.height / 2, width: box.width, height: box.height });
const toBox = (box: Rect): AnnotationBox => ({ id: box.id, class_id: box.class_id, x_center: Number((box.left + box.width / 2).toFixed(8)), y_center: Number((box.top + box.height / 2).toFixed(8)), width: Number(box.width.toFixed(8)), height: Number(box.height.toFixed(8)) });
const point = (event: React.PointerEvent<SVGSVGElement>) => { const bounds = event.currentTarget.getBoundingClientRect(); if (bounds.width <= 0 || bounds.height <= 0 || event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) return null; return { x: (event.clientX - bounds.left) / bounds.width, y: (event.clientY - bounds.top) / bounds.height }; };
const resizeRect = (original: Rect, corner: Corner, x: number, y: number): Rect => {
  const right = original.left + original.width, bottom = original.top + original.height;
  const west = corner === "nw" || corner === "sw", north = corner === "nw" || corner === "ne";
  const left = west ? clamp(x, 0, right - MIN_BOX) : original.left, top = north ? clamp(y, 0, bottom - MIN_BOX) : original.top;
  const nextRight = west ? right : clamp(x, original.left + MIN_BOX, 1), nextBottom = north ? bottom : clamp(y, original.top + MIN_BOX, 1);
  return { ...original, left, top, width: nextRight - left, height: nextBottom - top };
};
const colorFor = (classes: AnnotationClass[], id: string) => classes.find((item) => item.id === id)?.color ?? "#ffffff";
const editableTarget = (target: EventTarget | null) => target instanceof HTMLElement && (target.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
const aborted = (value: unknown) => value instanceof DOMException && value.name === "AbortError";

export function AnnotationWorkspace({ jobId, initialImageIndex }: { jobId: string; initialImageIndex?: number }) {
  const [project, setProject] = useState<AnnotationProject | null>(null);
  const [position, setPosition] = useState(0);
  const [boxes, setBoxes] = useState<Rect[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [activeClass, setActiveClass] = useState<string | null>(null);
  const [className, setClassName] = useState("");
  const [classColor, setClassColor] = useState("#1c6b50");
  const [loading, setLoading] = useState(true);
  const [imageLoading, setImageLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirtyState] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflictState] = useState(false);
  const [readyPreview, setReadyPreview] = useState<string | null>(null);
  const [previewGeneration, setPreviewGeneration] = useState(0);
  const gesture = useRef<Gesture | null>(null);
  const mounted = useRef(true);
  const busyRef = useRef(false);
  const dirtyRef = useRef(false);
  const conflictRef = useRef(false);
  const projectRef = useRef<AnnotationProject | null>(null);
  const positionRef = useRef(0);
  const projectController = useRef<AbortController | null>(null);
  const imageController = useRef<AbortController | null>(null);
  const mutationController = useRef<AbortController | null>(null);
  const imageRequest = useRef(0);
  const suppressPop = useRef(false);

  const setDirty = useCallback((value: boolean) => { dirtyRef.current = value; setDirtyState(value); }, []);
  const setConflict = useCallback((value: boolean) => { conflictRef.current = value; setConflictState(value); }, []);
  const commitProject = useCallback((value: AnnotationProject) => { projectRef.current = value; setProject(value); }, []);
  const commitPosition = useCallback((value: number) => { positionRef.current = value; setPosition(value); }, []);
  const validateBoxes = useCallback((value: AnnotationBox[], nextProject: AnnotationProject) => {
    if (value.some((box) => !nextProject.classes.some((item) => item.id === box.class_id))) throw new ApiError(502, "MANIFEST_INVALID");
  }, []);

  const loadImage = useCallback(async (nextProject: AnnotationProject, nextPosition: number) => {
    const summary = nextProject.images[nextPosition];
    if (!summary || busyRef.current) return false;
    imageController.current?.abort();
    const controller = new AbortController(); imageController.current = controller;
    const identity = ++imageRequest.current;
    if (mounted.current) { setImageLoading(true); setError(null); }
    try {
      const value = await getImageAnnotations(jobId, summary.index, controller.signal);
      validateBoxes(value.boxes, nextProject);
      if (!mounted.current || controller.signal.aborted || identity !== imageRequest.current) return false;
      const updated = { ...nextProject, revision: value.project_revision };
      setReadyPreview(null); setPreviewGeneration((current) => current + 1); commitProject(updated); commitPosition(nextPosition); setBoxes(value.boxes.map(fromBox));
      setSelected(null); gesture.current = null; setDirty(false); setConflict(false); return true;
    } catch (caught) {
      if (mounted.current && !controller.signal.aborted && identity === imageRequest.current && !aborted(caught)) setError(userErrorMessage(caught));
      return false;
    } finally { if (mounted.current && identity === imageRequest.current) setImageLoading(false); }
  }, [commitPosition, commitProject, jobId, setConflict, setDirty, validateBoxes]);

  const loadProject = useCallback(async (preserveImageIndex?: number) => {
    if (busyRef.current) return false;
    projectController.current?.abort(); imageController.current?.abort();
    const controller = new AbortController(); projectController.current = controller;
    const identity = ++imageRequest.current;
    if (mounted.current) { setLoading(projectRef.current === null); setImageLoading(projectRef.current !== null); setError(null); }
    try {
      const nextProject = await getOrCreateAnnotationProject(jobId, controller.signal);
      const requestedIndex = preserveImageIndex ?? initialImageIndex;
      const nextPosition = requestedIndex === undefined ? 0 : nextProject.images.findIndex((item) => item.index === requestedIndex);
      if (nextPosition < 0) throw new ApiError(404, "ANNOTATION_IMAGE_NOT_FOUND");
      const summary = nextProject.images[nextPosition];
      const detail = summary ? await getImageAnnotations(jobId, summary.index, controller.signal) : null;
      if (detail) validateBoxes(detail.boxes, nextProject);
      if (!mounted.current || controller.signal.aborted || identity !== imageRequest.current) return false;
      const committed = detail ? { ...nextProject, revision: detail.project_revision } : nextProject;
      setReadyPreview(null); setPreviewGeneration((current) => current + 1); commitProject(committed); commitPosition(nextPosition); setBoxes(detail?.boxes.map(fromBox) ?? []);
      setActiveClass((current) => current && committed.classes.some((item) => item.id === current) ? current : committed.classes[0]?.id ?? null);
      setSelected(null); gesture.current = null; setDirty(false); setConflict(false); return true;
    } catch (caught) {
      if (mounted.current && !controller.signal.aborted && identity === imageRequest.current && !aborted(caught)) setError(userErrorMessage(caught));
      return false;
    } finally { if (mounted.current && identity === imageRequest.current) { setLoading(false); setImageLoading(false); } }
  }, [commitPosition, commitProject, initialImageIndex, jobId, setConflict, setDirty, validateBoxes]);

  useEffect(() => {
    mounted.current = true; const startup = window.setTimeout(() => void loadProject(), 0);
    return () => { window.clearTimeout(startup); mounted.current = false; gesture.current = null; projectController.current?.abort(); imageController.current?.abort(); mutationController.current?.abort(); };
  }, [loadProject]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (!selected || editableTarget(event.target) || busyRef.current || imageLoading || conflictRef.current || !readyPreview?.endsWith(`:${previewGeneration}`) || !["Delete", "Backspace"].includes(event.key)) return;
      event.preventDefault(); setBoxes((current) => current.filter((item) => item.id !== selected)); setSelected(null); setDirty(true);
    };
    window.addEventListener("keydown", onKey); return () => window.removeEventListener("keydown", onKey);
  }, [imageLoading, previewGeneration, readyPreview, selected, setDirty]);
  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    const onPopState = () => {
      if (suppressPop.current) { suppressPop.current = false; return; }
      if (window.confirm("Kaydedilmemiş değişiklikler kaybolacak. Sayfadan ayrılmak istiyor musunuz?")) { dirtyRef.current = false; return; }
      suppressPop.current = true; window.history.forward();
    };
    window.addEventListener("beforeunload", onBeforeUnload); window.addEventListener("popstate", onPopState);
    return () => { window.removeEventListener("beforeunload", onBeforeUnload); window.removeEventListener("popstate", onPopState); };
  }, [dirty]);

  const reportMutationError = useCallback((caught: unknown) => {
    if (aborted(caught) || !mounted.current) return;
    const revisionConflict = caught instanceof ApiError && caught.status === 409 && caught.code === "ANNOTATION_REVISION_CONFLICT";
    if (revisionConflict) setConflict(true);
    setError(revisionConflict ? "Etiketler başka bir oturumda değiştirildi. Verileriniz kaydedilmedi." : userErrorMessage(caught));
  }, [setConflict]);
  const mutateClass = async (action: (signal: AbortSignal) => Promise<{ revision: number; annotation_class: AnnotationClass } | number>, apply: (value: { revision: number; annotation_class: AnnotationClass } | number, current: AnnotationProject) => AnnotationProject) => {
    const current = projectRef.current; if (!current || busyRef.current || conflictRef.current || imageLoading) return;
    const controller = new AbortController(); mutationController.current = controller; busyRef.current = true; setBusy(true); setError(null);
    try { const value = await action(controller.signal); if (mounted.current && !controller.signal.aborted) commitProject(apply(value, projectRef.current ?? current)); }
    catch (caught) { reportMutationError(caught); }
    finally { if (mutationController.current === controller) mutationController.current = null; busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const addClass = async (event: React.FormEvent) => {
    event.preventDefault(); const current = projectRef.current; const name = className.trim(); if (!current || !name || current.classes.length >= current.limits.max_classes) return;
    await mutateClass((signal) => createAnnotationClass(jobId, current.revision, name, classColor, signal), (value, latest) => {
      if (typeof value === "number") return latest; setActiveClass(value.annotation_class.id); setClassName(""); return { ...latest, revision: value.revision, classes: [...latest.classes, value.annotation_class] };
    });
  };
  const renameClass = (item: AnnotationClass) => {
    const name = window.prompt("Yeni sınıf adı", item.name)?.trim(); const current = projectRef.current; if (!name || !current) return;
    void mutateClass((signal) => updateAnnotationClass(jobId, item.id, current.revision, name, item.color, signal), (value, latest) => typeof value === "number" ? latest : { ...latest, revision: value.revision, classes: latest.classes.map((entry) => entry.id === item.id ? value.annotation_class : entry) });
  };
  const removeClass = (item: AnnotationClass) => {
    const current = projectRef.current; if (!current || boxes.some((box) => box.class_id === item.id) || !window.confirm(`“${item.name}” sınıfı silinsin mi?`)) return;
    void mutateClass((signal) => deleteAnnotationClass(jobId, item.id, current.revision, signal), (revision, latest) => {
      if (typeof revision !== "number") return latest; const classes = latest.classes.filter((entry) => entry.id !== item.id); setActiveClass((active) => active === item.id ? classes[0]?.id ?? null : active); return { ...latest, revision, classes };
    });
  };

  const endGesture = (event?: React.PointerEvent<SVGSVGElement>) => {
    const active = gesture.current; if (!active) return; gesture.current = null;
    if (event?.currentTarget.hasPointerCapture?.(active.pointerId)) event.currentTarget.releasePointerCapture(active.pointerId);
    setBoxes((current) => current.flatMap((box) => {
      if (box.id !== active.id) return [box];
      const width = clamp(box.width, 0, 1), height = clamp(box.height, 0, 1);
      if (active.kind === "draw" && (width < MIN_BOX || height < MIN_BOX)) return [];
      return [{ ...box, width: Math.max(MIN_BOX, width), height: Math.max(MIN_BOX, height), left: clamp(box.left, 0, 1 - Math.max(MIN_BOX, width)), top: clamp(box.top, 0, 1 - Math.max(MIN_BOX, height)) }];
    }));
  };
  const pointerDown = (event: React.PointerEvent<SVGSVGElement>) => {
    if (event.button !== 0 || imageLoading || busyRef.current || conflictRef.current || !readyPreview?.endsWith(`:${previewGeneration}`)) return;
    const target = event.target as SVGElement; const location = point(event); if (!location) return; const id = target.dataset.boxId; const corner = target.dataset.handle as Corner | undefined;
    if (id) { const original = boxes.find((item) => item.id === id); if (!original) return; setSelected(id); gesture.current = { kind: corner ? "resize" : "move", corner, id, pointerId: event.pointerId, startX: location.x, startY: location.y, original }; }
    else {
      if (!activeClass || !project || boxes.length >= project.limits.max_boxes_per_image) { setError(activeClass ? "Bu görsel için kutu sınırına ulaşıldı." : "Kutu çizmeden önce aktif bir sınıf seçin."); return; }
      const fresh: Rect = { id: crypto.randomUUID(), class_id: activeClass, left: location.x, top: location.y, width: 0, height: 0 }; setBoxes((current) => [...current, fresh]); setSelected(fresh.id); gesture.current = { kind: "draw", id: fresh.id, pointerId: event.pointerId, startX: location.x, startY: location.y, original: fresh };
    }
    event.currentTarget.setPointerCapture?.(event.pointerId); event.preventDefault();
  };
  const pointerMove = (event: React.PointerEvent<SVGSVGElement>) => {
    const active = gesture.current; if (!active || busyRef.current || conflictRef.current || !readyPreview?.endsWith(`:${previewGeneration}`)) return; const location = point(event); if (!location) return;
    setBoxes((current) => current.map((box) => {
      if (box.id !== active.id) return box;
      if (active.kind === "draw") { const left = Math.min(active.startX, location.x), top = Math.min(active.startY, location.y); return { ...box, left, top, width: Math.abs(location.x - active.startX), height: Math.abs(location.y - active.startY) }; }
      if (active.kind === "move") return { ...box, left: clamp(active.original.left + location.x - active.startX, 0, 1 - active.original.width), top: clamp(active.original.top + location.y - active.startY, 0, 1 - active.original.height) };
      return resizeRect(active.original, active.corner!, location.x, location.y);
    })); setDirty(true);
  };
  const navigate = (offset: number) => {
    const current = projectRef.current; if (!current || dirtyRef.current || busyRef.current || conflictRef.current) { if (dirtyRef.current) setError("Kaydedilmemiş değişiklikler var. Önce kaydedin veya değişiklikleri geri alın."); return; }
    const next = positionRef.current + offset; if (next >= 0 && next < current.images.length) void loadImage(current, next);
  };
  const save = async (advance: boolean) => {
    const current = projectRef.current, currentPosition = positionRef.current, image = current?.images[currentPosition];
    if (!current || !image || busyRef.current || conflictRef.current || imageLoading || !readyPreview?.endsWith(`:${previewGeneration}`) || !dirtyRef.current) return;
    const controller = new AbortController(); mutationController.current = controller; busyRef.current = true; setBusy(true); setError(null);
    try {
      const value = await putImageAnnotations(jobId, image.index, current.revision, boxes.length > 0, boxes.map(toBox), controller.signal); validateBoxes(value.boxes, current);
      if (!mounted.current || controller.signal.aborted) return;
      const images = current.images.map((item, index) => index === currentPosition ? { ...item, completed: value.completed, box_count: value.boxes.length } : item);
      const updated = { ...current, revision: value.project_revision, images }; commitProject(updated); setBoxes(value.boxes.map(fromBox)); setDirty(false); setConflict(false);
      if (advance && currentPosition < images.length - 1) { busyRef.current = false; await loadImage(updated, currentPosition + 1); busyRef.current = true; }
    } catch (caught) { reportMutationError(caught); }
    finally { if (mutationController.current === controller) mutationController.current = null; busyRef.current = false; if (mounted.current) setBusy(false); }
  };

  if (loading) return <main className="page-shell annotation-shell"><div className="status-message" role="status">Etiketleme projesi hazırlanıyor…</div></main>;
  if (!project) return <main className="page-shell annotation-shell"><div className="alert error" role="alert">{error ?? "Etiketleme projesi yüklenemedi."}</div><button className="button secondary" onClick={() => void loadProject()}>Yeniden dene</button></main>;
  const resultHref = `/jobs/${encodeURIComponent(jobId)}/result`;
  const galleryHref = `/jobs/${encodeURIComponent(jobId)}/annotations`;
  const guardExit = (event: React.MouseEvent<HTMLAnchorElement>) => { if (dirtyRef.current && !window.confirm("Kaydedilmemiş değişiklikler kaybolacak. Sayfadan ayrılmak istiyor musunuz?")) event.preventDefault(); };
  if (project.images.length === 0) return <main className="page-shell annotation-shell"><a className="back-link" href={resultHref}>← Sonuca dön</a><h1>Etiketlenecek görsel yok</h1><p>Bu veri setinde güvenli YOLO önizlemesi bulunan görsel yok.</p></main>;
  const image = project.images[position], previewKey = `${image.index}:${image.preview_url}:${previewGeneration}`, imageReady = readyPreview === previewKey;
  const completed = project.images.filter((item) => item.completed || item.box_count > 0).length, locked = busy || imageLoading || conflict || !imageReady;
  const handles: { corner: Corner; label: string }[] = [{ corner: "se", label: "Sağ alt boyutlandırma tutamacı" }, { corner: "nw", label: "Sol üst boyutlandırma tutamacı" }, { corner: "ne", label: "Sağ üst boyutlandırma tutamacı" }, { corner: "sw", label: "Sol alt boyutlandırma tutamacı" }];
  const keyboardResize = (event: React.KeyboardEvent<SVGRectElement>, box: Rect, corner: Corner) => {
    if (!imageReady || busy || conflict || imageLoading || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) return;
    event.preventDefault(); const step = event.shiftKey ? 10 / 640 : 1 / 640;
    const x = corner.endsWith("e") ? box.left + box.width : box.left, y = corner.startsWith("s") ? box.top + box.height : box.top;
    const nextX = x + (event.key === "ArrowLeft" ? -step : event.key === "ArrowRight" ? step : 0), nextY = y + (event.key === "ArrowUp" ? -step : event.key === "ArrowDown" ? step : 0);
    setBoxes((current) => current.map((item) => item.id === box.id ? resizeRect(item, corner, nextX, nextY) : item)); setDirty(true);
  };
  return <main className="page-shell annotation-shell">
    <header className="annotation-header"><div><a className="back-link" href={galleryHref} onClick={guardExit}>← Galeriye dön</a><p className="eyebrow">Manuel görsel etiketleme</p><h1>Etiketleme çalışma alanı</h1><p>{position + 1} / {project.total_images} · {completed} görsel etiketli</p></div><div className="annotation-actions"><button className="button secondary" disabled={position === 0 || locked} onClick={() => navigate(-1)}>Önceki görsel</button><button className="button secondary" disabled={position === project.images.length - 1 || locked} onClick={() => navigate(1)}>Sonraki görsel</button><button className="button secondary" disabled={!dirty || locked} onClick={() => void loadImage(project, position)}>Değişiklikleri geri al</button><button className="button primary" disabled={!dirty || locked} onClick={() => void save(false)}>{busy ? "Kaydediliyor…" : "Kaydet"}</button><button className="button primary" disabled={!dirty || locked || position === project.images.length - 1} onClick={() => void save(true)}>Kaydet ve sonraki</button></div></header>
    {error && <div className="alert error" role="alert">{error}{conflict && <button className="button secondary" disabled={busy || imageLoading} onClick={() => void loadProject(project.images[position].index)}>Sunucudaki sürümü yeniden yükle</button>}</div>}
    <div className="annotation-layout"><aside className="class-panel" aria-label="Sınıflar"><h2>Sınıflar</h2><form onSubmit={addClass}><label className="field">Yeni sınıf adı<input value={className} maxLength={80} disabled={locked} onChange={(event) => setClassName(event.target.value)} /></label><label className="color-field">Renk<input type="color" value={classColor} disabled={locked} onChange={(event) => setClassColor(event.target.value)} /></label><button className="button primary" disabled={locked || !className.trim() || project.classes.length >= project.limits.max_classes}>Sınıf ekle</button></form><ul className="class-list">{project.classes.map((item) => <li key={item.id} className={activeClass === item.id ? "active" : ""}><button className="class-select" disabled={locked} onClick={() => setActiveClass(item.id)} aria-pressed={activeClass === item.id}><span style={{ background: item.color }} />{item.name}</button><button disabled={locked} aria-label={`${item.name} sınıfını yeniden adlandır`} onClick={() => renameClass(item)}>Düzenle</button><button aria-label={`${item.name} sınıfını sil`} disabled={locked || boxes.some((box) => box.class_id === item.id)} onClick={() => removeClass(item)}>Sil</button></li>)}</ul></aside>
      <section className="canvas-panel" aria-label="Etiketleme görseli"><div className="image-title"><strong>{image.filename}</strong><span>{boxes.length} kutu</span></div><div className="annotation-canvas">{(imageLoading || !imageReady) && <div className="canvas-loading" role="status">Görsel doğrulanıyor…</div>}<img key={previewKey} src={image.preview_url} alt={image.filename} draggable={false} onLoad={(event) => { if (event.currentTarget.naturalWidth === 640 && event.currentTarget.naturalHeight === 640) { setReadyPreview(previewKey); setError(null); } else { setReadyPreview(null); setError("Görsel güvenli 640×640 önizleme formatında değil."); } }} onError={() => { setReadyPreview(null); setError("Görsel önizlemesi yüklenemedi."); }} />{imageReady && <svg aria-label="Bounding box çalışma alanı" viewBox="0 0 1 1" preserveAspectRatio="none" onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={endGesture} onPointerCancel={endGesture} onLostPointerCapture={endGesture}>{boxes.map((box) => { const itemClass = project.classes.find((item) => item.id === box.class_id); return <g key={box.id}><rect data-box-id={box.id} aria-label={`${itemClass?.name ?? "Sınıf"} kutusu${selected === box.id ? "; seçili" : ""}`} role="button" tabIndex={0} onFocus={() => setSelected(box.id)} x={box.left} y={box.top} width={box.width} height={box.height} fill={`${colorFor(project.classes, box.class_id)}22`} stroke={colorFor(project.classes, box.class_id)} strokeWidth={selected === box.id ? .008 : .005} vectorEffect="non-scaling-stroke" /><text pointerEvents="none" x={box.left + .006} y={Math.max(.025, box.top + .025)} fontSize=".025" fill="white" stroke="black" strokeWidth=".002">{itemClass?.name ?? "Sınıf"}</text>{selected === box.id && handles.map(({ corner, label }) => { const east = corner.endsWith("e"), south = corner.startsWith("s"); return <rect key={corner} data-box-id={box.id} data-handle={corner} aria-label={`${label}; ok tuşlarıyla yeniden boyutlandır`} role="button" tabIndex={0} onKeyDown={(event) => keyboardResize(event, box, corner)} x={box.left + (east ? box.width : 0) - .015} y={box.top + (south ? box.height : 0) - .015} width=".03" height=".03" fill={colorFor(project.classes, box.class_id)} />; })}</g>; })}</svg>}</div><p className="hint">Aktif sınıfı seçin ve görsel üzerinde sürükleyerek kutu çizin. Seçili kutuyu taşıyabilir, dört köşe tutamacından boyutlandırabilir veya Delete ile silebilirsiniz.</p></section></div>
  </main>;
}
