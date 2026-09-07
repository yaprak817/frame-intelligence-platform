"use client";

import { useEffect, useState } from "react";
import { getJobStatus } from "@/lib/api/client";
import { DatasetGallery } from "./dataset-gallery";
import { ResultGallery } from "./result-gallery";

export function ResultView({ jobId }: { jobId: string }) {
  return <ResolvedResultView key={jobId} jobId={jobId} />;
}

function ResolvedResultView({ jobId }: { jobId: string }) {
  const [dataset, setDataset] = useState<boolean | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    void getJobStatus(jobId, controller.signal).then((job) => {
      if (job.status !== "SUCCEEDED" || job.result?.available !== true) {
        setError(true);
        return;
      }
      const dataset = job.source_type === "IMAGE_DATASET";
      if ((dataset ? "IMAGE_DATASET" : "VIDEO_FRAMES") !== job.result.result_kind) {
        setError(true);
        return;
      }
      setDataset(dataset);
      setError(false);
    }).catch((caught) => {
      if (!(caught instanceof DOMException && caught.name === "AbortError")) setError(true);
    });
    return () => controller.abort();
  }, [jobId]);
  if (error) return <main className="page-shell"><div className="alert error" role="alert">Sonuç türü doğrulanamadı.</div></main>;
  if (dataset === null) return <main className="page-shell"><div className="status-message" role="status">Sonuç türü alınıyor…</div></main>;
  return dataset ? <DatasetGallery jobId={jobId} /> : <ResultGallery jobId={jobId} />;
}
