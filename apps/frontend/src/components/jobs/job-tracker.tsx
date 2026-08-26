"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { failureMessage } from "@/lib/api/errors";
import type { JobStatus } from "@/lib/api/types";
import { useJobPolling } from "@/hooks/use-job-polling";

const STATUS_COPY: Record<JobStatus, { label: string; detail: string }> = {
  PENDING_DISPATCH: { label: "İş sıraya gönderiliyor", detail: "İş kaydedildi ve işleme kuyruğuna aktarılıyor." },
  QUEUED: { label: "İşleme sırası bekleniyor", detail: "Video işleme için hazır ve worker bekliyor." },
  RUNNING: { label: "Video işleniyor", detail: "Kaliteli ve çeşitli kareler seçiliyor." },
  SUCCEEDED: { label: "İşleme tamamlandı", detail: "Sonuç hazır. Analiz özetini ve seçilen kareleri görüntüleyebilirsiniz." },
  FAILED: { label: "İşleme tamamlanamadı", detail: "Video için sonuç üretilemedi." },
};

export function JobTracker({ jobId }: { jobId: string }) {
  const { job, error, loading } = useJobPolling(jobId);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => heading.current?.focus(), []);

  const copy = job ? STATUS_COPY[job.status] : null;
  return (
    <div className="page-shell job-shell">
      <Link className="back-link" href="/">← Yeni iş oluştur</Link>
      <header className="job-heading">
        <p className="eyebrow">İş takibi</p>
        <h1 ref={heading} tabIndex={-1}>İş durumu</h1>
        <div className="job-id-row"><span>İş kimliği</span><code>{jobId}</code></div>
      </header>

      <section className="status-card" aria-live="polite" aria-busy={loading}>
        {loading && !job && <div className="status-message"><span className="spinner" aria-hidden="true" />Durum alınıyor…</div>}
        {error && <div className="alert error" role="alert">{error}</div>}
        {job && copy && (
          <>
            <div className={`status-icon status-${job.status.toLowerCase()}`} aria-hidden="true" />
            <span className="status-label">{copy.label}</span>
            <h2>{copy.label}</h2>
            <p>{copy.detail}</p>
            <dl className="job-details">
              <div><dt>Kaynak</dt><dd>{job.source}</dd></div>
              <div><dt>Tür</dt><dd>{job.source_type === "UPLOAD" ? "Dosya yükleme" : "Video URL’si"}</dd></div>
              <div><dt>Oluşturulma</dt><dd>{new Intl.DateTimeFormat("tr-TR", { dateStyle: "medium", timeStyle: "medium" }).format(new Date(job.created_at))}</dd></div>
            </dl>
            {job.status === "FAILED" && job.failure && (
              <div className="failure-panel" role="alert">
                <strong>{failureMessage(job.failure.code)}</strong>
                <span>Güvenle yeni bir iş oluşturup tekrar deneyebilirsiniz.</span>
              </div>
            )}
            {job.status === "SUCCEEDED" && <div className="success-panel" role="status"><span>Sonuç hazır.</span><Link className="button primary result-link" href={`/jobs/${encodeURIComponent(jobId)}/result`}>Sonuçları görüntüle</Link></div>}
          </>
        )}
      </section>
    </div>
  );
}
