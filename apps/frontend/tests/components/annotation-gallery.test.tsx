import { act, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/errors";

const api = vi.hoisted(() => ({
  getOrCreateAnnotationProject: vi.fn(),
  listAnnotationTrainings: vi.fn(),
  createAndStartAnnotationTraining: vi.fn(),
  getAnnotationTraining: vi.fn(),
}));
vi.mock("@/lib/api/client", () => api);
import { AnnotationGallery, POLL_MAX_DELAY_MS, POLL_MAX_RETRIES, trainingPollRetryDelay } from "@/components/annotations/annotation-gallery";

const jobId = "11111111-1111-4111-8111-111111111111";
const images = Array.from({ length: 13 }, (_, index) => ({
  index,
  filename: `${index}.jpg`,
  width: 640,
  height: 640,
  timestamp_ms: index === 0 ? 1250 : null,
  completed: index === 0,
  box_count: index === 0 ? 1 : 0,
  preview_url: `/api/v1/jobs/${jobId}/annotations/images/${index}/preview`,
}));
const project = {
  id: "33333333-3333-4333-8333-333333333333", job_id: jobId, revision: 2,
  classes: [], images, page: 1, page_size: 100, total_images: images.length,
  limits: { max_classes: 100, max_boxes_per_image: 200, max_boxes_per_project: 50_000 },
};
const running = {
  id: "44444444-4444-4444-8444-444444444444", snapshot_version: 1,
  source_revision: 2, status: "RUNNING", image_count: 50, class_count: 1,
  box_count: 1, train_image_count: 40, validation_image_count: 10,
  config: { max_snapshot_images: 50, epochs: 1, batch_size: 1, image_size: 640 },
  created_at: "2026-09-14T00:00:00Z", started_at: "2026-09-14T00:00:01Z",
  completed_at: null, failure_code: null, progress_completed: 0,
  progress_total: 1, model_version: null,
  status_url: `/api/v1/jobs/${jobId}/annotations/trainings/44444444-4444-4444-8444-444444444444`,
  snapshot_download_url: null,
} as const;

describe("annotation gallery", () => {
  beforeEach(() => {
    api.getOrCreateAnnotationProject.mockReset().mockResolvedValue(project);
    api.listAnnotationTrainings.mockReset().mockResolvedValue([]);
    api.createAndStartAnnotationTraining.mockReset();
    api.getAnnotationTraining.mockReset();
  });

  it("renders cards, safe lazy previews, status and count", async () => {
    render(<AnnotationGallery jobId={jobId} />);
    expect(await screen.findByText("1 / 13 manuel etiketlendi")).toBeVisible();
    expect(screen.getAllByText("MANUEL ETİKETLENDİ")).toHaveLength(1);
    expect(screen.getAllByText("ETİKETLENMEDİ")).toHaveLength(11);
    const first = screen.getByRole("link", { name: /^0\.jpg/ });
    expect(first).toHaveAttribute("href", `/jobs/${jobId}/annotations/0`);
    expect(screen.getByRole("img", { name: "0.jpg" })).toHaveAttribute("loading", "lazy");
    expect(screen.getByText("1.250 sn")).toBeVisible();
    expect(screen.getAllByText("640×640").length).toBeGreaterThan(0);
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

describe("training polling backoff", () => {
  afterEach(() => vi.useRealTimers());

  it("retries only transient failures with bounded exponential delays", () => {
    expect(trainingPollRetryDelay(new TypeError("network"), 0)).toBe(1500);
    expect(trainingPollRetryDelay(new ApiError(503), 1)).toBe(3000);
    expect(trainingPollRetryDelay(new ApiError(429, undefined, 60), 2)).toBe(POLL_MAX_DELAY_MS);
    expect(trainingPollRetryDelay(new ApiError(504), 20)).toBe(POLL_MAX_DELAY_MS);
    expect(trainingPollRetryDelay(new ApiError(400), 0)).toBeNull();
    expect(trainingPollRetryDelay(new Error("bug"), 0)).toBeNull();
  });

  it("recovers after a transient failure and stops at terminal status", async () => {
    vi.useFakeTimers();
    api.listAnnotationTrainings.mockResolvedValue([running]);
    api.getAnnotationTraining.mockRejectedValueOnce(new ApiError(503)).mockResolvedValueOnce({
      ...running, status: "SUCCEEDED", progress_completed: 1,
      completed_at: "2026-09-14T00:00:03Z", model_version: 1,
      snapshot_download_url: `${running.status_url}/snapshot/download`,
    });
    render(<AnnotationGallery jobId={jobId} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(2);
    expect(screen.getByText("Model v1 hazır")).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(8000); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(2);
  });

  it("stops after the bounded retry budget and offers a manual retry", async () => {
    vi.useFakeTimers();
    api.listAnnotationTrainings.mockResolvedValue([running]);
    api.getAnnotationTraining.mockRejectedValue(new ApiError(503));
    render(<AnnotationGallery jobId={jobId} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(POLL_MAX_RETRIES + 1);
    expect(screen.getByRole("button", { name: "Durumu yeniden dene" })).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(POLL_MAX_RETRIES + 1);
  });

  it("does not retry a non-retryable response and aborts on unmount", async () => {
    vi.useFakeTimers();
    api.listAnnotationTrainings.mockResolvedValue([running]);
    api.getAnnotationTraining.mockRejectedValue(new ApiError(400));
    const view = render(<AnnotationGallery jobId={jobId} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(1);
    const signal = api.getAnnotationTraining.mock.calls[0][2] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(1);
  });

  it("creates one Strict Mode poll and prevents duplicate training clicks", async () => {
    vi.useFakeTimers();
    api.listAnnotationTrainings.mockResolvedValue([running]);
    api.getAnnotationTraining.mockResolvedValue(running);
    const strict = render(<StrictMode><AnnotationGallery jobId={jobId} /></StrictMode>);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(1);
    strict.unmount();

    api.listAnnotationTrainings.mockResolvedValue([]);
    api.getOrCreateAnnotationProject.mockResolvedValue({
      ...project,
      images: Array.from({ length: 50 }, (_, index) => ({ ...images[0], index, filename: `${index}.jpg`, completed: true })),
      total_images: 50,
    });
    let finish!: (value: typeof running) => void;
    api.createAndStartAnnotationTraining.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    render(<AnnotationGallery jobId={jobId} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    const button = screen.getByRole("button", { name: /Modeli/ });
    await act(async () => { button.click(); button.click(); });
    expect(api.createAndStartAnnotationTraining).toHaveBeenCalledTimes(1);
    await act(async () => finish(running));
  });

  it("ignores a deferred training list from a previous job", async () => {
    const nextJobId = "22222222-2222-4222-8222-222222222222";
    let finishOld!: (value: typeof running[]) => void;
    api.getOrCreateAnnotationProject.mockImplementation((id: string) => Promise.resolve({ ...project, job_id: id }));
    api.listAnnotationTrainings.mockImplementation((id: string) => id === jobId
      ? new Promise((resolve) => { finishOld = resolve; })
      : Promise.resolve([]));
    const view = render(<AnnotationGallery jobId={jobId} />);
    await screen.findByText("1 / 13 manuel etiketlendi");
    const oldSignal = api.listAnnotationTrainings.mock.calls[0][1] as AbortSignal;
    view.rerender(<AnnotationGallery jobId={nextJobId} />);
    await waitFor(() => expect(screen.getByRole("link", { name: /Sonuca dön/ })).toHaveAttribute("href", `/jobs/${nextJobId}/result`));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => finishOld([running]));
    expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
    expect(api.getAnnotationTraining).not.toHaveBeenCalled();
  });

  it("does not show an old mutation result or abort rejection after a job change", async () => {
    const nextJobId = "22222222-2222-4222-8222-222222222222";
    const completeImages = Array.from({ length: 50 }, (_, index) => ({ ...images[0], index, completed: true }));
    api.getOrCreateAnnotationProject.mockImplementation((id: string) => Promise.resolve({ ...project, job_id: id, images: completeImages, total_images: 50 }));
    let finishOld!: (value: typeof running) => void;
    api.createAndStartAnnotationTraining.mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }));
    const view = render(<AnnotationGallery jobId={jobId} />);
    await screen.findByText("50 / 50 manuel etiketlendi");
    await userEvent.setup().click(screen.getByRole("button", { name: "Modeli eğit" }));
    const oldSignal = api.createAndStartAnnotationTraining.mock.calls[0][3] as AbortSignal;
    view.rerender(<AnnotationGallery jobId={nextJobId} />);
    await waitFor(() => expect(screen.getByRole("link", { name: /Sonuca dön/ })).toHaveAttribute("href", `/jobs/${nextJobId}/result`));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => finishOld(running));
    expect(screen.queryByText("RUNNING")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Modeli eğit" })).toBeEnabled();
    let rejectSecond!: (reason: Error) => void;
    api.createAndStartAnnotationTraining.mockImplementationOnce(() => new Promise((_, reject) => { rejectSecond = reject; }));
    await userEvent.setup().click(screen.getByRole("button", { name: "Modeli eğit" }));
    expect(api.createAndStartAnnotationTraining).toHaveBeenCalledTimes(2);
    const thirdJobId = "33333333-3333-4333-8333-333333333333";
    view.rerender(<AnnotationGallery jobId={thirdJobId} />);
    await waitFor(() => expect(screen.getByRole("link", { name: /Sonuca dön/ })).toHaveAttribute("href", `/jobs/${thirdJobId}/result`));
    await act(async () => rejectSecond(new DOMException("aborted", "AbortError")));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not schedule a poll or update state after an unmounted request resolves", async () => {
    vi.useFakeTimers();
    api.listAnnotationTrainings.mockResolvedValue([running]);
    let finishPoll!: (value: typeof running) => void;
    api.getAnnotationTraining.mockReturnValue(new Promise((resolve) => { finishPoll = resolve; }));
    const view = render(<AnnotationGallery jobId={jobId} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(1);
    const signal = api.getAnnotationTraining.mock.calls[0][2] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => finishPoll(running));
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
    expect(api.getAnnotationTraining).toHaveBeenCalledTimes(1);
  });
});
