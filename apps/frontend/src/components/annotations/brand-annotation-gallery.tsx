"use client";

import { useEffect, useState } from "react";
import { AnnotationGallery } from "./annotation-gallery";

function errorMessage(payload: unknown): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;

    if (typeof detail === "string") return detail;

    if (
      detail &&
      typeof detail === "object" &&
      "message" in detail &&
      typeof (detail as { message?: unknown }).message === "string"
    ) {
      return (detail as { message: string }).message;
    }
  }

  return "Marka etiketleme alan? haz?rlanamad?.";
}

export function BrandAnnotationGallery({ brandId }: { brandId: string }) {
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    void fetch(
      `/api/v1/brands/${encodeURIComponent(brandId)}/annotations?page=1&page_size=100`,
      {
        method: "POST",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal: controller.signal,
      },
    )
      .then(async (response) => {
        const payload = await response.json().catch(() => null);

        if (!response.ok) {
          throw new Error(errorMessage(payload));
        }

        const projectJobId =
          payload &&
          typeof payload === "object" &&
          "job_id" in payload &&
          typeof (payload as { job_id?: unknown }).job_id === "string"
            ? (payload as { job_id: string }).job_id
            : null;

        if (!projectJobId) {
          throw new Error("Etiketleme projesinin kaynak i?i bulunamad?.");
        }

        if (active) {
          setJobId(projectJobId);
          setError(null);
        }
      })
      .catch((caught) => {
        if (
          active &&
          !(caught instanceof DOMException && caught.name === "AbortError")
        ) {
          setError(
            caught instanceof Error
              ? caught.message
              : "Marka etiketleme alan? haz?rlanamad?.",
          );
        }
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [brandId]);

  if (error) {
    return (
      <main className="page-shell">
        <div className="alert error" role="alert">
          {error}
        </div>
      </main>
    );
  }

  if (!jobId) {
    return (
      <main className="page-shell">
        <div className="status-message" role="status">
          Marka etiketleme alan? haz?rlan?yor?
        </div>
      </main>
    );
  }

  return <AnnotationGallery jobId={jobId} />;
}
