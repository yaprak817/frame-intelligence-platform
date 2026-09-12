import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DatasetGallery } from "@/components/results/dataset-gallery";
import { ResultView } from "@/components/results/result-view";

const jobId = "11111111-1111-4111-8111-111111111111";

const { getDatasetResult, getJobStatus } = vi.hoisted(() => ({
  getDatasetResult: vi.fn(),
  getJobStatus: vi.fn(),
}));
vi.mock("@/lib/api/client", () => ({ getDatasetResult, getJobStatus }));
vi.mock("@/components/results/result-gallery", () => ({
  ResultGallery: () => <div>video-gallery</div>,
}));

describe("dataset result gallery", () => {
  beforeEach(() => {
    getJobStatus.mockReset();
    getDatasetResult.mockResolvedValue({
      schema_version: 1,
      dataset_type: "image",
      job_id: "11111111-1111-4111-8111-111111111111",
      created_at: "2026-09-02T00:00:00Z",
      summary: { uploaded_files: 2, accepted_files: 1, normal: 1, challenging: 0, unusable: 1, rejected: 0, duplicates: 0, ignored_metadata_entries: 0, recommended_count: 1, recommended_normal: 1, recommended_challenging: 0, target_challenging_ratio: 0.2, actual_challenging_ratio: 0, ratio_note: null },
      recommended_indices: [0],
      images: [
        { index: 0, filename: "image_000001.jpg", content_type: "image/jpeg", size_bytes: 100, sha256: "a".repeat(64), width: 100, height: 50, quality_category: "normal", sharpness: 120, brightness: 100, underexposed_ratio: 0, overexposed_ratio: 0, resolution_usable: true, duplicate: false, access_url: "/api/v1/jobs/11111111-1111-4111-8111-111111111111/result/images/0/preview", download_url: "/download" },
        { index: 1, filename: "image_000002.jpg", content_type: "application/octet-stream", size_bytes: 5, sha256: "b".repeat(64), width: 0, height: 0, quality_category: "unusable", sharpness: 0, brightness: 0, underexposed_ratio: 0, overexposed_ratio: 0, resolution_usable: false, duplicate: false, access_url: null, download_url: null },
      ],
      accepted_download_url: "/accepted.zip",
      yolo_download_url: "/yolo.zip",
    });
  });

  it("shows summary, filters, preview and both exports", async () => {
    const user = userEvent.setup();
    render(<DatasetGallery jobId="11111111-1111-4111-8111-111111111111" />);
    expect(await screen.findByRole("heading", { name: "Veri seti analizi" })).toBeVisible();
    expect(screen.getByRole("link", { name: /Kabul edilen/ })).toHaveAttribute("href", expect.stringMatching(/\/accepted$/));
    expect(screen.getByRole("link", { name: /YOLO-ready/ })).toHaveAttribute("href", expect.stringMatching(/\/yolo$/));
    expect(screen.getByRole("img")).toHaveAttribute("src", expect.stringMatching(/^\/api\/v1\//));
    await user.click(screen.getByRole("button", { name: "Kullanılamaz" }));
    expect(screen.getByText("Önizleme kullanılamıyor")).toBeVisible();
    expect(screen.queryByRole("link", { name: "İndir" })).not.toBeInTheDocument();
  });

  it("keeps a rate-limited download in the app and prevents duplicate requests", async () => {
    const navigate = vi.fn();
    render(<DatasetGallery jobId="11111111-1111-4111-8111-111111111111" initialDownloadError="Çok fazla istek gönderildi. 7 saniye bekleyip tekrar deneyin." downloadNavigator={navigate} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("7 saniye bekleyip tekrar deneyin");
    const yolo = await screen.findByRole("link", { name: /YOLO-ready/ });
    const accepted = screen.getByRole("link", { name: /Kabul edilen/ });
    expect(fireEvent.click(yolo)).toBe(false);
    expect(fireEvent.click(accepted)).toBe(false);
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith(expect.stringMatching(/\/yolo$/));
    expect(yolo).toHaveAttribute("aria-disabled", "true");
    expect(accepted).toHaveAttribute("aria-disabled", "true");
  });

  it.each(["FAILED", "RUNNING", "QUEUED"])(
    "does not render a gallery for %s jobs",
    async (status) => {
      getJobStatus.mockResolvedValue({
        id: "11111111-1111-4111-8111-111111111111",
        status,
        source_type: "UPLOAD",
        source: "clip.mp4",
        created_at: "2026-09-02T00:00:00Z",
        started_at: null,
        completed_at: null,
        failure: status === "FAILED" ? { code: "FAILED", message: "safe" } : null,
        result: null,
      });
      render(<ResultView jobId="11111111-1111-4111-8111-111111111111" />);
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "Sonuç türü doğrulanamadı",
      );
      expect(screen.queryByText("video-gallery")).not.toBeInTheDocument();
    },
  );

  it.each([
    ["UPLOAD", "video-gallery"],
    ["IMAGE_DATASET", "Veri seti analizi"],
  ])("renders a validated successful %s result", async (sourceType, expected) => {
    getJobStatus.mockResolvedValue({
      id: "11111111-1111-4111-8111-111111111111",
      status: "SUCCEEDED",
      source_type: sourceType,
      source: "safe",
      created_at: "2026-09-02T00:00:00Z",
      started_at: null,
      completed_at: "2026-09-02T00:01:00Z",
      failure: null,
      result: {
        result_kind: sourceType === "IMAGE_DATASET" ? "IMAGE_DATASET" : "VIDEO_FRAMES",
        available: true,
        metadata_url: "/result",
        manifest_download_url: "/manifest",
      },
    });
    render(<ResultView jobId="11111111-1111-4111-8111-111111111111" />);
    expect(await screen.findByText(expected)).toBeVisible();
    if (sourceType === "IMAGE_DATASET") expect(screen.getByRole("link", { name: "Etiketlemeye başla" })).toHaveAttribute("href", `/jobs/${jobId}/annotations`);
    else expect(screen.queryByRole("link", { name: "Etiketlemeye başla" })).not.toBeInTheDocument();
  });

  it("rejects a source and result discriminator mismatch", async () => {
    getJobStatus.mockResolvedValue({
      id: "11111111-1111-4111-8111-111111111111",
      status: "SUCCEEDED",
      source_type: "IMAGE_DATASET",
      source: "safe",
      created_at: "2026-09-02T00:00:00Z",
      started_at: null,
      completed_at: "2026-09-02T00:01:00Z",
      failure: null,
      result: { result_kind: "VIDEO_FRAMES", available: true, metadata_url: "/result", manifest_download_url: "/manifest" },
    });
    render(<ResultView jobId="11111111-1111-4111-8111-111111111111" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Sonuç türü doğrulanamadı");
    expect(screen.queryByText("video-gallery")).not.toBeInTheDocument();
  });
});
