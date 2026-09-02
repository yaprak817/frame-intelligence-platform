import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { JobStatus, JobStatusResponse, PublicResultManifest } from "@/lib/api/types";

const api = vi.hoisted(() => ({ getJobStatus: vi.fn(), getJobResult: vi.fn(), getManifestDownload: vi.fn(), createFrameAccess: vi.fn(), createFrameExport: vi.fn(), getFrameExport: vi.fn() }));
vi.mock("@/lib/api/client", async (original) => ({ ...(await original<typeof import("@/lib/api/client")>()), ...api }));
import { ResultGallery } from "@/components/results/result-gallery";

const hash = "a".repeat(64);
const jobId = "11111111-1111-4111-8111-111111111111";
const job = (status: JobStatus, failure = status === "FAILED" ? { code: "INVALID_VIDEO", message: "private detail" } : null): JobStatusResponse => ({ id: jobId, status, source_type: "UPLOAD", source: "clip.mp4", created_at: "2026-08-24T10:00:00Z", started_at: null, completed_at: null, failure, result: status === "SUCCEEDED" ? { available: true, metadata_url: `/api/v1/jobs/${jobId}/result`, manifest_download_url: `/api/v1/jobs/${jobId}/result/manifest` } : null });
const manifest = (frames = 1): PublicResultManifest => ({ schema_version: 1, job_id: jobId, created_at: "2026-08-24T10:00:00Z", summary: { frames_saved: frames, candidates: 3, shortlisted: 2, duplicates_removed: 1, processing_seconds: 2, duration_seconds: 10 }, frames: Array.from({ length: frames }, (_, index) => ({ index, filename: `frame_${String(index).padStart(6, "0")}_${index * 1000}ms_640x480.jpg`, content_type: "image/jpeg", size_bytes: 10, sha256: hash, timestamp_ms: index * 1000, width: 640, height: 480, access_url: `/api/v1/jobs/${jobId}/result/frames/${index}/access` })) });

describe("result gallery", () => {
  beforeEach(() => {
    HTMLDialogElement.prototype.showModal = vi.fn(function (this: HTMLDialogElement) { this.setAttribute("open", ""); });
    HTMLDialogElement.prototype.close = vi.fn(function (this: HTMLDialogElement) { this.removeAttribute("open"); });
  });
  afterEach(() => { Object.values(api).forEach((mock) => mock.mockReset()); vi.unstubAllGlobals(); vi.useRealTimers(); });
  it("cancels an export polling timer on unmount", async () => {
    vi.useFakeTimers();
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest());
    api.createFrameExport.mockResolvedValue({ id: "22222222-2222-4222-8222-222222222222", job_id: jobId, status: "PREPARING", mode: "all", frame_count: 1, created_at: "2026-08-24T10:00:00Z", completed_at: null, status_url: "/status", download_url: null, failure_code: null });
    const view = render(<ResultGallery jobId={jobId} />);
    await vi.waitFor(() => expect(screen.getByRole("button", { name: /Tüm frame/ })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: /Tüm frame/ }));
    await vi.waitFor(() => expect(api.createFrameExport).toHaveBeenCalledOnce());
    const signal = api.createFrameExport.mock.calls[0][3] as AbortSignal;
    view.unmount();
    expect(signal.aborted).toBe(true); expect(vi.getTimerCount()).toBe(0); expect(api.getFrameExport).not.toHaveBeenCalled();
    vi.useRealTimers();
  });
  it("deduplicates synchronous export clicks", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest());
    api.createFrameExport.mockImplementation(() => new Promise(() => {}));
    render(<ResultGallery jobId={jobId} />);
    const button = await screen.findByRole("button", { name: /Tüm frame/ });
    fireEvent.click(button); fireEvent.click(button);
    expect(api.createFrameExport).toHaveBeenCalledTimes(1);
  });
  it.each(["PENDING_DISPATCH", "QUEUED", "RUNNING"] as JobStatus[])("shows not-ready for %s without requesting result", async (status) => {
    api.getJobStatus.mockResolvedValue(job(status)); render(<ResultGallery jobId={jobId} />);
    expect(await screen.findByRole("heading", { name: "Sonuç henüz hazır değil" })).toBeVisible(); expect(api.getJobResult).not.toHaveBeenCalled();
  });
  it("shows a safe failed state", async () => {
    api.getJobStatus.mockResolvedValue(job("FAILED", null)); render(<ResultGallery jobId={jobId} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("sonuç galerisi oluşturulamadı"); expect(screen.queryByText("private detail")).not.toBeInTheDocument();
  });
  it("shows summary and an explicit empty gallery", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest(0)); render(<ResultGallery jobId={jobId} />);
    expect(await screen.findByRole("heading", { name: "Analiz özeti" })).toBeVisible(); expect(screen.getByRole("heading", { name: "Gösterilecek kare yok" })).toBeVisible();
  });
  it("opens and closes the frame modal with keyboard and restores focus", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest()); api.createFrameAccess.mockResolvedValue({ url: "https://download.example/0", expires_at: new Date(Date.now() + 60_000).toISOString(), content_type: "image/jpeg", size_bytes: 10, sha256: hash });
    render(<ResultGallery jobId={jobId} />); const trigger = await screen.findByRole("button", { name: /büyüt/ }); await userEvent.click(trigger);
    expect(screen.getByRole("dialog")).toBeVisible(); await userEvent.keyboard("{Escape}"); expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); expect(trigger).toHaveFocus();
  });
  it("refreshes a failed presigned image only once", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest()); api.createFrameAccess.mockResolvedValue({ url: "https://download.example/0", expires_at: new Date(Date.now() + 60_000).toISOString(), content_type: "image/jpeg", size_bytes: 10, sha256: hash });
    render(<ResultGallery jobId={jobId} />); const image = await screen.findByRole("img"); fireEvent.error(image); await waitFor(() => expect(api.createFrameAccess).toHaveBeenCalledTimes(2)); fireEvent.error(image); expect(await screen.findByText("Kare yüklenemedi.")).toBeVisible(); expect(api.createFrameAccess).toHaveBeenCalledTimes(2);
  });
  it("downloads the public manifest without exposing its URL", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest(0)); api.getManifestDownload.mockResolvedValue(new Blob(["{}"]));
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {}); vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:test"), revokeObjectURL: vi.fn() });
    render(<ResultGallery jobId={jobId} />); await userEvent.click(await screen.findByRole("button", { name: "Public manifesti indir" })); await waitFor(() => expect(api.getManifestDownload).toHaveBeenCalledWith(jobId, expect.any(AbortSignal))); expect(click).toHaveBeenCalled();
  });
  it("traps focus in the native dialog in both directions", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest()); api.createFrameAccess.mockResolvedValue({ url: "https://download.example/0", expires_at: new Date(Date.now() + 60_000).toISOString(), content_type: "image/jpeg", size_bytes: 10, sha256: hash });
    render(<ResultGallery jobId={jobId} />); const trigger = await screen.findByRole("button", { name: /büyüt/ }); await userEvent.click(trigger);
    const close = screen.getByRole("button", { name: "Kapat" }); expect(close).toHaveFocus();
    await userEvent.tab(); expect(close).toHaveFocus(); await userEvent.tab({ shift: true }); expect(close).toHaveFocus();
    await userEvent.click(close); expect(trigger).toHaveFocus(); expect(document.body.style.overflow).toBe("");
  });
  it("refreshes an expired modal URL and retries an image error only once", async () => {
    api.getJobStatus.mockResolvedValue(job("SUCCEEDED")); api.getJobResult.mockResolvedValue(manifest());
    api.createFrameAccess.mockResolvedValueOnce({ url: "https://download.example/expired", expires_at: new Date(Date.now() - 1_000).toISOString(), content_type: "image/jpeg", size_bytes: 10, sha256: hash }).mockResolvedValue({ url: "https://download.example/fresh", expires_at: new Date(Date.now() + 60_000).toISOString(), content_type: "image/jpeg", size_bytes: 10, sha256: hash });
    render(<ResultGallery jobId={jobId} />); const trigger = await screen.findByRole("button", { name: /büyüt/ }); await userEvent.click(trigger);
    await waitFor(() => expect(api.createFrameAccess).toHaveBeenCalledTimes(2));
    fireEvent.error(screen.getByRole("dialog").querySelector("img")!); await waitFor(() => expect(api.createFrameAccess).toHaveBeenCalledTimes(3));
    fireEvent.error(screen.getByRole("dialog").querySelector("img")!); expect(await screen.findByText("Büyütülmüş kare yüklenemedi.")).toBeVisible(); expect(api.createFrameAccess).toHaveBeenCalledTimes(3);
  });
  it("aborts a manifest download on job change and hides its stale error", async () => {
    const secondJobId = "22222222-2222-4222-8222-222222222222";
    let downloadSignal!: AbortSignal;
    api.getJobStatus.mockImplementation((id: string) => Promise.resolve({ ...job("SUCCEEDED"), id }));
    api.getJobResult.mockImplementation((id: string) => Promise.resolve({ ...manifest(0), job_id: id }));
    api.getManifestDownload.mockImplementation((_id: string, signal: AbortSignal) => new Promise((_resolve, reject) => {
      downloadSignal = signal;
      signal.addEventListener("abort", () => reject(new DOMException("internal stale detail", "AbortError")), { once: true });
    }));
    const view = render(<ResultGallery jobId={jobId} />); await userEvent.click(await screen.findByRole("button", { name: "Public manifesti indir" }));
    expect(screen.getByRole("button", { name: "Manifest indiriliyor…" })).toBeDisabled();
    view.rerender(<ResultGallery jobId={secondJobId} />); expect(downloadSignal.aborted).toBe(true);
    await waitFor(() => expect(api.getJobResult).toHaveBeenCalledWith(secondJobId, expect.any(AbortSignal)));
    expect(await screen.findByRole("heading", { name: "Analiz özeti" })).toBeVisible();
    await waitFor(() => expect(screen.getByRole("button", { name: "Public manifesti indir" })).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText(/internal stale detail/)).not.toBeInTheDocument();
    expect(screen.getByText(secondJobId)).toBeVisible();
  });

  it("aborts the previous job and hides stale state on navigation", async () => {
    let firstSignal!: AbortSignal;
    api.getJobStatus.mockImplementation((id: string, signal: AbortSignal) => {
      if (id === "old-job") {
        firstSignal = signal;
        return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))));
      }
      return Promise.resolve({ ...job("RUNNING"), id: "new-job" });
    });
    const view = render(<ResultGallery jobId="old-job" />);
    view.rerender(<ResultGallery jobId="new-job" />);
    expect(firstSignal.aborted).toBe(true);
    expect(await screen.findByRole("heading", { name: "Sonuç henüz hazır değil" })).toBeVisible();
    expect(screen.getByText(/new-job/)).toBeVisible();
    expect(screen.queryByText(/old-job/)).not.toBeInTheDocument();
  });
});
