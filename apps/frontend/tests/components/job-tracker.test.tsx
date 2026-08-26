import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { JobStatus, JobStatusResponse } from "@/lib/api/types";

const useJobPolling = vi.fn();
vi.mock("@/hooks/use-job-polling", () => ({ useJobPolling: () => useJobPolling() }));
import { JobTracker } from "@/components/jobs/job-tracker";

function response(status: JobStatus): JobStatusResponse {
  return { id: "job-1", status, source_type: "UPLOAD", source: "clip.mp4", created_at: "2026-08-24T10:00:00Z", started_at: null, completed_at: null, failure: status === "FAILED" ? { code: "INVALID_VIDEO", message: "internal worker detail" } : null, result: status === "SUCCEEDED" ? { available: true, metadata_url: "/api/v1/jobs/job-1/result", manifest_download_url: "/api/v1/jobs/job-1/result/manifest" } : null };
}

describe("job tracker", () => {
  it.each([
    ["PENDING_DISPATCH", "İş sıraya gönderiliyor"],
    ["QUEUED", "İşleme sırası bekleniyor"],
    ["RUNNING", "Video işleniyor"],
    ["SUCCEEDED", "İşleme tamamlandı"],
    ["FAILED", "İşleme tamamlanamadı"],
  ] as [JobStatus, string][])("renders %s", (status, label) => {
    useJobPolling.mockReturnValue({ job: response(status), error: null, loading: false });
    render(<JobTracker jobId="job-1" />);
    expect(screen.getByRole("heading", { name: label })).toBeVisible();
    expect(screen.getByText(label, { selector: ".status-label" })).toBeVisible();
    expect(screen.queryByText(status.replace("_", " "))).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "İş durumu" })).toHaveFocus();
    expect(screen.queryByText("internal worker detail")).not.toBeInTheDocument();
  });

  it("announces polling errors", () => {
    useJobPolling.mockReturnValue({ job: null, error: "İş bulunamadı.", loading: false });
    render(<JobTracker jobId="missing" />);
    expect(screen.getByRole("alert")).toHaveTextContent("İş bulunamadı.");
  });
});
