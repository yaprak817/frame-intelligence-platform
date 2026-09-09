import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { JobSubmission } from "@/components/submission/job-submission";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

class FakeXHR {
  static latest: FakeXHR;
  static instances: FakeXHR[] = [];
  status = 202;
  response: unknown = { job_id: "11111111-1111-4111-8111-111111111111", status: "PENDING_DISPATCH", status_url: "/api/v1/jobs/1" };
  upload = { addEventListener: vi.fn((name: string, callback: (event: ProgressEvent) => void) => { if (name === "progress") this.progress = callback; }) };
  listeners: Record<string, () => void> = {};
  progress?: (event: ProgressEvent) => void;
  open = vi.fn(); setRequestHeader = vi.fn(); send = vi.fn(() => { this.progress?.({ lengthComputable: true, loaded: 5, total: 10 } as ProgressEvent); });
  abort = vi.fn(() => this.listeners.abort?.());
  addEventListener(name: string, callback: () => void) { this.listeners[name] = callback; }
  set responseType(_value: XMLHttpRequestResponseType) {}
  constructor() { FakeXHR.latest = this; FakeXHR.instances.push(this); }
}

describe("job submission", () => {
  beforeEach(() => {
    push.mockReset();
    FakeXHR.instances = [];
    vi.stubGlobal("crypto", { randomUUID: () => "request-id" });
    vi.stubGlobal("XMLHttpRequest", FakeXHR);
    vi.stubGlobal("fetch", vi.fn());
  });

  it("has labelled controls and focuses validation alert", async () => {
    const user = userEvent.setup();
    render(<JobSubmission />);
    expect(screen.getByLabelText("Video dosyası")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "İşi başlat" }));
    const alert = screen.getByRole("alert");
    expect(alert).toHaveFocus();
    expect(screen.getByLabelText("Video dosyası")).toHaveAttribute("aria-invalid", "true");
  });

  it("submits a URL with a same-origin request and navigates", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ job_id: "job-url", status: "PENDING_DISPATCH", status_url: "/api/v1/jobs/job-url" }), { status: 202, headers: { "Content-Type": "application/json" } }));
    const user = userEvent.setup();
    render(<JobSubmission />);
    await user.click(screen.getByRole("tab", { name: "Video URL’si" }));
    await user.type(screen.getByLabelText("Video bağlantısı"), "https://example.com/video");
    await user.click(screen.getByRole("button", { name: "İşi başlat" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/jobs/job-url"));
    expect(fetch).toHaveBeenCalledWith("/api/v1/jobs/url", expect.objectContaining({ method: "POST", headers: expect.objectContaining({ "Idempotency-Key": "web-request-id" }) }));
  });

  it("reports upload progress and supports abort", async () => {
    const user = userEvent.setup();
    render(<JobSubmission />);
    const input = screen.getByLabelText("Video dosyası");
    await user.upload(input, new File(["video"], "clip.mp4", { type: "video/mp4" }));
    fireEvent.submit(input.closest("form")!);
    expect(await screen.findByRole("progressbar")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Yüklemeyi iptal et" }));
    expect(FakeXHR.latest.abort).toHaveBeenCalledOnce();
    await waitFor(() => expect(screen.queryByRole("progressbar")).not.toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "İşi başlat" })).toBeEnabled();
  });

  it("blocks two submissions dispatched in the same event loop", async () => {
    render(<JobSubmission />);
    const input = screen.getByLabelText("Video dosyası");
    fireEvent.change(input, { target: { files: [new File(["video"], "clip.mp4", { type: "video/mp4" })] } });
    const form = input.closest("form")!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(FakeXHR.instances).toHaveLength(1);
  });

  it("keeps a single submission lifecycle and navigates under Strict Mode", async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ job_id: "job-url", status: "PENDING_DISPATCH", status_url: "/api/v1/jobs/job-url" }), { status: 202, headers: { "Content-Type": "application/json" } }));
    render(<StrictMode><JobSubmission /></StrictMode>);
    fireEvent.click(screen.getByRole("tab", { name: "Video URL’si" }));
    const input = screen.getByLabelText("Video bağlantısı");
    fireEvent.change(input, { target: { value: "https://example.com/video" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/jobs/job-url"));
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([413, 415, 422])("shows a safe Turkish message for %s", async (status) => {
    vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ detail: "private bucket credential" }), { status, headers: { "Content-Type": "application/json" } }));
    const user = userEvent.setup();
    render(<JobSubmission />);
    await user.click(screen.getByRole("tab", { name: "Video URL’si" }));
    await user.type(screen.getByLabelText("Video bağlantısı"), "https://example.com/video");
    await user.click(screen.getByRole("button", { name: "İşi başlat" }));
    const alert = await screen.findByRole("alert");
    expect(alert).not.toHaveTextContent("private bucket credential");
  });
});
