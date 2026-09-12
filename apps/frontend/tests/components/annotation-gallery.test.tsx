import { render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ getOrCreateAnnotationProject: vi.fn() }));
vi.mock("@/lib/api/client", () => api);
import { AnnotationGallery } from "@/components/annotations/annotation-gallery";

const jobId = "11111111-1111-4111-8111-111111111111";
const images = Array.from({ length: 13 }, (_, index) => ({
  index,
  filename: `${index}.jpg`,
  completed: index === 0,
  box_count: index === 0 ? 1 : 0,
  preview_url: `/api/v1/jobs/${jobId}/annotations/images/${index}/preview`,
}));
const project = {
  id: "33333333-3333-4333-8333-333333333333", job_id: jobId, revision: 2,
  classes: [], images, page: 1, page_size: 100, total_images: images.length,
  limits: { max_classes: 100, max_boxes_per_image: 200, max_boxes_per_project: 50_000 },
};

describe("annotation gallery", () => {
  beforeEach(() => { api.getOrCreateAnnotationProject.mockReset().mockResolvedValue(project); });

  it("renders cards, safe lazy previews, status and count", async () => {
    render(<AnnotationGallery jobId={jobId} />);
    expect(await screen.findByText("1 / 13 manuel etiketlendi")).toBeVisible();
    expect(screen.getAllByText("MANUEL ETİKETLENDİ")).toHaveLength(1);
    expect(screen.getAllByText("ETİKETLENMEDİ")).toHaveLength(11);
    const first = screen.getByRole("link", { name: /^0\.jpg/ });
    expect(first).toHaveAttribute("href", `/jobs/${jobId}/annotations/0`);
    expect(screen.getByRole("img", { name: "0.jpg" })).toHaveAttribute("loading", "lazy");
    expect(document.body.innerHTML).not.toMatch(/backend:8000|minio:9000|run_token|object_key|bucket/i);
  });

  it("filters, sorts and keeps pagination boundaries safe", async () => {
    const user = userEvent.setup();
    render(<AnnotationGallery jobId={jobId} />);
    await screen.findByText("1 / 13 manuel etiketlendi");
    expect(screen.getByText("1 / 2")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Sonraki sayfa" }));
    expect(screen.getByText("2 / 2")).toBeVisible();
    expect(screen.getByRole("button", { name: "Sonraki sayfa" })).toBeDisabled();
    await user.selectOptions(screen.getByLabelText("Durum"), "manual");
    expect(screen.getByText("1 / 1")).toBeVisible();
    expect(screen.getByText("MANUEL ETİKETLENDİ")).toBeVisible();
    await user.selectOptions(screen.getByLabelText("Durum"), "all");
    await user.selectOptions(screen.getByLabelText("Sıralama"), "desc");
    expect(screen.getAllByRole("link").find((link) => link.classList.contains("annotation-card"))).toHaveAttribute("href", `/jobs/${jobId}/annotations/12`);
  });

  it("does not duplicate project creation under Strict Mode", async () => {
    render(<StrictMode><AnnotationGallery jobId={jobId} /></StrictMode>);
    await screen.findByText("1 / 13 manuel etiketlendi");
    await waitFor(() => expect(api.getOrCreateAnnotationProject).toHaveBeenCalledTimes(1));
  });
});
